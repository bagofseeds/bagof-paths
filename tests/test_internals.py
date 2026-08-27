"""Defensive branches: location attributes, missing members, repr, matcher
edge cases -- kept covered so the wrapper's degraded paths do not rot.
"""

import os
import pathlib

import pytest

from bagof.paths import Path, UnsupportedPathOperation


class _RichDriver(os.PathLike):
    """A driver exposing string protocol/path/drive/root/anchor attributes."""

    protocol = "s3"
    path = "bucket/key"
    drive = "bucket"
    root = "/"
    anchor = "bucket/"

    def __fspath__(self) -> str:
        return "s3://bucket/key"

    def __str__(self) -> str:
        return "s3://bucket/key"


def test_location_string_attributes_are_trusted() -> None:
    p = Path(_RichDriver())
    assert p.protocol == "s3"
    assert p.path == "bucket/key"
    assert p.drive == "bucket"
    assert p.root == "/"
    assert p.anchor == "bucket/"


def test_wrapped_and_repr() -> None:
    driver = pathlib.Path("/a/b")
    p = Path(driver)
    assert p.wrapped is driver
    assert repr(p) == "Path('/a/b')"


def test_supports_computed_and_capabilities() -> None:
    p = Path("/a/b")
    assert p.supports("match") is True
    assert p.supports("full_match") is True
    assert p.supports("name") is True
    caps = p.capabilities()
    assert "name" in caps
    assert "match" in caps


class _NoName(os.PathLike):
    """A path-shaped driver missing the `name` property."""

    def __fspath__(self) -> str:
        return "/x"

    def __str__(self) -> str:
        return "/x"


def test_missing_property_raises() -> None:
    p = Path(_NoName())
    with pytest.raises(UnsupportedPathOperation):
        _ = p.name


class _NoAsUri(os.PathLike):
    """A driver with `name` but no `as_uri` method and no fallback."""

    def __fspath__(self) -> str:
        return "/x"

    def __str__(self) -> str:
        return "/x"

    @property
    def name(self) -> str:
        return "x"


def test_missing_method_raises() -> None:
    p = Path(_NoAsUri())
    with pytest.raises(UnsupportedPathOperation):
        p.as_uri()


def test_full_match_exotic_character_classes() -> None:
    # Exercise the vendored fnmatch translation's class branches (ranges,
    # negation, empty and invalid ranges, escapes); assert only that they
    # translate to a working matcher, not their exact glob semantics.
    for pattern in (
        "/[a-c]/b",
        "/[!a-c]/b",
        "/[!]/b",
        "/[]/b",
        "/[[]/b",
        "/[z-a]/b",
        "/[&~|]/b",
    ):
        assert isinstance(Path("/a/b").full_match(pattern), bool)
