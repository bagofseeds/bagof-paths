"""Glob matching, validated against stdlib pathlib where it exists."""

import re
import sys
from pathlib import PurePosixPath

import pytest

from bagof.paths import Path, _match


def test_full_match_basic() -> None:
    p = Path("/a/b/c.txt")
    assert p.full_match("/a/b/c.txt") is True
    assert p.full_match("/a/*/c.txt") is True
    assert p.full_match("/a/**/c.txt") is True
    assert p.full_match("/a/**") is True
    assert p.full_match("**/c.txt") is True
    # full_match is whole-path, not right-anchored
    assert p.full_match("*.txt") is False


def test_full_match_case_sensitivity() -> None:
    p = Path("/A/B/C.TXT")
    assert p.full_match("/a/b/c.txt") is False
    assert p.full_match("/a/b/c.txt", case_sensitive=False) is True


_CASES = [
    ("a/b/c.txt", "a/b/c.txt"),
    ("a/b/c.txt", "a/*/c.txt"),
    ("a/b/c.txt", "**/c.txt"),
    ("a/b/c.txt", "a/**"),
    ("a/.h/c", "a/**/c"),
    ("a/b", "a/b/"),
    ("bucket/key/x", "bucket/**/x"),
    ("/a/b/c", "/a/**"),
    ("/a/b/c", "/a/*/c"),
    ("/a/b/c", "/x/**"),
]


@pytest.mark.skipif(
    sys.version_info < (3, 13), reason="pathlib.full_match is 3.13+"
)
@pytest.mark.parametrize("path,pattern", _CASES)
def test_full_match_equivalent_to_pathlib(path: str, pattern: str) -> None:
    assert _match.full_match(path, pattern) == PurePosixPath(
        path
    ).full_match(pattern)


def test_match_is_right_anchored() -> None:
    p = Path("/a/b/c.txt")
    assert p.match("*.txt") is True
    assert p.match("b/*.txt") is True
    assert p.match("a/*.txt") is False


def test_match_case_insensitive() -> None:
    assert Path("/a/b/C.TXT").match("*.txt", case_sensitive=False) is True
    assert Path("/a/b/C.TXT").match("*.txt", case_sensitive=True) is False


def test_full_match_character_classes_and_wildcards() -> None:
    # Exercise the vendored fnmatch translation (classes, ranges, negation,
    # single-char) on every interpreter, not only the 3.13 oracle leg.
    assert Path("/a/b.txt").full_match("/[ab]/b.txt") is True
    assert Path("/c/b.txt").full_match("/[ab]/b.txt") is False
    assert Path("/a/b.txt").full_match("/[!x]/b.txt") is True
    assert Path("/a/b.txt").full_match("/[a-c]/b.txt") is True
    assert Path("/a/b.txt").full_match("/?/b.txt") is True
    assert Path("/ab/b.txt").full_match("/?/b.txt") is False


def test_glob_translate_hidden_handling() -> None:
    # Directly cover the vendored translator's include_hidden branch: a bare
    # '*' segment must not match a leading-dot component.
    regex = _match._glob_translate(
        "a/*.txt", recursive=True, include_hidden=False, seps="/"
    )
    assert re.match(regex, "a/b.txt") is not None
    assert re.match(regex, "a/.hidden.txt") is None
