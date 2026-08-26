"""The async wrapper over stdlib pathlib, driven with asyncio.run."""

import asyncio
import os
import pathlib

import pytest

from bagof.paths import AsyncPath, Path, UnsupportedPathOperation


def _run(coro: object) -> object:
    return asyncio.run(coro)


def test_read_write_and_status(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(tmp_path) / "a.txt"
        assert await p.exists() is False
        assert await p.write_text("hi") == 2
        assert await p.read_text() == "hi"
        assert await p.exists() is True
        assert await p.is_file() is True

    _run(go())


def test_bytes_roundtrip(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(tmp_path) / "a.bin"
        await p.write_bytes(b"data")
        assert await p.read_bytes() == b"data"

    _run(go())


def test_pure_path_members_are_sync() -> None:
    # Lexical members are shared and stay synchronous on AsyncPath.
    p = AsyncPath("/a/b/c.txt")
    assert p.name == "c.txt"
    assert p.suffix == ".txt"
    assert isinstance(p.parent, AsyncPath)
    assert (p / "d").match("*/d") is True


def test_mkdir_iterdir(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        root = AsyncPath(tmp_path)
        d = root / "sub"
        await d.mkdir()
        assert await d.is_dir() is True
        await (d / "x.txt").touch()
        names = sorted([entry.name async for entry in d.iterdir()])
        assert names == ["x.txt"]
        kinds = [isinstance(entry, AsyncPath) async for entry in d.iterdir()]
        assert all(kinds)

    _run(go())


def test_glob(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.txt").touch()
    (tmp_path / "b.log").touch()

    async def go() -> None:
        root = AsyncPath(tmp_path)
        txts = sorted([p.name async for p in root.glob("*.txt")])
        assert txts == ["a.txt"]

    _run(go())


def test_copy_move_return_async_paths(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        src = AsyncPath(tmp_path) / "a.txt"
        await src.write_text("x")
        dst = await src.copy(tmp_path / "b.txt")
        assert isinstance(dst, AsyncPath)
        assert await dst.read_text() == "x"
        moved = await dst.move(tmp_path / "c.txt")
        assert isinstance(moved, AsyncPath)
        assert await moved.read_text() == "x"
        assert await dst.exists() is False

    _run(go())


def test_rmdir_recursive(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        d = AsyncPath(tmp_path) / "d"
        await d.mkdir()
        await (d / "f").touch()
        await d.rmdir(recursive=True)
        assert await d.exists() is False

    _run(go())


def test_walk(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.txt").touch()
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").touch()

    async def go() -> None:
        seen = {}
        async for dirpath, dirnames, filenames in AsyncPath(tmp_path).walk():
            assert isinstance(dirpath, AsyncPath)
            seen[str(dirpath)] = (sorted(dirnames), sorted(filenames))
        assert seen[str(tmp_path)] == (["sub"], ["a.txt"])

    _run(go())


def test_resolve_returns_async_path(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(tmp_path) / "a"
        assert isinstance(await p.resolve(), AsyncPath)

    _run(go())


def test_async_and_sync_paths_are_never_equal() -> None:
    # Different front-end families must not compare equal (settled identity).
    assert (AsyncPath("/a/b") == Path("/a/b")) is False
    assert AsyncPath("/a/b") == AsyncPath("/a/b")
    assert hash(AsyncPath("/a/b")) == hash(AsyncPath("/a/b"))


def test_unlink_missing(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(tmp_path) / "gone"
        with pytest.raises(FileNotFoundError):
            await p.unlink()
        await p.unlink(missing_ok=True)  # no raise

    _run(go())


def test_open_returns_async_file(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(tmp_path) / "a.txt"
        async with await p.open("w") as handle:
            await handle.write("hello\nworld\n")
        async with await p.open() as handle:
            assert await handle.read() == "hello\nworld\n"
        lines = []
        async with await p.open() as handle:
            async for line in handle:
                lines.append(line)
        assert lines == ["hello\n", "world\n"]

    _run(go())


def test_walk_honors_pruning(tmp_path: pathlib.Path) -> None:
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "a.txt").touch()
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "b.txt").touch()

    async def go() -> None:
        seen = []
        async for _dp, dirnames, filenames in AsyncPath(tmp_path).walk():
            seen.extend(filenames)
            if "skip" in dirnames:
                dirnames.remove("skip")  # prune -- must skip skip/b.txt
        assert "a.txt" in seen
        assert "b.txt" not in seen

    _run(go())


class _FakeAsyncDriver(os.PathLike):
    """A natively-async driver (coroutine methods) -- not yet supported."""

    def __fspath__(self) -> str:
        return "/x"

    def __str__(self) -> str:
        return "/x"

    async def exists(self) -> bool:
        return True

    async def open(self, *args: object, **kwargs: object) -> object:
        return None


def test_async_driver_is_rejected_cleanly() -> None:
    with pytest.raises(UnsupportedPathOperation):
        AsyncPath(_FakeAsyncDriver())


def test_sync_type_hook_honors_subclass(tmp_path: pathlib.Path) -> None:
    class MyPath(Path):
        @property
        def name(self) -> str:
            return "OVR-" + super().name

    class MyAsyncPath(AsyncPath):
        _sync_type = MyPath

    src = tmp_path / "a.txt"
    src.write_text("x")
    (tmp_path / "dest").mkdir()

    async def go() -> None:
        await MyAsyncPath(src).copy_into(tmp_path / "dest")

    _run(go())
    # The subclass's name override was honored inside the worker thread.
    assert (tmp_path / "dest" / "OVR-a.txt").read_text() == "x"
