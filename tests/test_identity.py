"""Identity: equality, hashing, and driver-independence."""

import os
import pathlib
import sys

import pytest

from bagof.paths import AsyncPath, Path, PurePath


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


def test_a_front_end_subclass_still_compares_by_location() -> None:
    # The front-end family is not part of identity (issue #22).
    class AsyncLike(Path):
        _family = "async"

    assert Path("/a/b") == AsyncLike("/a/b")


# -- comparing with unwrapped paths (issue #22) ------------------------------
def test_equals_the_pathlib_path_it_wraps_both_ways() -> None:
    # brainhops: a function returned a wrapper, its caller compared it with
    # the pathlib path it passed in, and got a silent False.
    given = pathlib.Path("/data/scan.nii")
    result = Path(given)
    assert result == given
    assert given == result
    assert not (result != given)
    assert not (given != result)
    assert result != pathlib.Path("/data/other.nii")
    assert pathlib.Path("/data/other.nii") != result


def test_hashes_as_pathlib_so_sets_and_dicts_mix() -> None:
    p = "/data/scan.nii"
    assert hash(Path(p)) == hash(pathlib.Path(p))
    assert hash(Path(p)) == hash(pathlib.PurePosixPath(p))
    assert pathlib.Path(p) in {Path(p)}
    assert Path(p) in {pathlib.Path(p)}
    assert {pathlib.Path(p): 1}[Path(p)] == 1
    assert {Path(p): 1}[pathlib.Path(p)] == 1
    assert len({Path(p), pathlib.Path(p), PurePath(p), AsyncPath(p)}) == 1


def test_relative_path_compares_with_pathlib() -> None:
    assert Path("a/b") == pathlib.PurePath("a/b")
    assert hash(Path("a/b")) == hash(pathlib.PurePath("a/b"))


def test_pure_full_and_async_paths_are_equal_and_hash_alike() -> None:
    paths = [PurePath("/a/b"), Path("/a/b"), AsyncPath("/a/b")]
    for one in paths:
        for other in paths:
            assert one == other
            assert hash(one) == hash(other)
    assert PurePath("/a/b") != AsyncPath("/a/c")


@pytest.mark.parametrize("url", ["s3://bkt/k", "memory://b/k"])
def test_pure_full_and_async_remote_paths_are_equal(url: str) -> None:
    paths = [PurePath(url), Path(url), AsyncPath(url)]
    for one in paths:
        for other in paths:
            assert one == other
            assert hash(one) == hash(other)


def test_a_string_never_equals_a_path() -> None:
    for wrapper in (Path("/a/b"), PurePath("/a/b"), AsyncPath("/a/b")):
        assert (wrapper == "/a/b") is False
        assert ("/a/b" == wrapper) is False
        assert (wrapper == b"/a/b") is False
    assert (Path("s3://b/k") == "s3://b/k") is False


class _Fspath:
    """A minimal os.PathLike: a name and nothing else."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __fspath__(self) -> str:
        return self.text


def test_equals_a_plain_os_pathlike_by_its_file_name() -> None:
    assert Path("/a/b") == _Fspath("/a/b")
    assert _Fspath("/a/b") == Path("/a/b")
    assert Path("/a/b") != _Fspath("/a/c")
    # Its file name is local, so it never names a remote object.
    assert Path("s3://a/b") != _Fspath("s3://a/b")


def test_other_types_are_not_comparable() -> None:
    assert Path("/a/b").__eq__(object()) is NotImplemented
    assert Path("/a/b").__eq__(None) is NotImplemented
    assert Path("/a/b").__eq__("/a/b") is NotImplemented
    assert Path("/a/b") != 1


class _BadFspath:
    """An os.PathLike whose __fspath__ fails or returns no text."""

    def __init__(self, result: object) -> None:
        self._result = result

    def __fspath__(self) -> object:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.parametrize(
    "result", [TypeError("no name"), b"/a/b"], ids=["raises", "bytes"]
)
def test_a_path_like_without_a_text_name_is_not_comparable(
    result: object,
) -> None:
    other = _BadFspath(result)
    assert Path("/a/b").__eq__(other) is NotImplemented
    assert Path("/a/b") != other


class _Unwrappable:
    """Looks like a driver (it has ``parts``) but has no readable location."""

    parts = ("a", "b")

    def __str__(self) -> str:
        raise ValueError("no location")


def test_a_driver_lookalike_that_cannot_be_wrapped_is_not_comparable() -> None:
    assert Path("/a/b").__eq__(_Unwrappable()) is NotImplemented


def test_a_remote_path_never_equals_a_local_one() -> None:
    assert Path("s3://b/k") != pathlib.PurePosixPath("s3://b/k")
    assert Path("s3://b/k") != pathlib.PurePosixPath("b/k")


def test_remote_hash_is_unchanged() -> None:
    assert hash(Path("s3://bkt/k")) == hash(("s3", "bkt/k"))
    assert hash(PurePath("s3a://bkt/k")) == hash(("s3", "bkt/k"))


def test_windows_flavour_hashes_as_pathlib() -> None:
    w = pathlib.PureWindowsPath("C:/Data/Scan.nii")
    assert PurePath(w) == w
    assert w == PurePath(w)
    assert hash(PurePath(w)) == hash(w)
    assert PurePath(w) in {w}
    assert w in {PurePath(w)}


def test_windows_and_posix_flavours_never_equal() -> None:
    # As in pathlib: the same string is a different path on another flavour.
    assert pathlib.PureWindowsPath("A") != pathlib.PurePosixPath("A")
    assert PurePath(pathlib.PureWindowsPath("A")) != PurePath(
        pathlib.PurePosixPath("A")
    )
    assert PurePath(pathlib.PureWindowsPath("A")) != pathlib.PurePosixPath(
        "A"
    )


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="duck-typed path arguments: 3.12+"
)
def test_pathlib_is_relative_to_a_wrapper() -> None:
    assert pathlib.Path("/a/b/c").is_relative_to(Path("/a"))
    assert not pathlib.Path("/a/b/c").is_relative_to(Path("/x"))


def test_pathlib_relative_to_a_wrapper() -> None:
    assert pathlib.Path("/a/b/c").relative_to(Path("/a")) == pathlib.Path(
        "b/c"
    )


# -- the backends -----------------------------------------------------------
def test_anyio_path() -> None:
    anyio = pytest.importorskip("anyio")
    p = "/a/b"
    for wrapper in (Path(p), PurePath(p), AsyncPath(p)):
        assert wrapper == anyio.Path(p)
        assert anyio.Path(p) == wrapper
        assert hash(wrapper) == hash(anyio.Path(p))
    assert AsyncPath(anyio.Path(p)) == pathlib.Path(p)
    assert AsyncPath(anyio.Path(p)) == Path(p)
    assert AsyncPath(p) != anyio.Path("/a/c")


def test_upath_local() -> None:
    UPath = pytest.importorskip("upath").UPath
    p = "/a/b"
    assert Path(p) == UPath(p)
    assert UPath(p) == Path(p)
    assert hash(Path(p)) == hash(UPath(p))
    assert UPath(p) in {Path(p)}


@pytest.mark.parametrize("url", ["s3://bkt/k", "memory://b/k", "gs://b/x"])
def test_upath_remote(url: str) -> None:
    UPath = pytest.importorskip("upath").UPath
    for wrapper in (Path(url), PurePath(url), AsyncPath(url)):
        assert wrapper == UPath(url)
        assert UPath(url) == wrapper
        assert hash(wrapper) == hash(UPath(url))
        assert UPath(url) in {wrapper}
    assert Path(url) != UPath(url + "x")


def test_upath_alias_is_equal_but_hashes_apart() -> None:
    # Documented: universal-pathlib hashes the scheme as spelled.
    UPath = pytest.importorskip("upath").UPath
    alias = UPath("s3a://bkt/k")
    assert Path("s3://bkt/k") == alias
    assert alias == Path("s3://bkt/k")
    assert hash(Path("s3://bkt/k")) != hash(alias)


def test_equality_ignores_storage_options() -> None:
    UPath = pytest.importorskip("upath").UPath
    a = UPath("memory://b/k", anon=True)
    b = UPath("memory://b/k", anon=False)
    assert a != b  # universal-pathlib compares the options too
    assert Path(a) == b
    assert b == Path(a)
    assert Path(a) == Path(b)


def test_cloudpathlib_equal_one_way() -> None:
    # cloudpathlib answers False (not "unknown") for a foreign type, so
    # only the wrapper's side sees the match; the hashes differ.
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    cloud = local.LocalS3Path("s3://bkt/k", client=client)
    assert Path("s3://bkt/k") == cloud
    assert Path(cloud) == Path("s3://bkt/k")
    assert Path("s3://bkt/other") != cloud
