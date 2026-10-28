"""Scoped local copies of remote paths, and the stages that share them (#20).

`local_copy()` gives a local path a by-name library can open: a local path as
it is, a remote file or directory downloaded under its exact name. With
`with`, the copy goes when the block ends; taken by hand, it stays until its
`cleanup()` (or its stage closes), and garbage collection never removes it. A
stage (`Path.local_stage()`) shares one directory between the copies made
inside it, laid out as in their store, and counts holders so a path held by
two overlapping blocks is downloaded once and removed with its last holder.
"""

import asyncio
import gc
import os
import pathlib

import pytest

from bagof.paths import AsyncPath, Path, PurePath, _local_copy

upath = pytest.importorskip("upath")
from fsspec.implementations.memory import MemoryFileSystem  # noqa: E402


@pytest.fixture(autouse=True)
def _store() -> object:
    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]
    for key, data in [
        ("b/d/scan.hdr", b"hdr"),
        ("b/d/scan.img", b"img"),
        ("b/d/slide.mrxs", b"index"),
        ("b/d/slide/Data0000.dat", b"tile0"),
        ("b/d/slide/sub/Data0001.dat", b"tile1"),
    ]:
        upath.UPath("memory://" + key).write_bytes(data)
    yield
    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]


HDR = "memory://b/d/scan.hdr"
IMG = "memory://b/d/scan.img"
SLIDE = "memory://b/d/slide.mrxs"
TILES = "memory://b/d/slide"


def _stage_root(local: object) -> pathlib.Path:
    """The `bagof-stage-*` directory a copy lives in."""
    for parent in pathlib.Path(os.fspath(local)).parents:
        if parent.name.startswith("bagof-stage-"):
            return parent
    raise AssertionError(f"{local} is not in a stage")


# -- one copy, no stage ----------------------------------------------------
def test_a_local_path_is_used_as_it_is(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "scan.nii"
    f.write_bytes(b"x")
    with Path(f).local_copy() as local:
        assert local == Path(f)
    assert f.exists()  # never removed
    handle = Path(f).local_copy()
    assert os.fspath(handle) == str(f)
    handle.cleanup()
    assert f.exists()


def test_a_remote_file_keeps_its_name_and_goes_at_block_exit() -> None:
    with Path(HDR).local_copy() as local:
        assert isinstance(local, Path) and local.is_local()
        assert local.name == "scan.hdr"
        assert local.read_bytes() == b"hdr"
        where = os.fspath(local)
    assert not os.path.exists(where)
    assert not _stage_root(where).exists()  # the private stage went too


def test_a_remote_directory_is_copied_whole() -> None:
    with Path(TILES).local_copy() as local:
        assert local.name == "slide"
        assert (local / "Data0000.dat").read_bytes() == b"tile0"
        assert (local / "sub" / "Data0001.dat").read_bytes() == b"tile1"
    assert not local.exists()


def test_an_exception_in_the_block_cleans_up_and_propagates() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with Path(HDR).local_copy() as local:
            where = os.fspath(local)
            raise RuntimeError("boom")
    assert not os.path.exists(where)


def test_a_query_is_not_part_of_the_local_name() -> None:
    # memory:// keeps the "?" in its key; the local name leaves it out.
    upath.UPath("memory://b/d/v.nii?versionId=1").write_bytes(b"v")
    with Path("memory://b/d/v.nii?versionId=1").local_copy() as local:
        assert local.name == "v.nii"
        assert local.read_bytes() == b"v"


# -- the handle, taken by hand ---------------------------------------------
def test_a_handle_is_path_like_and_stays_until_cleanup() -> None:
    handle = Path(IMG).local_copy()
    where = os.fspath(handle)
    assert open(handle, "rb").read() == b"img"  # os.PathLike
    assert handle.path.name == "scan.img"
    assert os.path.exists(where)
    handle.cleanup()
    assert not os.path.exists(where)
    handle.cleanup()  # idempotent


def test_a_dropped_handle_is_not_removed_by_garbage_collection() -> None:
    # A library may still be reading the file after the handle is dropped.
    where = os.fspath(Path(IMG).local_copy())
    gc.collect()
    assert os.path.exists(where)
    import shutil

    shutil.rmtree(_stage_root(where))


def test_a_handle_in_a_stage_goes_when_the_stage_closes() -> None:
    with Path.local_stage():
        handle = Path(IMG).local_copy()
        where = os.fspath(handle)
        assert os.path.exists(where)
    assert not os.path.exists(where)


# -- stages ----------------------------------------------------------------
def test_copies_in_a_stage_keep_their_relative_positions() -> None:
    with Path.local_stage() as stage:
        with Path(SLIDE).local_copy() as slide, \
                Path(TILES).local_copy() as tiles:
            assert slide.parent == tiles.parent
            assert (slide.parent / "slide" / "Data0000.dat").exists()
            remote = PurePath(TILES) / "sub"
            assert str(remote.relative_to(PurePath(SLIDE).parent)) == str(
                (tiles / "sub").relative_to(slide.parent)
            )
            assert stage.path in slide.parents


def test_each_copy_goes_with_its_own_block() -> None:
    with Path.local_stage():
        with Path(HDR).local_copy() as hdr:
            with Path(IMG).local_copy() as img:
                pass
            assert not img.exists()  # its own block ended
            assert hdr.exists()      # still held
        assert not hdr.exists()


def test_a_copy_held_twice_is_downloaded_once_and_goes_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    real = _local_copy._download

    def counted(wrapper: object, local: str) -> None:
        calls.append(local)
        real(wrapper, local)

    monkeypatch.setattr(_local_copy, "_download", counted)
    with Path.local_stage():
        first = Path(HDR).local_copy()
        with Path(HDR).local_copy() as again:
            assert os.fspath(again) == os.fspath(first)
        assert os.path.exists(first)  # the first holder still has it
        first.cleanup()
        assert not os.path.exists(first)
    assert len(calls) == 1


def test_a_removed_copy_takes_its_empty_directories_with_it() -> None:
    with Path.local_stage() as stage:
        with Path(HDR).local_copy():
            pass
        assert list(pathlib.Path(os.fspath(stage.path)).iterdir()) == []


def test_the_stage_removes_its_leftovers_when_it_closes() -> None:
    with Path.local_stage() as stage:
        Path(HDR).local_copy()  # taken by hand, never cleaned up
        root = os.fspath(stage.path)
    assert not os.path.exists(root)
    assert stage.closed


def test_an_explicit_stage_wins_and_the_innermost_is_active() -> None:
    other = Path.local_stage()
    with Path.local_stage() as outer:
        with Path.local_stage() as inner:
            with Path(HDR).local_copy() as local:
                assert inner.path in local.parents
            with Path(HDR).local_copy(stage=other) as local:
                assert other.path in local.parents
        with Path(HDR).local_copy() as local:
            assert outer.path in local.parents
    other.cleanup()
    assert not other.path.exists()


def test_a_cleaned_up_stage_refuses_new_copies() -> None:
    stage = Path.local_stage()
    stage.cleanup()
    with pytest.raises(ValueError, match="cleaned up"):
        Path(HDR).local_copy(stage=stage)


def test_two_versions_cannot_share_one_slot() -> None:
    for version in ("1", "2"):
        key = "memory://b/d/v.nii?versionId=" + version
        upath.UPath(key).write_bytes(b"v")
    with Path.local_stage():
        with Path("memory://b/d/v.nii?versionId=1").local_copy():
            with pytest.raises(ValueError, match="already holds"):
                Path("memory://b/d/v.nii?versionId=2").local_copy()


def test_an_unused_stage_is_removed_by_garbage_collection() -> None:
    stage = Path.local_stage()
    root = os.fspath(stage.path)
    del stage
    gc.collect()
    assert not os.path.exists(root)


def test_a_failed_download_leaves_nothing_to_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _local_copy._download_file

    def broken(wrapper: object, local: str) -> None:
        with open(local, "wb") as handle:
            handle.write(b"ha")  # a half file
        raise OSError("connection lost")

    with Path.local_stage() as stage:
        monkeypatch.setattr(_local_copy, "_download_file", broken)
        with pytest.raises(OSError, match="connection lost"):
            Path(HDR).local_copy()
        assert list(pathlib.Path(os.fspath(stage.path)).rglob("*")) == []
        monkeypatch.setattr(_local_copy, "_download_file", real)
        with Path(HDR).local_copy() as local:
            assert local.read_bytes() == b"hdr"


def test_supports_answers_for_the_new_members() -> None:
    assert Path(HDR).supports("local_copy")
    assert AsyncPath(HDR).supports("local_copy")
    assert Path(HDR).supports("local_stage")
    assert not PurePath(HDR).supports("local_copy")  # no I/O on PurePath
    assert "local_copy" in Path(HDR).capabilities()


# -- AsyncPath ---------------------------------------------------------------
def test_async_with_downloads_and_removes() -> None:
    async def go() -> str:
        async with AsyncPath(IMG).local_copy() as local:
            assert isinstance(local, AsyncPath)
            assert local.name == "scan.img"
            assert await local.read_bytes() == b"img"
            return os.fspath(local)

    assert not os.path.exists(asyncio.run(go()))


def test_await_gives_a_handle_that_stays_until_cleanup() -> None:
    async def go() -> None:
        handle = await AsyncPath(IMG).local_copy()
        where = os.fspath(handle)
        assert isinstance(handle.path, AsyncPath)
        assert os.path.exists(where)
        await handle.cleanup()
        assert not os.path.exists(where)

    asyncio.run(go())


def test_an_unfetched_async_handle_has_no_path() -> None:
    handle = AsyncPath(IMG).local_copy()
    with pytest.raises(ValueError, match="await it first"):
        os.fspath(handle)


def test_the_active_stage_reaches_the_worker_thread() -> None:
    # AsyncPath runs a synchronous driver's copy in a worker thread, which
    # only sees the caller's active stage if the bridge carries the context.
    async def go() -> None:
        async with Path.local_stage() as stage:
            async with AsyncPath(IMG).local_copy() as local:
                assert stage.path in Path(os.fspath(local)).parents

    asyncio.run(go())


def test_concurrent_tasks_each_use_their_own_stage() -> None:
    async def one(path: str) -> bool:
        async with AsyncPath.local_stage() as stage:
            await asyncio.sleep(0)
            async with AsyncPath(path).local_copy() as local:
                await asyncio.sleep(0)
                return stage.path in Path(os.fspath(local)).parents

    async def go() -> list:
        return await asyncio.gather(one(HDR), one(IMG), one(SLIDE))

    assert asyncio.run(go()) == [True, True, True]


def test_a_natively_async_driver_downloads_on_the_loop() -> None:
    fsspec = pytest.importorskip("fsspec")
    wrapper_mod = pytest.importorskip("fsspec.implementations.asyn_wrapper")

    class _MemAsyncLC(wrapper_mod.AsyncFileSystemWrapper):
        protocol = "memasynclc"
        async_impl = True

        def __init__(self, *args: object, **kwargs: object) -> None:
            for drop in ("asynchronous", "skip_instance_cache"):
                kwargs.pop(drop, None)
            super().__init__(MemoryFileSystem(), asynchronous=True)

        @classmethod
        def _strip_protocol(cls, path: str) -> str:
            text = path.replace("memasynclc://", "memory://")
            return MemoryFileSystem._strip_protocol(text)

    fsspec.register_implementation("memasynclc", _MemAsyncLC, clobber=True)
    from bagof.paths._async_fsspec import AsyncFSPath

    async def go() -> None:
        remote = AsyncPath("memasynclc://b/d/slide")
        assert isinstance(remote.wrapped, AsyncFSPath)
        async with AsyncPath.local_stage() as stage:
            async with remote.local_copy() as local:
                assert stage.path in Path(os.fspath(local)).parents
                data = await (local / "sub" / "Data0001.dat").read_bytes()
                assert data == b"tile1"
            assert not await local.exists()

    asyncio.run(go())
