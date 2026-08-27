"""The synchronous pure-path surface over stdlib pathlib."""

import os

import pytest

from bagof.paths import Path, UnsupportedPathOperation


def test_construct_from_str_and_pathlib() -> None:
    from pathlib import Path as LocalPath

    assert str(Path("/a/b/c.txt")) == "/a/b/c.txt"
    assert str(Path(LocalPath("/a/b/c.txt"))) == "/a/b/c.txt"
    # wrapping a wrapper unwraps it rather than nesting
    assert Path(Path("/a/b")) == Path("/a/b")


def test_reject_non_path_input() -> None:
    with pytest.raises(TypeError):
        Path(42)


def test_pure_path_properties() -> None:
    p = Path("/a/b/c.tar.gz")
    assert p.name == "c.tar.gz"
    assert p.stem == "c.tar"
    assert p.suffix == ".gz"
    assert p.suffixes == [".tar", ".gz"]
    assert p.parts == ("/", "a", "b", "c.tar.gz")


def test_parent_and_parents_are_wrapped() -> None:
    p = Path("/a/b/c.txt")
    assert p.parent == Path("/a/b")
    assert isinstance(p.parent, Path)
    assert tuple(p.parents) == (Path("/a/b"), Path("/a"), Path("/"))
    assert all(isinstance(q, Path) for q in p.parents)


def test_pure_path_methods_rewrap() -> None:
    p = Path("/a/b/c.txt")
    assert p.joinpath("d", "e") == Path("/a/b/c.txt/d/e")
    assert p.with_name("z.bin") == Path("/a/b/z.bin")
    assert p.with_stem("z") == Path("/a/b/z.txt")
    assert p.with_suffix(".bin") == Path("/a/b/c.bin")
    assert isinstance(p.joinpath("d"), Path)


def test_truediv() -> None:
    assert Path("/a") / "b" / "c" == Path("/a/b/c")
    assert "/a" / Path("b") == Path("/a/b")
    # a wrapper on the right is unwrapped
    assert Path("/a") / Path("b") == Path("/a/b")


def test_string_methods() -> None:
    p = Path("/a/b")
    assert p.as_posix() == "/a/b"
    assert p.as_uri() == "file:///a/b"
    assert p.is_absolute() is True
    assert Path("a/b").is_absolute() is False


def test_relative_to_unwraps_wrapper_argument() -> None:
    p = Path("/a/b/c")
    assert p.relative_to(Path("/a")) == Path("b/c")
    assert p.is_relative_to(Path("/a")) is True
    assert p.is_relative_to(Path("/x")) is False


def test_match_computed() -> None:
    assert Path("/a/b/c.txt").match("*.txt") is True
    assert Path("/a/b/c.txt").match("*.bin") is False
    assert Path("/a/b/c.txt").match("b/*.txt") is True  # anchored from right


def test_fspath_local_returns_str() -> None:
    p = Path("/a/b/c.txt")
    assert os.fspath(p) == "/a/b/c.txt"
    # usable anywhere os.fspath is accepted
    assert os.path.basename(p) == "c.txt"


def test_fspath_raises_for_non_local() -> None:
    p = Path(_FakeCloud("bucket/key"))
    assert p.protocol == "s3"
    with pytest.raises(UnsupportedPathOperation):
        os.fspath(p)


def test_supports() -> None:
    p = Path("/a/b")
    assert p.supports("name") is True
    assert p.supports("joinpath") is True
    # base location members resolve as attributes
    assert p.supports("protocol") is True
    # underscore / internal names are not "supported" operations
    assert p.supports("_key") is False
    assert p.supports("__init__") is False


def test_full_match_computed_regardless_of_driver() -> None:
    # full_match is computed lexically on the canonical path, so it works even
    # on a driver that implements no full_match of its own.
    p = Path(_NoFullMatch())  # str() == "/a/b"
    assert p.supports("full_match") is True
    assert p.full_match("/a/b") is True
    assert p.full_match("/a/*") is True
    assert p.full_match("/x/*") is False
    assert p.full_match("**/b") is True


def test_url_string_needs_a_driver() -> None:
    with pytest.raises(ValueError):
        Path("s3://bucket/key")


def test_driver_kwarg_not_yet_supported() -> None:
    with pytest.raises(NotImplementedError):
        Path("/a", driver="upath")


def test_wrap_pathlike_without_fspath() -> None:
    # A non-local UPath has no __fspath__; the constructor must still accept
    # it (gate on path-shaped, not on __fspath__).
    p = Path(_PartsOnly("/a/b"))
    assert str(p) == "/a/b"


def test_fspath_raises_for_scheme_string_without_protocol_attr() -> None:
    # The trap: a driver with a scheme-ful str and NO .protocol attribute
    # must be classified non-local (parsed from the string) so os.fspath
    # raises rather than silently delegating (which, for a cloud driver,
    # would download the file).
    p = Path(_SchemeStr("s3://bucket/key"))
    assert p.protocol == "s3"
    with pytest.raises(UnsupportedPathOperation):
        os.fspath(p)


class _FakeCloud(os.PathLike):
    """A minimal non-local path-like object for the fspath test."""

    protocol = "s3"

    def __init__(self, key: str) -> None:
        self._key = key

    def __fspath__(self) -> str:
        return self._key

    def __str__(self) -> str:
        return "s3://" + self._key


class _SchemeStr(os.PathLike):
    """A driver with a scheme-ful str and NO protocol attribute."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __fspath__(self) -> str:  # would "work" if wrongly treated as local
        return self._text

    def __str__(self) -> str:
        return self._text


class _PartsOnly:
    """A path-shaped object with no __fspath__ (like a non-local UPath)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text

    @property
    def parts(self) -> tuple:
        return tuple(self._text.strip("/").split("/"))


class _NoFullMatch(os.PathLike):
    """A driver that does not implement full_match."""

    def __fspath__(self) -> str:
        return "/a/b"

    def __str__(self) -> str:
        return "/a/b"
