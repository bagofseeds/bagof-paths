"""Staged writes: `staged()` and `atomic=` (issue #19).

A staging area is a hidden directory beside the target with an entry named
like it. Whatever the caller creates there -- a file or a directory -- is
moved onto the target on commit; an error, a discard, or staging nothing
leaves the target as it was. A remote target is staged locally and uploaded.
"""

import asyncio
import errno
import gc
import os
import pathlib
import stat
import sys

import pytest
import typing_extensions as tx

import bagof.paths._staged as staged_module
from bagof.paths import (
    AsyncPath,
    AsyncStage,
    CommitError,
    Path,
    PurePath,
    Stage,
)

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX semantics")


def _run(coro: tx.Any) -> tx.Any:
    return asyncio.run(coro)


def _listing(directory: pathlib.Path) -> tx.List[str]:
    return sorted(p.name for p in directory.iterdir())


# -- the staged path ---------------------------------------------------------
def test_staged_path_is_named_like_the_target_and_absent(
    tmp_path: pathlib.Path,
) -> None:
    target = Path(tmp_path / "scan.nii.gz")
    stage = target.staged()
    assert isinstance(stage, Stage)
    assert stage.path.name == "scan.nii.gz"
    assert stage.path.suffixes == [".nii", ".gz"]
    assert not stage.path.exists()
    assert isinstance(stage.path, Path)
    # A hidden directory beside the target, on the same file system.
    staging = pathlib.Path(str(stage.path)).parent
    assert staging.parent == tmp_path
    assert staging.name.startswith(".scan.nii.gz.staging-")
    stage.discard()
    assert not staging.exists()


def test_staging_a_path_with_no_name_is_refused() -> None:
    with pytest.raises(ValueError, match="names no file"):
        Path("/").staged()


# -- local files --------------------------------------------------------------
def test_new_file(tmp_path: pathlib.Path) -> None:
    target = Path(tmp_path / "new.txt")
    with target.staged() as tmp:
        tmp.write_text("hello")
        assert not target.exists()
    assert target.read_text() == "hello"
    assert _listing(tmp_path) == ["new.txt"]


def test_replace_existing_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.txt").write_text("old")
    target = Path(tmp_path / "x.txt")
    with target.staged() as tmp:
        tmp.write_text("new")
        assert target.read_text() == "old"  # untouched until commit
    assert target.read_text() == "new"
    assert _listing(tmp_path) == ["x.txt"]


@POSIX
def test_replacing_keeps_the_old_permissions(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.txt").write_text("old")
    os.chmod(str(tmp_path / "x.txt"), 0o640)
    with Path(tmp_path / "x.txt").staged() as tmp:
        tmp.write_text("new")
    assert stat.S_IMODE(os.stat(str(tmp_path / "x.txt")).st_mode) == 0o640


@POSIX
def test_a_symbolic_link_is_followed(tmp_path: pathlib.Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "data.txt").write_text("old")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real" / "data.txt")
    with Path(tmp_path / "link.txt").staged() as tmp:
        assert pathlib.Path(str(tmp)).parent.parent == tmp_path / "real"
        tmp.write_text("new")
    assert (tmp_path / "link.txt").is_symlink()
    assert (tmp_path / "real" / "data.txt").read_text() == "new"


@POSIX
def test_a_reader_with_the_old_file_open_keeps_reading_it(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "x.bin").write_bytes(b"AAAA")
    with open(str(tmp_path / "x.bin"), "rb") as reader:
        Path(tmp_path / "x.bin").write_bytes(b"BBBB", atomic=True)
        assert reader.read() == b"AAAA"
    assert (tmp_path / "x.bin").read_bytes() == b"BBBB"


# -- local directories and type changes ---------------------------------------
def test_new_directory(tmp_path: pathlib.Path) -> None:
    target = Path(tmp_path / "store.zarr")
    with target.staged() as tmp:
        tmp.mkdir()
        (tmp / "a").write_text("1")
    assert _listing(tmp_path / "store.zarr") == ["a"]
    assert _listing(tmp_path) == ["store.zarr"]


def test_replace_existing_directory(tmp_path: pathlib.Path) -> None:
    (tmp_path / "store.zarr").mkdir()
    (tmp_path / "store.zarr" / "old").write_text("1")
    with Path(tmp_path / "store.zarr").staged() as tmp:
        tmp.mkdir()
        (tmp / "new").write_text("2")
        assert _listing(tmp_path / "store.zarr") == ["old"]
    assert _listing(tmp_path / "store.zarr") == ["new"]
    assert _listing(tmp_path) == ["store.zarr"]


def test_file_becomes_directory(tmp_path: pathlib.Path) -> None:
    (tmp_path / "out").write_text("a file")
    with Path(tmp_path / "out").staged() as tmp:
        tmp.mkdir()
        (tmp / "inner").write_text("x")
    assert (tmp_path / "out").is_dir()
    assert _listing(tmp_path) == ["out"]


def test_directory_becomes_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "inner").write_text("x")
    with Path(tmp_path / "out").staged() as tmp:
        tmp.write_text("a file")
    assert (tmp_path / "out").read_text() == "a file"
    assert _listing(tmp_path) == ["out"]


# -- nothing staged, errors, discard ------------------------------------------
def test_nothing_staged_changes_nothing(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.txt").write_text("old")
    with Path(tmp_path / "x.txt").staged():
        pass
    with Path(tmp_path / "missing.txt").staged():
        pass
    assert (tmp_path / "x.txt").read_text() == "old"
    assert _listing(tmp_path) == ["x.txt"]


def test_an_error_in_the_block_leaves_the_target(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "x.txt").write_text("old")
    with pytest.raises(RuntimeError, match="boom"):
        with Path(tmp_path / "x.txt").staged() as tmp:
            tmp.write_text("half")
            raise RuntimeError("boom")
    assert (tmp_path / "x.txt").read_text() == "old"
    assert _listing(tmp_path) == ["x.txt"]


def test_commit_and_discard_by_hand(tmp_path: pathlib.Path) -> None:
    target = Path(tmp_path / "x.txt")
    stage = target.staged()
    stage.path.write_text("by hand")
    stage.commit()
    assert target.read_text() == "by hand"
    stage.discard()  # after a commit: nothing to do
    with pytest.raises(ValueError, match="already committed"):
        stage.commit()

    other = target.staged()
    other.path.write_text("dropped")
    other.discard()
    other.discard()
    assert target.read_text() == "by hand"
    with pytest.raises(ValueError, match="discarded"):
        other.commit()
    assert _listing(tmp_path) == ["x.txt"]


def test_a_dropped_stage_cleans_up(tmp_path: pathlib.Path) -> None:
    stage = Path(tmp_path / "x.txt").staged()
    stage.path.write_text("never committed")
    del stage
    gc.collect()
    assert _listing(tmp_path) == []


def test_two_stages_on_one_target_last_commit_wins(
    tmp_path: pathlib.Path,
) -> None:
    target = Path(tmp_path / "x.txt")
    first, second = target.staged(), target.staged()
    assert first.path != second.path
    first.path.write_text("first")
    second.path.write_text("second")
    first.commit()
    second.commit()
    assert target.read_text() == "second"
    assert _listing(tmp_path) == ["x.txt"]


# -- a failed commit ----------------------------------------------------------
def test_a_failed_replace_keeps_target_and_staged_data(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.txt").write_text("old")
    target = Path(tmp_path / "x.txt")
    stage = target.staged(keep_on_failure=True)
    stage.path.write_text("new")

    def refuse(src: str, dst: str) -> None:
        raise PermissionError(errno.EACCES, "in use", dst)

    monkeypatch.setattr(staged_module.os, "replace", refuse)
    with pytest.raises(CommitError) as info:
        stage.commit()
    error = info.value
    assert isinstance(error, OSError)
    assert error.errno == errno.EACCES
    assert isinstance(error.__cause__, PermissionError)
    assert error.target is target
    assert str(stage.path) in str(error) and str(target) in str(error)
    assert target.read_text() == "old"
    assert stage.path.read_text() == "new"  # the work is kept

    monkeypatch.undo()
    del stage  # kept past garbage collection too ...
    gc.collect()
    leftovers = [p for p in tmp_path.iterdir() if p.name != "x.txt"]
    assert len(leftovers) == 1
    # ... until it is committed again (here by hand) or discarded.
    os.replace(str(leftovers[0] / "x.txt"), str(tmp_path / "x.txt"))


def test_a_failed_directory_swap_restores_the_old_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "old").write_text("1")
    stage = Path(tmp_path / "out").staged(keep_on_failure=True)
    stage.path.mkdir()
    (stage.path / "new").write_text("2")

    real_rename = os.rename
    calls = []

    def second_move_fails(src: str, dst: str) -> None:
        calls.append(src)
        if len(calls) == 2:
            raise OSError(errno.EXDEV, "cannot move", dst)
        real_rename(src, dst)

    monkeypatch.setattr(staged_module.os, "rename", second_move_fails)
    with pytest.raises(CommitError):
        stage.commit()
    monkeypatch.undo()
    assert _listing(tmp_path / "out") == ["old"]
    assert (stage.path / "new").read_text() == "2"
    # A failed commit may be retried.
    stage.commit()
    assert _listing(tmp_path / "out") == ["new"]
    assert _listing(tmp_path) == ["out"]


# -- copy=True ----------------------------------------------------------------
def test_copy_starts_from_the_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "log.txt").write_text("line 1\n")
    target = Path(tmp_path / "log.txt")
    with target.staged(copy=True) as tmp:
        assert tmp.read_text() == "line 1\n"
        with tmp.open("a") as handle:
            handle.write("line 2\n")
        assert target.read_text() == "line 1\n"  # untouched until commit
    assert target.read_text() == "line 1\nline 2\n"


def test_copy_starts_from_the_directory(tmp_path: pathlib.Path) -> None:
    (tmp_path / "store.zarr" / "sub").mkdir(parents=True)
    (tmp_path / "store.zarr" / "a").write_text("1")
    (tmp_path / "store.zarr" / "sub" / "b").write_text("2")
    target = Path(tmp_path / "store.zarr")
    with target.staged(copy=True) as tmp:
        assert (tmp / "sub" / "b").read_text() == "2"
        (tmp / "c").write_text("3")
        assert _listing(tmp_path / "store.zarr") == ["a", "sub"]
    assert _listing(tmp_path / "store.zarr") == ["a", "c", "sub"]
    assert (tmp_path / "store.zarr" / "sub" / "b").read_text() == "2"


def test_copy_of_a_missing_target_copies_nothing(
    tmp_path: pathlib.Path,
) -> None:
    with Path(tmp_path / "missing").staged(copy=True) as tmp:
        assert not tmp.exists()
    assert _listing(tmp_path) == []


# -- atomic= ------------------------------------------------------------------
def test_atomic_write_bytes_and_text(tmp_path: pathlib.Path) -> None:
    target = Path(tmp_path / "x.bin")
    assert target.write_bytes(b"abc", atomic=True) == 3
    assert target.read_bytes() == b"abc"
    assert target.write_text("é", encoding="utf-8", atomic=True) == 1
    assert target.read_text(encoding="utf-8") == "é"
    assert _listing(tmp_path) == ["x.bin"]


def test_atomic_open_commits_on_close(tmp_path: pathlib.Path) -> None:
    target = Path(tmp_path / "x.txt")
    handle = target.open("w", atomic=True)
    handle.write("new")
    assert not target.exists()
    handle.close()
    assert handle.closed
    handle.close()  # closing twice is harmless
    assert target.read_text() == "new"


def test_atomic_open_discards_on_error(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.txt").write_text("old")
    with pytest.raises(RuntimeError):
        with Path(tmp_path / "x.txt").open("w", atomic=True) as handle:
            handle.write("half")
            raise RuntimeError
    assert (tmp_path / "x.txt").read_text() == "old"
    assert _listing(tmp_path) == ["x.txt"]


def test_atomic_open_append_and_update_start_from_a_copy(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "x.txt").write_text("abc")
    with Path(tmp_path / "x.txt").open("a", atomic=True) as handle:
        handle.write("d")
    assert (tmp_path / "x.txt").read_text() == "abcd"
    with Path(tmp_path / "x.txt").open("r+", atomic=True) as handle:
        handle.write("X")
    assert (tmp_path / "x.txt").read_text() == "Xbcd"


def test_atomic_open_modes_that_do_not_write(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.txt").write_text("old")
    with pytest.raises(ValueError, match="writes"):
        Path(tmp_path / "x.txt").open("r", atomic=True)
    with pytest.raises(FileExistsError):
        Path(tmp_path / "x.txt").open("x", atomic=True)
    with Path(tmp_path / "new.txt").open("x", atomic=True) as handle:
        handle.write("created")
    assert (tmp_path / "new.txt").read_text() == "created"
    assert _listing(tmp_path) == ["new.txt", "x.txt"]


# -- capabilities -------------------------------------------------------------
def test_staged_is_listed_for_the_io_paths_only(
    tmp_path: pathlib.Path,
) -> None:
    assert Path(tmp_path).supports("staged")
    assert "staged" in Path(tmp_path).capabilities()
    assert AsyncPath(tmp_path).supports("staged")
    assert not PurePath(tmp_path).supports("staged")
    assert "staged" not in PurePath(tmp_path).capabilities()


# -- remote (universal-pathlib over fsspec's memory store) --------------------
@pytest.fixture
def memory() -> tx.Iterator[None]:
    pytest.importorskip("upath")
    from fsspec.implementations.memory import MemoryFileSystem

    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]
    yield
    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]


def _remote_names(path: Path) -> tx.List[str]:
    return sorted(p.name for p in path.iterdir())


def test_remote_file_is_staged_locally_and_uploaded(memory: None) -> None:
    target = Path("memory://bucket/dir/x.txt")
    target.write_text("old")
    with target.staged() as tmp:
        assert tmp.is_local()
        assert tmp.name == "x.txt"
        tmp.write_text("new")
        assert target.read_text() == "old"
    assert target.read_text() == "new"
    assert not pathlib.Path(str(tmp)).parent.exists()


def test_remote_directory_replaces_and_removes_stale(memory: None) -> None:
    target = Path("memory://bucket/tree")
    (target / "keep").write_text("k")
    (target / "stale").write_text("s")
    with target.staged(copy=True) as tmp:
        assert sorted(os.listdir(str(tmp))) == ["keep", "stale"]
        (tmp / "stale").unlink()
        (tmp / "sub").mkdir()
        (tmp / "sub" / "new").write_text("n")
    assert _remote_names(target) == ["keep", "sub"]
    assert (target / "sub" / "new").read_text() == "n"


def test_remote_type_changes(memory: None) -> None:
    target = Path("memory://bucket/out")
    target.write_text("a file")
    with target.staged() as tmp:
        tmp.mkdir()
        (tmp / "inner").write_text("x")
    assert target.is_dir()
    assert (target / "inner").read_text() == "x"
    with target.staged() as tmp:
        tmp.write_text("a file again")
    assert target.read_text() == "a file again"


def test_remote_copy_and_error_and_nothing(memory: None) -> None:
    target = Path("memory://bucket/x.txt")
    target.write_text("old")
    with target.staged(copy=True) as tmp:
        assert tmp.read_text() == "old"
    with pytest.raises(RuntimeError):
        with target.staged() as tmp:
            tmp.write_text("half")
            raise RuntimeError
    with target.staged(copy=True) as tmp:
        tmp.unlink()  # staging nothing
    assert target.read_text() == "old"
    missing = Path("memory://bucket/missing.txt")
    with missing.staged(copy=True) as tmp:
        assert not tmp.exists()
    assert not missing.exists()


def test_remote_atomic_write(memory: None) -> None:
    target = Path("memory://bucket/x.bin")
    assert target.write_bytes(b"abc", atomic=True) == 3
    assert target.read_bytes() == b"abc"


def test_a_failed_upload_keeps_target_and_staged_data(
    memory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Path("memory://bucket/x.txt")
    target.write_text("old")
    stage = target.staged(keep_on_failure=True)
    stage.path.write_text("new")

    def refuse(local: str, remote: tx.Any) -> None:
        raise ConnectionError("network down")

    monkeypatch.setattr(staged_module, "_upload_file", refuse)
    with pytest.raises(CommitError, match="network down") as info:
        stage.commit()
    assert info.value.errno is None
    assert target.read_text() == "old"
    assert stage.path.read_text() == "new"
    monkeypatch.undo()
    stage.commit()
    assert target.read_text() == "new"


# -- AsyncPath ----------------------------------------------------------------
def test_async_staged_local(tmp_path: pathlib.Path) -> None:
    target = AsyncPath(tmp_path / "x.txt")

    async def go() -> None:
        stage = target.staged()
        assert isinstance(stage, AsyncStage)
        with pytest.raises(RuntimeError, match="async with"):
            stage.path  # noqa: B018 - not made yet
        async with stage as tmp:
            assert isinstance(tmp, AsyncPath)
            assert tmp.name == "x.txt"
            await tmp.write_text("async")
        assert await target.read_text() == "async"

        manual = await target.staged()
        await manual.path.write_text("by hand")
        await manual.commit()
        await manual.discard()
        assert await target.read_text() == "by hand"

        dropped = await target.staged(copy=True)
        assert await dropped.path.read_text() == "by hand"
        await dropped.path.write_text("dropped")
        await dropped.discard()
        with pytest.raises(ValueError, match="discarded"):
            await dropped.commit()

        never_entered = target.staged()
        await never_entered.commit()  # nothing was staged

    _run(go())
    assert (tmp_path / "x.txt").read_text() == "by hand"
    assert _listing(tmp_path) == ["x.txt"]


def test_async_error_in_the_block(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.txt").write_text("old")

    async def go() -> None:
        async with AsyncPath(tmp_path / "x.txt").staged() as tmp:
            await tmp.write_text("half")
            raise RuntimeError

    with pytest.raises(RuntimeError):
        _run(go())
    assert (tmp_path / "x.txt").read_text() == "old"
    assert _listing(tmp_path) == ["x.txt"]


def test_async_atomic_writes(tmp_path: pathlib.Path) -> None:
    target = AsyncPath(tmp_path / "x.txt")

    async def go() -> None:
        assert await target.write_bytes(b"ab", atomic=True) == 2
        assert await target.write_text("cd", atomic=True) == 2
        handle = await target.open("a", atomic=True)
        await handle.write("e")
        await handle.close()
        assert await target.read_text() == "cde"
        with pytest.raises(RuntimeError):
            async with await target.open("w", atomic=True) as broken:
                await broken.write("half")
                raise RuntimeError
        with pytest.raises(ValueError):
            await target.open("rb", atomic=True)
        with pytest.raises(FileExistsError):
            await target.open("x", atomic=True)

    _run(go())
    assert (tmp_path / "x.txt").read_text() == "cde"
    assert _listing(tmp_path) == ["x.txt"]


def test_async_remote_over_a_sync_driver(memory: None) -> None:
    target = AsyncPath("memory://bucket/tree")

    async def go() -> None:
        await (target / "stale").write_text("s")
        async with target.staged(copy=True) as tmp:
            assert tmp.is_local()
            await (tmp / "stale").unlink()
            await (tmp / "new").write_text("n")
        names = sorted([p.name async for p in target.iterdir()])
        assert names == ["new"]

    _run(go())


# -- AsyncPath over a natively-async fsspec backend ---------------------------
@pytest.fixture
def native() -> tx.Iterator[str]:
    fsspec = pytest.importorskip("fsspec")
    wrapper = pytest.importorskip("fsspec.implementations.asyn_wrapper")
    from fsspec.implementations.memory import MemoryFileSystem

    class _MemStaged(wrapper.AsyncFileSystemWrapper):
        protocol = "memstaged"
        async_impl = True

        def __init__(self, *args: object, **kwargs: object) -> None:
            for drop in ("asynchronous", "skip_instance_cache"):
                kwargs.pop(drop, None)
            super().__init__(MemoryFileSystem(), asynchronous=True)

        @classmethod
        def _strip_protocol(cls, path: str) -> str:
            text = path.replace("memstaged://", "memory://")
            return MemoryFileSystem._strip_protocol(text)

    fsspec.register_implementation("memstaged", _MemStaged, clobber=True)
    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]
    yield "memstaged"
    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]


def test_async_remote_over_a_native_driver(native: str) -> None:
    from bagof.paths._async_fsspec import AsyncFSPath

    async def go() -> None:
        target = AsyncPath(f"{native}://bucket/x.txt")
        assert isinstance(target.wrapped, AsyncFSPath)
        await target.write_text("old")
        async with target.staged(copy=True) as tmp:
            assert tmp.is_local()
            assert await tmp.read_text() == "old"
            await tmp.write_text("new")
        assert await target.read_text() == "new"

        tree = AsyncPath(f"{native}://bucket/tree")
        await (tree / "stale").write_text("s")
        await (tree / "keep").write_text("k")
        async with tree.staged(copy=True) as tmp:
            await (tmp / "stale").unlink()
            await (tmp / "new").write_text("n")
        names = sorted([p.name async for p in tree.iterdir()])
        assert names == ["keep", "new"]

        async with target.staged() as tmp:
            await tmp.mkdir()
            await (tmp / "inner").write_text("i")
        assert await target.is_dir()
        async with target.staged() as tmp:
            await tmp.write_text("file again")
        assert await target.read_text() == "file again"

    _run(go())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX semantics")
def test_the_staged_path_carries_subclass_state(
    tmp_path: pathlib.Path,
) -> None:
    class Tagged(Path):
        pass

    target = Tagged(tmp_path / "x.txt")
    target.tag = "kept"
    with target.staged() as tmp:
        assert isinstance(tmp, Tagged)
        assert tmp.tag == "kept"


# -- edges --------------------------------------------------------------------
@POSIX
def test_a_name_too_long_for_the_prefix_still_stages(
    tmp_path: pathlib.Path,
) -> None:
    name = "x" * 250  # the ".<name>.staging-" prefix would pass NAME_MAX
    with Path(tmp_path / name).staged() as tmp:
        assert tmp.name == name
        tmp.write_text("ok")
    assert (tmp_path / name).read_text() == "ok"
    assert _listing(tmp_path) == [name]


def test_repr_names_the_target(tmp_path: pathlib.Path) -> None:
    stage = Path(tmp_path / "x.txt").staged()
    assert repr(stage) == f"Stage({str(tmp_path / 'x.txt')!r})"
    stage.discard()


def test_a_failed_copy_cleans_up(
    memory: None, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    target = Path("memory://bucket/x.txt")
    target.write_text("old")

    def broken(*args: tx.Any) -> None:
        raise ConnectionError("download failed")

    monkeypatch.setattr(staged_module, "_download_file", broken)
    monkeypatch.setattr(staged_module.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(ConnectionError):
        target.staged(copy=True)
    monkeypatch.setattr(staged_module, "_fill_remote", broken)

    async def go() -> None:
        async with AsyncPath("memory://bucket/x.txt").staged(copy=True):
            pass  # pragma: no cover - entering fails

    with pytest.raises(ConnectionError):
        _run(go())
    assert _listing(tmp_path) == []


def test_async_failed_commit_keeps_the_staged_data(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.txt").write_text("old")

    def refuse(*args: tx.Any) -> None:
        raise PermissionError(errno.EACCES, "in use")

    async def go() -> None:
        stage = await AsyncPath(tmp_path / "x.txt").staged(
            keep_on_failure=True
        )
        await stage.path.write_text("new")
        monkeypatch.setattr(staged_module, "_commit_local", refuse)
        with pytest.raises(CommitError) as info:
            await stage.commit()
        assert info.value.errno == errno.EACCES
        assert await stage.path.read_text() == "new"
        await stage.discard()

    _run(go())
    assert (tmp_path / "x.txt").read_text() == "old"
    assert _listing(tmp_path) == ["x.txt"]


def test_atomic_open_that_fails_to_open_cleans_up(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(LookupError):
        Path(tmp_path / "x.txt").open("w", encoding="nope", atomic=True)

    async def go() -> None:
        await AsyncPath(tmp_path / "y.txt").open(
            "w", encoding="nope", atomic=True
        )

    with pytest.raises(LookupError):
        _run(go())
    assert _listing(tmp_path) == []


def test_atomic_file_objects_behave_like_files(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "x.txt").write_text("a\nb\n")
    handle = Path(tmp_path / "x.txt").open("r+", atomic=True)
    assert list(handle) == ["a\n", "b\n"]
    handle.write("c\n")
    handle._abort()
    handle._abort()  # aborting twice is harmless
    assert (tmp_path / "x.txt").read_text() == "a\nb\n"

    async def go() -> None:
        target = AsyncPath(tmp_path / "x.txt")
        async with await target.open("a", atomic=True) as appended:
            await appended.write("c\n")
        await appended.close()  # closing twice is harmless
        updated = await target.open("r+", atomic=True)
        assert [line async for line in updated] == ["a\n", "b\n", "c\n"]
        await updated._abort()
        await updated._abort()

    _run(go())
    assert (tmp_path / "x.txt").read_text() == "a\nb\nc\n"
    assert _listing(tmp_path) == ["x.txt"]


def test_upload_through_cloudpathlib() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    target = Path(client.CloudPath("s3://bucket/dir/x.txt"))
    target.write_text("old")
    with target.staged(copy=True) as tmp:
        assert tmp.read_text() == "old"
        tmp.write_text("new")
    assert target.read_text() == "new"


class _WriteOnly:
    """A remote driver with neither a filesystem upload nor upload_from."""

    protocol = "fake"
    path = "bucket/x.bin"
    written: tx.List[bytes] = []

    def __str__(self) -> str:
        return "fake://bucket/x.bin"

    def write_bytes(self, data: bytes) -> int:
        type(self).written.append(data)
        return len(data)


def test_upload_falls_back_to_one_write(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.bin").write_bytes(b"content")
    staged_module._upload_file(str(tmp_path / "x.bin"), Path(_WriteOnly()))
    assert _WriteOnly.written == [b"content"]


def test_staging_under_a_missing_parent_raises(tmp_path: pathlib.Path) -> None:
    target = Path(tmp_path / "missing" / "x.txt")
    with pytest.raises(FileNotFoundError) as info:
        target.staged()
    assert info.value.filename == str(target)
    with pytest.raises(FileNotFoundError):
        target.write_bytes(b"x", atomic=True)


def test_commit_error_survives_pickling(tmp_path: pathlib.Path) -> None:
    import pickle

    error = CommitError(
        Path(tmp_path / "x"), str(tmp_path / ".x.staging-a/x"),
        PermissionError(errno.EACCES, "in use"),
    )
    copy = pickle.loads(pickle.dumps(error))
    assert type(copy) is CommitError
    assert copy.errno == errno.EACCES
    assert str(copy) == str(error)
    assert copy.target == error.target and copy.staged == error.staged

    removed = CommitError(Path(tmp_path / "x"), None, OSError("boom"))
    copy = pickle.loads(pickle.dumps(removed))
    assert copy.staged is None and str(copy) == str(removed)


def test_native_driver_with_nothing_to_copy_or_commit(native: str) -> None:
    async def go() -> None:
        target = AsyncPath(f"{native}://bucket/missing.txt")
        async with target.staged(copy=True) as tmp:
            assert not await tmp.exists()
        assert not await target.exists()

    _run(go())


def test_a_staging_directory_that_cannot_be_made_raises(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**kwargs: tx.Any) -> str:
        raise PermissionError(errno.EACCES, "read-only directory")

    monkeypatch.setattr(staged_module.tempfile, "mkdtemp", refuse)
    with pytest.raises(PermissionError):
        Path(tmp_path / "x.txt").staged()


# -- a failed commit leaves nothing behind (issue #46) ------------------------
# staged() removes its data and raises CommitError, unless keep_on_failure
# (tested above); atomic= removes it and raises the error itself, as the
# plain write would.
@pytest.mark.parametrize("front", ["sync", "async"])
def test_a_failed_commit_removes_the_staged_data(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, front: str
) -> None:
    target = tmp_path / "x.txt"
    target.write_text("old")

    def refuse(src: str, dst: str) -> None:
        raise PermissionError(errno.EACCES, "in use")

    async def go() -> None:
        async with AsyncPath(target).staged() as tmp:
            await tmp.write_text("new")

    monkeypatch.setattr(staged_module.os, "replace", refuse)
    with pytest.raises(CommitError) as info:
        if front == "sync":
            with Path(target).staged() as tmp:
                tmp.write_text("new")
        else:
            _run(go())
    error = info.value
    assert error.errno == errno.EACCES
    assert isinstance(error.__cause__, PermissionError)
    assert error.staged is None
    assert "removed" in str(error)
    assert target.read_text() == "old"
    assert _listing(tmp_path) == ["x.txt"]


def test_a_stage_whose_commit_failed_is_discarded(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = Path(tmp_path / "x.txt").staged()
    stage.path.write_text("new")

    def refuse(src: str, dst: str) -> None:
        raise PermissionError(errno.EACCES, "in use")

    monkeypatch.setattr(staged_module.os, "replace", refuse)
    with pytest.raises(CommitError):
        stage.commit()
    monkeypatch.undo()
    with pytest.raises(ValueError, match="discarded"):
        stage.commit()
    assert _listing(tmp_path) == []


def test_a_failed_remote_commit_removes_the_staged_data(
    memory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Path("memory://bucket/x.txt")
    target.write_text("old")
    stage = target.staged()
    stage.path.write_text("new")
    staging = pathlib.Path(str(stage.path)).parent

    def refuse(local: str, remote: tx.Any) -> None:
        raise ConnectionError("network down")

    monkeypatch.setattr(staged_module, "_upload_file", refuse)
    with pytest.raises(CommitError, match="network down") as info:
        stage.commit()
    assert info.value.staged is None
    assert target.read_text() == "old"
    assert not staging.exists()


@pytest.mark.parametrize("front", ["sync", "async"])
def test_a_failed_atomic_commit_leaves_nothing_behind(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, front: str
) -> None:
    target = tmp_path / "y.bin"
    target.write_bytes(b"old")

    def refuse(src: str, dst: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(staged_module.os, "replace", refuse)
    with pytest.raises(PermissionError) as info:
        if front == "sync":
            Path(target).write_bytes(b"new", atomic=True)
        else:
            _run(AsyncPath(target).write_bytes(b"new", atomic=True))
    assert not isinstance(info.value, CommitError)
    assert info.value.errno == errno.EACCES
    assert target.read_bytes() == b"old"
    assert _listing(tmp_path) == ["y.bin"]


@pytest.mark.parametrize("front", ["sync", "async"])
def test_a_failed_atomic_close_leaves_nothing_behind(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, front: str
) -> None:
    target = tmp_path / "y.txt"

    def refuse(src: str, dst: str) -> None:
        raise OSError(errno.EIO, "boom")

    async def go() -> None:
        handle = await AsyncPath(target).open("w", atomic=True)
        await handle.write("new")
        monkeypatch.setattr(staged_module.os, "replace", refuse)
        await handle.close()

    with pytest.raises(OSError) as info:
        if front == "sync":
            handle = Path(target).open("w", atomic=True)
            handle.write("new")
            monkeypatch.setattr(staged_module.os, "replace", refuse)
            handle.close()
        else:
            _run(go())
    assert info.value.errno == errno.EIO
    assert not isinstance(info.value, CommitError)
    assert _listing(tmp_path) == []


def test_a_failed_remote_atomic_upload_leaves_nothing_behind(
    memory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Path("memory://bucket/x.txt")
    target.write_text("old")

    def refuse(local: str, target: tx.Any) -> None:
        raise OSError(errno.EIO, "upload failed")

    monkeypatch.setattr(staged_module, "_upload_file", refuse)
    with pytest.raises(OSError) as info:
        target.write_text("new", atomic=True)
    assert not isinstance(info.value, CommitError)
    assert target.read_text() == "old"


# -- an atomic write refuses a directory (issue #45) --------------------------
# As pathlib and os.replace do; staged() replaces a change of type unless
# replace_type=False.
def _a_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    tree = tmp_path / "x.nii"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "keep.txt").write_text("k")
    return tree


ATOMIC_WRITERS = {
    "write_bytes": lambda p: p.write_bytes(b"new", atomic=True),
    "write_text": lambda p: p.write_text("new", atomic=True),
    "open-w": lambda p: p.open("w", atomic=True).close(),
    "open-a": lambda p: p.open("a", atomic=True).close(),
}


async def _async_write(p: AsyncPath, how: str) -> None:
    if how == "write_bytes":
        await p.write_bytes(b"new", atomic=True)
    elif how == "write_text":
        await p.write_text("new", atomic=True)
    else:
        handle = await p.open("w" if how == "open-w" else "a", atomic=True)
        await handle.close()


@pytest.mark.parametrize("how", sorted(ATOMIC_WRITERS))
def test_atomic_write_refuses_a_directory(
    tmp_path: pathlib.Path, how: str
) -> None:
    tree = _a_tree(tmp_path)
    with pytest.raises(IsADirectoryError) as info:
        ATOMIC_WRITERS[how](Path(tree))
    assert info.value.filename == str(tree)
    assert (tree / "sub" / "keep.txt").read_text() == "k"
    assert _listing(tmp_path) == ["x.nii"]

    with pytest.raises(IsADirectoryError):
        _run(_async_write(AsyncPath(tree), how))
    assert (tree / "sub" / "keep.txt").read_text() == "k"
    assert _listing(tmp_path) == ["x.nii"]


def test_atomic_write_refuses_a_directory_made_while_writing(
    tmp_path: pathlib.Path,
) -> None:
    # A check before the write cannot stand in for the one in the commit.
    target = tmp_path / "x.nii"
    handle = Path(target).open("wb", atomic=True)
    handle.write(b"new")
    _a_tree(tmp_path)
    with pytest.raises(IsADirectoryError) as info:
        handle.close()
    assert info.value.filename == str(target)
    assert (target / "sub" / "keep.txt").read_text() == "k"
    assert _listing(tmp_path) == ["x.nii"]

    async def go() -> None:
        other = tmp_path / "y.nii"
        handle = await AsyncPath(other).open("wb", atomic=True)
        await handle.write(b"new")
        other.mkdir()
        with pytest.raises(IsADirectoryError):
            await handle.close()
        assert other.is_dir()

    _run(go())
    assert _listing(tmp_path) == ["x.nii", "y.nii"]


def test_staged_can_refuse_a_change_of_type(tmp_path: pathlib.Path) -> None:
    tree = _a_tree(tmp_path)
    stage = Path(tree).staged(replace_type=False, keep_on_failure=True)
    stage.path.write_text("a file")
    with pytest.raises(CommitError) as info:
        stage.commit()
    assert info.value.errno == errno.EISDIR
    assert isinstance(info.value.__cause__, IsADirectoryError)
    assert (tree / "sub" / "keep.txt").read_text() == "k"
    assert stage.path.read_text() == "a file"  # the work is kept
    stage.discard()

    (tmp_path / "f").write_text("old")
    with pytest.raises(CommitError) as info:
        with Path(tmp_path / "f").staged(replace_type=False) as tmp:
            tmp.mkdir()
    assert info.value.errno == errno.ENOTDIR
    assert (tmp_path / "f").read_text() == "old"

    # The same type is replaced as usual.
    with Path(tree).staged(replace_type=False) as tmp:
        tmp.mkdir()
        (tmp / "new").write_text("n")
    assert _listing(tree) == ["new"]


def test_async_staged_can_refuse_a_change_of_type(
    tmp_path: pathlib.Path,
) -> None:
    tree = _a_tree(tmp_path)

    async def go() -> None:
        with pytest.raises(CommitError) as info:
            async with AsyncPath(tree).staged(replace_type=False) as tmp:
                await tmp.write_text("a file")
        assert info.value.errno == errno.EISDIR

    _run(go())
    assert (tree / "sub" / "keep.txt").read_text() == "k"


def test_remote_atomic_write_refuses_a_directory(memory: None) -> None:
    tree = Path("memory://bucket/out")
    (tree / "keep").write_text("k")
    with pytest.raises(IsADirectoryError) as info:
        tree.write_text("a file", atomic=True)
    assert info.value.filename == str(tree)
    assert (tree / "keep").read_text() == "k"

    with pytest.raises(IsADirectoryError):
        _run(AsyncPath("memory://bucket/out").write_text("x", atomic=True))
    assert (tree / "keep").read_text() == "k"

    target = Path("memory://bucket/file")
    target.write_text("old")
    with pytest.raises(CommitError) as info:
        with target.staged(replace_type=False) as tmp:
            tmp.mkdir()
            (tmp / "inner").write_text("x")
    assert info.value.errno == errno.ENOTDIR
    assert target.read_text() == "old"


def test_native_remote_atomic_write_refuses_a_directory(native: str) -> None:
    async def go() -> None:
        tree = AsyncPath(f"{native}://bucket/tree")
        await (tree / "keep").write_text("k")
        with pytest.raises(IsADirectoryError):
            await tree.write_text("a file", atomic=True)
        assert await (tree / "keep").read_text() == "k"

        target = AsyncPath(f"{native}://bucket/file")
        await target.write_text("old")
        with pytest.raises(CommitError) as info:
            async with target.staged(replace_type=False) as tmp:
                await tmp.mkdir()
                await (tmp / "inner").write_text("x")
        assert info.value.errno == errno.ENOTDIR
        assert await target.read_text() == "old"

    _run(go())


# -- an atomic write's errors name the target (issue #47) ---------------------
# The caller of atomic= never sees the staging directory, so an error naming
# one of its entries names the target instead; staged() names the path its
# caller wrote.
@pytest.mark.parametrize("front", ["sync", "async"])
def test_a_failed_atomic_commit_names_the_target(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, front: str
) -> None:
    target = tmp_path / "y.bin"

    def refuse(src: str, dst: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", src, dst)

    monkeypatch.setattr(staged_module.os, "replace", refuse)
    with pytest.raises(PermissionError) as info:
        if front == "sync":
            Path(target).write_bytes(b"new", atomic=True)
        else:
            _run(AsyncPath(target).write_bytes(b"new", atomic=True))
    error = info.value
    assert error.errno == errno.EACCES
    assert error.filename == str(target)
    assert error.filename2 is None
    assert ".staging-" not in str(error)
    assert _listing(tmp_path) == []


@pytest.mark.parametrize("mode", ["r+", "rb+"])
def test_an_atomic_open_error_names_the_target(
    tmp_path: pathlib.Path, mode: str
) -> None:
    target = tmp_path / "z.bin"
    with pytest.raises(FileNotFoundError) as info:
        Path(target).open(mode, atomic=True)
    assert info.value.filename == str(target)
    assert _listing(tmp_path) == []

    async def go() -> None:
        await AsyncPath(target).open(mode, atomic=True)

    with pytest.raises(FileNotFoundError) as info:
        _run(go())
    assert info.value.filename == str(target)
    assert _listing(tmp_path) == []


@pytest.mark.parametrize("front", ["sync", "async"])
def test_an_atomic_write_error_names_the_target(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, front: str
) -> None:
    target = tmp_path / "z.bin"
    real_open = pathlib.Path.open

    def full_disk(self: pathlib.Path, *args: tx.Any, **kw: tx.Any) -> tx.Any:
        if ".staging-" in str(self):
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        return real_open(self, *args, **kw)

    monkeypatch.setattr(pathlib.Path, "open", full_disk)
    with pytest.raises(OSError) as info:
        if front == "sync":
            Path(target).write_text("x", atomic=True)
        else:
            _run(AsyncPath(target).write_text("x", atomic=True))
    assert info.value.errno == errno.ENOSPC
    assert info.value.filename == str(target)
    assert _listing(tmp_path) == []


def test_an_error_without_a_staged_name_is_kept_as_it_is(
    tmp_path: pathlib.Path,
) -> None:
    class Boom(Exception):
        pass

    error = Boom()
    with pytest.raises(Boom) as info:
        with staged_module.atomic(Path(tmp_path / "x")):
            raise error
    assert info.value is error
    assert _listing(tmp_path) == []


def test_staged_errors_name_the_staged_path(tmp_path: pathlib.Path) -> None:
    # The caller wrote at the staged path, so that is the path named.
    target = Path(tmp_path / "z.bin")
    with pytest.raises(IsADirectoryError) as info:
        with target.staged() as tmp:
            tmp.mkdir()
            tmp.write_bytes(b"x")
    assert info.value.filename == str(tmp)
    assert _listing(tmp_path) == []


def test_a_staging_directory_that_cannot_be_made_names_the_target(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(prefix: str, dir: str) -> str:
        raise PermissionError(
            errno.EACCES, "Permission denied", os.path.join(dir, prefix)
        )

    monkeypatch.setattr(staged_module.tempfile, "mkdtemp", refuse)
    target = tmp_path / "x.txt"
    for make in (
        lambda: Path(target).staged(),
        lambda: Path(target).write_text("x", atomic=True),
    ):
        with pytest.raises(PermissionError) as info:
            make()
        assert info.value.filename == str(target)


def test_a_remote_staging_directory_error_keeps_its_own_name(
    memory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A remote target is staged in the system temp directory, which is not
    # beside the target: that error is about the temp directory, so it
    # keeps the name it has.
    def refuse(prefix: str, dir: tx.Any) -> str:
        raise PermissionError(errno.EACCES, "Permission denied", "/tmp/x")

    monkeypatch.setattr(staged_module.tempfile, "mkdtemp", refuse)
    with pytest.raises(PermissionError) as info:
        Path("memory://bucket/x.txt").staged()
    assert info.value.filename == "/tmp/x"
