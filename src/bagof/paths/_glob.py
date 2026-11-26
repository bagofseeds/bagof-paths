"""One `glob`/`rglob` for every driver: pathlib 3.14's, on every version.

The selectors below are adapted from CPython 3.14's `glob._GlobberBase`, and
the pattern handling from its `pathlib.Path.glob` (PSF license; see
`LICENSE-PSF-2.0.txt` and `NOTICE.md`). The matching is the vendored
`glob.translate` of `_match.py`. Two things differ from upstream, neither
in what matches:

- **They walk wrapper paths, not strings.** A directory's entries are the
  wrapper's own `iterdir()` children, so each carries what its listing said
  (its `pathinfo`), and "is this a directory" is asked of that record.
- **They do no I/O themselves.** A selector yields a request -- list this
  directory, is this entry a directory, does this path exist -- and is sent
  the answer, so one algorithm serves a synchronous `Path` (`iterate`) and
  an `AsyncPath` (`aiterate`).

A `**` lists its subtree with one recursive call where the store has one
(fsspec's `find`, cloudpathlib's recursive listing): the adapter returns a
`Tree`, and the directory listings below it are served from it.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re

import typing_extensions as tx

from ._match import _glob_translate
from ._pathinfo import LSTAT, typed_facts

_MAGIC = re.compile("([*?[])")
_SPECIAL = ("", ".", "..")

# What a selector asks for, and is sent back:
SCAN = "scan"  # (SCAN, dir) -> list of child paths, or None
TREE = "tree"  # (TREE, dir) -> nothing: a `**` is about to walk `dir`
IS_DIR = "is_dir"  # (IS_DIR, entry, follow_symlinks) -> bool
EXISTS = "exists"  # (EXISTS, path, dir_only) -> bool
FOUND = "found"  # (FOUND, path) -> a match; nothing is sent back

_Requests = tx.Generator[tx.Tuple[tx.Any, ...], tx.Any, None]


def parse_pattern(pattern: str, windows: bool) -> tx.List[str]:
    """A pattern's parts, as pathlib 3.14's `_parse_pattern` gives them.

    An anchored pattern raises `NotImplementedError`, an empty one
    `ValueError`; `.` and empty segments are dropped, and a trailing
    separator adds a final empty part.
    """
    if windows:
        drive, rest = ntpath.splitdrive(pattern)
        sep = "\\"
        rest = rest.replace("/", sep)
        anchored = bool(drive) or rest.startswith(sep)
    else:
        sep = "/"
        rest = pattern
        anchored = rest.startswith(sep)
    if anchored:
        raise NotImplementedError("Non-relative patterns are unsupported")
    parts = [part for part in rest.split(sep) if part and part != "."]
    if not parts:
        raise ValueError(f"Unacceptable pattern: {pattern!r}")
    if rest.endswith(sep):
        parts.append("")
    return parts


def pattern_text(pattern: tx.Any) -> str:
    """A `glob` pattern as a string: a `str` or any `os.PathLike`."""
    if isinstance(pattern, str):
        return pattern
    if isinstance(pattern, os.PathLike):
        text = os.fspath(pattern)
        if isinstance(text, str):
            return text
    raise TypeError(
        "a glob pattern must be a str or an os.PathLike returning str, not "
        f"{type(pattern).__name__}"
    )


def _cat(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


class _Globber:
    """pathlib 3.14's selectors, over paths, asking for I/O by yielding."""

    def __init__(
        self,
        case_sensitive: bool,
        case_pedantic: bool,
        recurse_symlinks: bool,
    ) -> None:
        self.flags = 0 if case_sensitive else re.IGNORECASE
        self.case_pedantic = case_pedantic
        self.recurse_symlinks = recurse_symlinks

    @staticmethod
    def join(path: tx.Any, segment: str) -> tx.Any:
        # Through the driver, so a store's own spelling is kept.
        return path.with_wrapped(path._wrapped / segment)

    @staticmethod
    def name(path: tx.Any) -> str:
        # The store's own name, as a listing gives it.
        return path._store_name()

    def compile(self, pattern: str) -> tx.Callable[[str], tx.Any]:
        regex = _glob_translate(
            pattern, recursive=True, include_hidden=True, seps="/"
        )
        return re.compile(regex, self.flags).match

    def selector(self, parts: tx.List[str]) -> tx.Any:
        """A function selecting from a path along `parts` (reversed)."""
        if not parts:
            return self.select_exists
        part = parts.pop()
        if part == "**":
            return self.recursive_selector(part, parts)
        if part in _SPECIAL:
            return self.special_selector(part, parts)
        if not self.case_pedantic and _MAGIC.search(part) is None:
            return self.literal_selector(part, parts)
        return self.wildcard_selector(part, parts)

    def special_selector(self, part: str, parts: tx.List[str]) -> tx.Any:
        """Selects a special child (`..`), or, for the empty part a
        trailing separator adds, the path itself if it is a directory."""
        if part == "":

            def select_dir(
                path: tx.Any, rel: str, exists: bool = False
            ) -> _Requests:
                return self.select_exists(path, rel, exists, dir_only=True)

            return select_dir
        select_next = self.selector(parts)

        def select_special(
            path: tx.Any, rel: str, exists: bool = False
        ) -> _Requests:
            return select_next(self.join(path, part), _cat(rel, part), exists)

        return select_special

    def literal_selector(self, part: str, parts: tx.List[str]) -> tx.Any:
        """Selects a literal descendant of a path."""
        # As upstream: consume the following literal parts here too.
        segments = [part]
        while parts and _MAGIC.search(parts[-1]) is None:
            segments.append(parts.pop())
        dir_only = segments[-1] == ""
        if dir_only:
            segments.pop()
        select_next = self.selector(parts)

        def select_literal(
            path: tx.Any, rel: str, exists: bool = False
        ) -> _Requests:
            for segment in segments:
                path = self.join(path, segment)
                rel = _cat(rel, segment)
            if dir_only:
                return self.select_exists(path, rel, False, dir_only=True)
            return select_next(path, rel, False)

        return select_literal

    def wildcard_selector(self, part: str, parts: tx.List[str]) -> tx.Any:
        """Selects the direct children of a path that match `part`."""
        match = None if part == "*" else self.compile(part)
        dir_only = bool(parts)
        select_next = self.selector(parts) if dir_only else None

        def select_wildcard(
            path: tx.Any, rel: str, exists: bool = False
        ) -> _Requests:
            entries = yield (SCAN, path)
            if entries is None:
                return
            for entry in entries:
                name = self.name(entry)
                if match is not None and not match(name):
                    continue
                if dir_only:
                    is_dir = yield (IS_DIR, entry, True)
                    if is_dir:
                        yield from select_next(entry, _cat(rel, name), True)
                else:
                    yield (FOUND, entry)

        return select_wildcard

    def recursive_selector(self, part: str, parts: tx.List[str]) -> tx.Any:
        """Selects a path and all its descendants that match."""
        # As upstream: consume following '**' parts, which have no effect,
        # and, when following links, the non-special parts after it, which
        # then filter the walk by one regular expression.
        while parts and parts[-1] == "**":
            parts.pop()
        follow = self.recurse_symlinks
        if follow:
            while parts and parts[-1] not in _SPECIAL:
                part += "/" + parts.pop()
        match = None if part == "**" else self.compile(part)
        dir_only = bool(parts)
        select_next = self.selector(parts)

        def select_recursive(
            path: tx.Any, rel: str, exists: bool = False
        ) -> _Requests:
            yield (TREE, path)
            if match is None or match(""):
                yield from select_next(path, rel, exists)
            stack = [(path, rel, "")]
            while stack:
                yield from select_recursive_step(stack)

        def select_recursive_step(
            stack: tx.List[tx.Tuple[tx.Any, str, str]],
        ) -> _Requests:
            path, rel, tail = stack.pop()
            entries = yield (SCAN, path)
            if entries is None:
                return
            for entry in entries:
                is_dir = yield (IS_DIR, entry, follow)
                if not is_dir and dir_only:
                    continue
                name = self.name(entry)
                entry_rel, entry_tail = _cat(rel, name), _cat(tail, name)
                if match is None or match(entry_tail):
                    if dir_only:
                        yield from select_next(entry, entry_rel, True)
                    else:
                        yield (FOUND, entry)
                if is_dir:
                    stack.append((entry, entry_rel, entry_tail))

        return select_recursive

    def select_exists(
        self, path: tx.Any, rel: str, exists: bool = False,
        dir_only: bool = False,
    ) -> _Requests:
        """Yields the path, if it exists (as a directory, for `dir_only`)."""
        if exists or (yield (EXISTS, path, dir_only)):
            yield (FOUND, path)


def select(
    root: tx.Any,
    pattern: tx.Any,
    *,
    recursive: bool,
    windows: bool,
    case_sensitive: tx.Optional[bool],
    recurse_symlinks: bool,
) -> _Requests:
    """The requests of one `glob` (`recursive`: `rglob`) from `root`.

    Raises for a bad pattern at once, as pathlib does, not when iterated.
    """
    text = pattern_text(pattern)
    if recursive:
        text = (ntpath if windows else posixpath).join("**", text)
    parts = parse_pattern(text, windows)
    if case_sensitive is None:
        case_sensitive, case_pedantic = not windows, False
    else:
        # A case rule the store may not share: list every part, as upstream.
        case_pedantic = True
    globber = _Globber(case_sensitive, case_pedantic, recurse_symlinks)
    return globber.selector(parts[::-1])(root, "", False)


# -- one recursive listing, served as directory listings ---------------------
class Tree:
    """A subtree listed in one call, answering each directory's listing.

    `key` names a path in the store; `children` maps a directory's key to
    its child paths. A directory below the base that the listing does not
    hold is empty (or not a directory); a symbolic link to one, or a path
    outside the base, is not covered and is listed the ordinary way.
    """

    __slots__ = ("_base", "_key", "_children")

    def __init__(
        self,
        base: str,
        key: tx.Callable[[tx.Any], str],
        children: tx.Dict[str, tx.List[tx.Any]],
    ) -> None:
        self._base = base
        self._key = key
        self._children = children

    def scan(self, path: tx.Any) -> tx.Optional[tx.List[tx.Any]]:
        """The children of `path`, or None when this tree does not hold it."""
        key = self._key(path)
        found = self._children.get(key)
        if found is not None:
            return list(found)
        if not (
            key == self._base or self._base == ""
            or key.startswith(self._base + "/")
        ):
            return None
        facts = path._listing_facts()
        if facts is not None and facts.get(LSTAT, "symlink") is True:
            return None
        return []


def tree_from(
    base: str,
    key: tx.Callable[[tx.Any], str],
    entries: tx.Iterable[tx.Tuple[str, str, tx.Any]],
) -> Tree:
    """A `Tree` from `(parent key, child key, child path)` triples.

    On an object store a file and a directory can share a name: they are
    one child, a directory, as a directory listing has it.
    """
    children: tx.Dict[str, tx.Dict[str, tx.Any]] = {}
    for parent, name, child in entries:
        siblings = children.setdefault(parent, {})
        if name not in siblings or not _listed_dir(siblings[name]):
            siblings[name] = child
    for siblings in children.values():
        for name, child in list(siblings.items()):
            if name in children and not _listed_dir(child):
                siblings[name] = child._listed(
                    child._wrapped, typed_facts(True)
                )
    return Tree(
        base, key,
        {parent: list(found.values()) for parent, found in children.items()},
    )


def _listed_dir(path: tx.Any) -> bool:
    facts = path._listing_facts()
    return facts is not None and facts.get(LSTAT, "dir") is True


# -- the two front-ends -------------------------------------------------------
def _from_trees(
    trees: tx.List[Tree], path: tx.Any
) -> tx.Optional[tx.List[tx.Any]]:
    for tree in trees:
        found = tree.scan(path)
        if found is not None:
            return found
    return None


def iterate(
    requests: _Requests,
    make_tree: tx.Callable[[tx.Any], tx.Optional[Tree]],
) -> tx.Iterator[tx.Any]:
    """Run `requests` on synchronous `Path` wrappers; yield the matches.

    `make_tree` lists a directory's subtree in one call, or returns None.
    """
    trees: tx.List[Tree] = []
    reply: tx.Any = None
    try:
        while True:
            try:
                request = requests.send(reply)
            except StopIteration:
                return
            kind, path = request[0], request[1]
            reply = None
            if kind == FOUND:
                yield path
            elif kind == SCAN:
                reply = _from_trees(trees, path)
                if reply is None:
                    try:
                        reply = list(path.iterdir())
                    except OSError:
                        pass
            elif kind == TREE:
                if _from_trees(trees, path) is None:
                    tree = make_tree(path)
                    if tree is not None:
                        trees.append(tree)
            elif kind == IS_DIR:
                reply = path.pathinfo.is_dir(follow_symlinks=request[2])
            else:
                info = path.pathinfo
                reply = (
                    info.is_dir() if request[2]
                    else info.exists(follow_symlinks=False)
                )
    finally:
        requests.close()


async def aiterate(
    requests: _Requests,
    make_tree: tx.Callable[[tx.Any], tx.Awaitable[tx.Optional[Tree]]],
) -> tx.AsyncIterator[tx.Any]:
    """`iterate`, on `AsyncPath` wrappers: every request is awaited."""
    trees: tx.List[Tree] = []
    reply: tx.Any = None
    try:
        while True:
            try:
                request = requests.send(reply)
            except StopIteration:
                return
            kind, path = request[0], request[1]
            reply = None
            if kind == FOUND:
                yield path
            elif kind == SCAN:
                reply = _from_trees(trees, path)
                if reply is None:
                    try:
                        reply = [child async for child in path.iterdir()]
                    except OSError:
                        pass
            elif kind == TREE:
                if _from_trees(trees, path) is None:
                    tree = await make_tree(path)
                    if tree is not None:
                        trees.append(tree)
            elif kind == IS_DIR:
                reply = await path.pathinfo.is_dir(
                    follow_symlinks=request[2]
                )
            else:
                info = path.pathinfo
                reply = await (
                    info.is_dir() if request[2]
                    else info.exists(follow_symlinks=False)
                )
    finally:
        requests.close()
