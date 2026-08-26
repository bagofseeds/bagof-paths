"""Glob matching for ``match`` / ``full_match``.

``full_match`` needs CPython 3.13's ``**``-aware, whole-path glob semantics on
every supported interpreter, so the two translation helpers below are adapted
from CPython 3.13's ``glob.translate`` and ``fnmatch._translate`` (PSF
license; see ``LICENSE-PSF-2.0.txt`` and ``NOTICE.md``). They are reformatted
to this repository's style and given type annotations, but the logic is
unchanged. Matching runs on the wrapper's canonical, scheme-less path with
``/`` separators, so it behaves identically regardless of the wrapped driver.

``match`` (right-anchored) delegates to the stdlib ``PurePosixPath.match``,
which exists on every supported version.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import PurePosixPath

import typing_extensions as tx

# Canonical (posix-style) separator used for all matching.
_SEP = "/"


def _fnmatch_translate(
    pat: str, star: str, question_mark: str
) -> tx.List[str]:
    """Adapted from CPython 3.13 ``fnmatch._translate``."""
    res = []
    add = res.append
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        i = i + 1
        if c == "*":
            # compress consecutive `*` into one
            if (not res) or res[-1] is not star:
                add(star)
        elif c == "?":
            add(question_mark)
        elif c == "[":
            j = i
            if j < n and pat[j] == "!":
                j = j + 1
            if j < n and pat[j] == "]":
                j = j + 1
            while j < n and pat[j] != "]":
                j = j + 1
            if j >= n:
                add("\\[")
            else:
                stuff = pat[i:j]
                if "-" not in stuff:
                    stuff = stuff.replace("\\", r"\\")
                else:
                    chunks = []
                    k = i + 2 if pat[i] == "!" else i + 1
                    while True:
                        k = pat.find("-", k, j)
                        if k < 0:
                            break
                        chunks.append(pat[i:k])
                        i = k + 1
                        k = k + 3
                    chunk = pat[i:j]
                    if chunk:
                        chunks.append(chunk)
                    else:
                        chunks[-1] += "-"
                    # Remove empty ranges -- invalid in RE.
                    for k in range(len(chunks) - 1, 0, -1):
                        if chunks[k - 1][-1] > chunks[k][0]:
                            chunks[k - 1] = chunks[k - 1][:-1] + chunks[k][1:]
                            del chunks[k]
                    # Escape backslashes and hyphens for set difference (--).
                    # Hyphens that create ranges shouldn't be escaped.
                    stuff = "-".join(
                        s.replace("\\", r"\\").replace("-", r"\-")
                        for s in chunks
                    )
                # Escape set operations (&&, ~~ and ||).
                stuff = re.sub(r"([&~|])", r"\\\1", stuff)
                i = j + 1
                if not stuff:
                    # Empty range: never match.
                    add("(?!)")
                elif stuff == "!":
                    # Negated empty range: match any character.
                    add(".")
                else:
                    if stuff[0] == "!":
                        stuff = "^" + stuff[1:]
                    elif stuff[0] in ("^", "["):
                        stuff = "\\" + stuff
                    add(f"[{stuff}]")
        else:
            add(re.escape(c))
    assert i == n
    return res


def _glob_translate(
    pat: str,
    *,
    recursive: bool = False,
    include_hidden: bool = False,
    seps: tx.Optional[str] = None,
) -> str:
    """Adapted from CPython 3.13 ``glob.translate``."""
    if not seps:
        if os.path.altsep:
            seps = (os.path.sep, os.path.altsep)
        else:
            seps = os.path.sep
    escaped_seps = "".join(map(re.escape, seps))
    any_sep = f"[{escaped_seps}]" if len(seps) > 1 else escaped_seps
    not_sep = f"[^{escaped_seps}]"
    if include_hidden:
        one_last_segment = f"{not_sep}+"
        one_segment = f"{one_last_segment}{any_sep}"
        any_segments = f"(?:.+{any_sep})?"
        any_last_segments = ".*"
    else:
        one_last_segment = f"[^{escaped_seps}.]{not_sep}*"
        one_segment = f"{one_last_segment}{any_sep}"
        any_segments = f"(?:{one_segment})*"
        any_last_segments = f"{any_segments}(?:{one_last_segment})?"

    results = []
    parts = re.split(any_sep, pat)
    last_part_idx = len(parts) - 1
    for idx, part in enumerate(parts):
        if part == "*":
            results.append(
                one_segment if idx < last_part_idx else one_last_segment
            )
        elif recursive and part == "**":
            if idx < last_part_idx:
                if parts[idx + 1] != "**":
                    results.append(any_segments)
            else:
                results.append(any_last_segments)
        else:
            if part:
                if not include_hidden and part[0] in "*?":
                    results.append(r"(?!\.)")
                results.extend(
                    _fnmatch_translate(part, f"{not_sep}*", not_sep)
                )
            if idx < last_part_idx:
                results.append(any_sep)
    res = "".join(results)
    return rf"(?s:{res})\Z"


def full_match(
    path: str, pattern: str, *, case_sensitive: tx.Optional[bool] = None
) -> bool:
    """Whether the whole ``path`` matches ``pattern`` (``**`` spans segments).

    Uses CPython 3.13's glob semantics on every interpreter.
    """
    pat = pattern.rstrip(_SEP) or pattern
    regex = _glob_translate(
        pat, recursive=True, include_hidden=True, seps=_SEP
    )
    flags = re.IGNORECASE if case_sensitive is False else 0
    return re.match(regex, path, flags) is not None


def match(
    path: str, pattern: str, *, case_sensitive: tx.Optional[bool] = None
) -> bool:
    """Whether ``path`` matches ``pattern``, anchored from the right."""
    pure = PurePosixPath(path)
    if case_sensitive is None:
        return pure.match(pattern)
    if sys.version_info >= (3, 12):
        return pure.match(pattern, case_sensitive=case_sensitive)
    if case_sensitive:
        return pure.match(pattern)
    return PurePosixPath(path.lower()).match(pattern.lower())
