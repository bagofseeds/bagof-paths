"""Identity: equality, hashing, and driver-independence."""

import os
import pathlib

from bagof.paths import Path


def test_equality_and_hash() -> None:
    assert Path("/a/b") == Path("/a/b")
    assert Path("/a/b") != Path("/a/c")
    assert hash(Path("/a/b")) == hash(Path("/a/b"))


def test_usable_as_dict_key_and_set_member() -> None:
    table = {Path("/a/b"): 1}
    assert table[Path("/a/b")] == 1
    assert Path("/a/b") in {Path("/a/b"), Path("/a/c")}


def test_equality_is_driver_independent_across_subclasses() -> None:
    # Two wrappers pointing at the same location are equal regardless of the
    # wrapper subclass -- mirroring pathlib, where PurePosixPath == PosixPath.
    class Other(Path):
        pass

    assert Path("/a/b") == Other("/a/b")
    assert hash(Path("/a/b")) == hash(Other("/a/b"))


def test_not_equal_to_foreign_types() -> None:
    assert (Path("/a/b") == "/a/b") is False
    assert Path("/a/b") != object()


def test_equality_across_stdlib_driver_types() -> None:
    # Different driver object types, same location -> equal.
    assert Path(pathlib.Path("/a/b")) == Path(pathlib.PurePosixPath("/a/b"))


class _ProtocolDriver(os.PathLike):
    """A driver exposing protocol/path attributes, like UPath."""

    def __init__(self, protocol: str, path: str, text: str) -> None:
        self.protocol = protocol
        self.path = path
        self._text = text

    def __fspath__(self) -> str:
        return self._text

    def __str__(self) -> str:
        return self._text


class _SchemeStrDriver:
    """A driver whose only clue to its location is a scheme-ful str()."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text

    @property
    def parts(self) -> tuple:
        return tuple(self._text.split("/"))


def test_equality_is_driver_independent_across_driver_types() -> None:
    # Same s3 location seen two ways: one driver reports protocol/path
    # attributes, the other only a scheme-ful string. Both must be equal.
    via_attrs = Path(_ProtocolDriver("s3", "bucket/key", "s3://bucket/key"))
    via_str = Path(_SchemeStrDriver("s3://bucket/key"))
    assert via_attrs == via_str
    assert hash(via_attrs) == hash(via_str)


def test_cross_family_inequality() -> None:
    # A future AsyncPath sets _family = "async"; it must never equal a Path.
    class AsyncLike(Path):
        _family = "async"

    assert (Path("/a/b") == AsyncLike("/a/b")) is False
    assert Path("/a/b") != AsyncLike("/a/b")
