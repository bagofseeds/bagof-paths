"""`is_local()`: whether a path names a file on the local file system (#18).

It is lexical -- it reads only the path, never storage -- and it is the same
test `os.fspath` makes, so the two never disagree: a path is local exactly
when `os.fspath` gives its file name.
"""

import asyncio
import os
import pathlib

import pytest

from bagof.paths import (
    AsyncPath,
    NoLocalPathError,
    Path,
    PurePath,
    UnsupportedPathOperation,
)

FRONTS = [PurePath, Path, AsyncPath]


@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "text", ["/tmp/x", "rel/x", "file:///tmp/x", "local:///tmp/x"]
)
def test_local_paths(cls: type, text: str) -> None:
    assert cls(text).is_local() is True


@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "text", ["s3://bucket/x", "gs://bucket/x", "https://host/x"]
)
def test_remote_paths(cls: type, text: str) -> None:
    assert cls(text).is_local() is False


@pytest.mark.parametrize("cls", [Path, AsyncPath])
def test_memory_is_not_local(cls: type) -> None:
    pytest.importorskip("upath")
    assert cls("memory://bucket/x").is_local() is False


def test_fsspec_chain_is_not_local() -> None:
    # A caching chain can produce a local copy, but the path itself is not a
    # local file name.
    pytest.importorskip("upath")
    p = Path("simplecache::s3://bucket/x", storage_options={})
    assert p.is_local() is False


def test_wrapped_upath_file_path_is_local(tmp_path: pathlib.Path) -> None:
    upath = pytest.importorskip("upath")
    driver = upath.UPath((tmp_path / "x").as_uri())
    assert driver.protocol == "file"
    p = Path(driver)
    assert p.is_local() is True
    assert os.fspath(p) == str(tmp_path / "x")


def test_async_path_over_a_local_driver(tmp_path: pathlib.Path) -> None:
    anyio = pytest.importorskip("anyio")
    p = AsyncPath(anyio.Path(tmp_path / "x"))
    assert p.is_local() is True


def test_is_local_reads_no_storage(tmp_path: pathlib.Path) -> None:
    # A file that does not exist is still a local path.
    missing = Path(tmp_path / "does" / "not" / "exist")
    assert missing.is_local() is True
    assert not missing.exists()


@pytest.mark.parametrize(
    "text", ["/tmp/x", "file:///tmp/x", "local:///tmp/x", "s3://bucket/x"]
)
def test_agrees_with_fspath(text: str) -> None:
    p = PurePath(text)
    if p.is_local():
        assert os.fspath(p) == str(p)
    else:
        with pytest.raises(UnsupportedPathOperation):
            os.fspath(p)


def test_is_local_is_listed_as_supported() -> None:
    for p in (PurePath("/tmp/x"), PurePath("s3://bucket/x")):
        assert p.supports("is_local")
        assert "is_local" in p.capabilities()


def test_is_local_is_synchronous_on_async_path() -> None:
    result = AsyncPath("/tmp/x").is_local()
    assert result is True
    assert not asyncio.iscoroutine(result)


# -- os.fspath on a remote path raises NoLocalPathError, a TypeError (#23) --


@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize("text", ["s3://bucket/x.nii", "https://host/x.nii"])
def test_fspath_on_remote_raises_no_local_path_error(
    cls: type, text: str
) -> None:
    p = cls(text)
    with pytest.raises(NoLocalPathError) as info:
        os.fspath(p)
    assert isinstance(info.value, TypeError)
    assert isinstance(info.value, UnsupportedPathOperation)
    assert isinstance(info.value, NotImplementedError)
    assert "'__fspath__' is not supported" in str(info.value)


@pytest.mark.parametrize("cls", [Path, AsyncPath])
def test_fspath_probe_on_memory_path(cls: type) -> None:
    # The issue's case: the usual probe falls through to the remote branch.
    pytest.importorskip("upath")
    p = cls("memory://bucket/x.nii")
    try:
        name = os.fspath(p)
    except TypeError:
        name = None
    assert name is None


@pytest.mark.parametrize("cls", FRONTS)
def test_fspath_probe_on_local_path(cls: type, tmp_path: pathlib.Path) -> None:
    p = cls(tmp_path / "x.nii")
    try:
        name = os.fspath(p)
    except TypeError:  # pragma: no cover - a local path has a file name
        name = None
    assert name == str(tmp_path / "x.nii")


def test_no_local_path_error_is_public() -> None:
    import bagof.paths

    assert "NoLocalPathError" in bagof.paths.__all__
    assert bagof.paths.NoLocalPathError is NoLocalPathError
