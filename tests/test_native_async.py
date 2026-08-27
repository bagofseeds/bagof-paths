"""Native-async drivers: AsyncPath awaits an async driver directly.

The driver's coroutine members are awaited (no thread); the few members it
does not have fall back to a local stdlib view run in a thread. A fake driver
(coroutine I/O over a real temp path) exercises both paths on every
interpreter; a real ``anyio.Path`` leg confirms it against an actual driver.
"""

import asyncio
import os
import pathlib

import pytest

import bagof.paths._async_path as ap
from bagof.paths import AsyncPath, UnsupportedPathOperation


def _run(coro: object) -> object:
    return asyncio.run(coro)


class _Handle:
    """A native async file handle over a synchronous one."""

    def __init__(self, fh: object) -> None:
        self._fh = fh

    async def read(self, *args: object) -> object:
        return self._fh.read(*args)

    async def readline(self, *args: object) -> object:
        return self._fh.readline(*args)

    async def write(self, data: object) -> int:
        return self._fh.write(data)

    async def flush(self) -> None:
        self._fh.flush()

    async def aclose(self) -> None:
        self._fh.close()


class _FakeAsync(os.PathLike):
    """A local driver whose I/O members are coroutines, like ``anyio.Path``.

    Lexical parts are synchronous; I/O parts are awaited. It deliberately lacks
    ``read_text``/``glob``/``copy``/``walk``/... so those exercise the local
    stdlib fallback.
    """

    def __init__(self, *segments: object) -> None:
        self._real = pathlib.Path(*segments)

    # -- lexical (synchronous) --
    def __fspath__(self) -> str:
        return os.fspath(self._real)

    def __str__(self) -> str:
        return str(self._real)

    def __truediv__(self, other: object) -> "_FakeAsync":
        return _FakeAsync(self._real / other)

    @property
    def name(self) -> str:
        return self._real.name

    @property
    def parent(self) -> "_FakeAsync":
        return _FakeAsync(str(self._real.parent))

    @property
    def suffix(self) -> str:
        return self._real.suffix

    def with_name(self, name: str) -> "_FakeAsync":
        return _FakeAsync(str(self._real.with_name(name)))

    # -- I/O (coroutines) --
    async def exists(self) -> bool:
        return self._real.exists()

    async def is_file(self) -> bool:
        return self._real.is_file()

    async def is_dir(self) -> bool:
        return self._real.is_dir()

    async def stat(self) -> os.stat_result:
        return self._real.stat()

    async def read_bytes(self) -> bytes:
        return self._real.read_bytes()

    async def write_bytes(self, data: object) -> int:
        return self._real.write_bytes(data)

    async def mkdir(
        self, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        self._real.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

    async def unlink(self, missing_ok: bool = False) -> None:
        self._real.unlink(missing_ok=missing_ok)

    async def rename(self, target: object) -> "_FakeAsync":
        self._real.rename(os.fspath(target))
        return _FakeAsync(os.fspath(target))

    async def resolve(self, strict: bool = False) -> "_FakeAsync":
        return _FakeAsync(str(self._real.resolve()))

    async def iterdir(self) -> object:
        for child in self._real.iterdir():
            yield _FakeAsync(str(child))

    async def open(self, mode: str = "r", **kwargs: object) -> _Handle:
        return _Handle(self._real.open(mode, **kwargs))


# -- the fake driver, on every interpreter ----------------------------------
def test_native_read_write_status(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(_FakeAsync(tmp_path) / "a.txt")
        assert await p.exists() is False
        assert await p.write_bytes(b"hi") == 2
        assert await p.read_bytes() == b"hi"
        assert await p.exists() is True
        assert await p.is_file() is True
        assert (await p.stat()).st_size == 2

    _run(go())


def test_native_read_text_via_fallback(tmp_path: pathlib.Path) -> None:
    # read_text is not a native member here: synthesized from read_bytes.
    async def go() -> None:
        p = AsyncPath(_FakeAsync(tmp_path) / "a.txt")
        await p.write_bytes("héllo".encode())
        assert await p.read_text() == "héllo"

    _run(go())


def test_native_lexical_is_sync(tmp_path: pathlib.Path) -> None:
    p = AsyncPath(_FakeAsync(tmp_path) / "a.txt")
    assert p.name == "a.txt"
    assert p.suffix == ".txt"
    assert isinstance(p.parent, AsyncPath)
    assert isinstance(p.parent.wrapped, _FakeAsync)


def test_native_iterdir_stays_in_family(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.txt").write_text("x")

    async def go() -> None:
        entries = [e async for e in AsyncPath(_FakeAsync(tmp_path)).iterdir()]
        assert [e.name for e in entries] == ["a.txt"]
        assert all(isinstance(e.wrapped, _FakeAsync) for e in entries)

    _run(go())


def test_native_glob_via_fallback(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.log").write_text("y")

    async def go() -> None:
        root = AsyncPath(_FakeAsync(tmp_path))
        names = sorted([e.name async for e in root.glob("*.txt")])
        assert names == ["a.txt"]

    _run(go())


class _TrioLikeAsync(_FakeAsync):
    """iterdir is a coroutine returning a *sync* iterable, like trio.Path."""

    async def iterdir(self) -> object:
        return [_FakeAsync(str(child)) for child in self._real.iterdir()]


def test_native_iterdir_coroutine_returning_iterable(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "a.txt").write_text("x")

    async def go() -> None:
        root = AsyncPath(_TrioLikeAsync(tmp_path))
        names = sorted([e.name async for e in root.iterdir()])
        assert names == ["a.txt"]

    _run(go())


def test_native_rglob_via_fallback(tmp_path: pathlib.Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x")

    async def go() -> None:
        root = AsyncPath(_FakeAsync(tmp_path))
        names = sorted([e.name async for e in root.rglob("*.txt")])
        assert names == ["a.txt"]

    _run(go())


@pytest.mark.skipif(
    not hasattr(os, "symlink") or not hasattr(pathlib.Path, "readlink"),
    reason="needs symlink support and pathlib.readlink (added in 3.9)",
)
def test_native_readlink_via_fallback(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "t.txt"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)

    async def go() -> None:
        got = await AsyncPath(_FakeAsync(link)).readlink()
        assert isinstance(got.wrapped, _FakeAsync)
        assert got.name == "t.txt"

    _run(go())


def test_native_mkdir_rename_resolve(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        d = AsyncPath(_FakeAsync(tmp_path) / "sub")
        await d.mkdir()
        assert await d.is_dir() is True
        f = AsyncPath(_FakeAsync(tmp_path) / "r.txt")
        await f.write_bytes(b"x")
        moved = await f.rename(tmp_path / "r2.txt")
        assert isinstance(moved.wrapped, _FakeAsync)
        assert await moved.read_bytes() == b"x"
        resolved = await AsyncPath(_FakeAsync(tmp_path)).resolve()
        assert isinstance(resolved.wrapped, _FakeAsync)

    _run(go())


def test_native_open_file(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(_FakeAsync(tmp_path) / "a.txt")
        async with await p.open("w") as handle:
            await handle.write("l1\nl2\n")
            await handle.flush()
        async with await p.open() as handle:
            assert await handle.read() == "l1\nl2\n"
        lines = []
        async with await p.open() as handle:
            async for line in handle:
                lines.append(line)
        assert lines == ["l1\n", "l2\n"]

    _run(go())


def test_native_copy_and_walk_via_fallback(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        src = AsyncPath(_FakeAsync(tmp_path) / "a.txt")
        await src.write_bytes(b"x")
        dst = await src.copy(tmp_path / "b.txt")
        assert isinstance(dst.wrapped, _FakeAsync)
        assert await dst.read_bytes() == b"x"
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.txt").write_text("y")
        roots = [
            dp.wrapped
            async for dp, _dn, _fn in AsyncPath(_FakeAsync(tmp_path)).walk()
        ]
        assert roots and all(isinstance(r, _FakeAsync) for r in roots)

    _run(go())


def test_native_unlink(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        p = AsyncPath(_FakeAsync(tmp_path) / "gone")
        with pytest.raises(FileNotFoundError):
            await p.unlink()
        await p.write_bytes(b"x")
        await p.unlink()
        assert await p.exists() is False

    _run(go())


def test_native_unsupported_member_raises(tmp_path: pathlib.Path) -> None:
    # A member neither the async driver nor the local fallback provides.
    async def go() -> None:
        p = AsyncPath(_FakeAsync(tmp_path) / "a.txt")
        await p.write_bytes(b"x")
        with pytest.raises(UnsupportedPathOperation):
            await p.download_to(tmp_path / "out")

    _run(go())


def test_non_local_async_driver_has_no_fallback() -> None:
    class _CloudAsync(os.PathLike):
        protocol = "s3"

        def __fspath__(self) -> str:
            return "s3://b/k"

        def __str__(self) -> str:
            return "s3://b/k"

        async def exists(self) -> bool:
            return True

    async def go() -> None:
        p = AsyncPath(_CloudAsync())
        assert await p.exists() is True  # a native member: awaited directly
        with pytest.raises(UnsupportedPathOperation):
            # a member the driver lacks, and no local view can stand in
            await p.read_bytes()

    _run(go())


# -- the async-file adapter, unit-level -------------------------------------
class _AsyncHandle:
    async def read(self, *args: object) -> str:
        return "x"

    async def aclose(self) -> None:
        self.closed = True


def test_adapt_async_file_awaitable() -> None:
    async def go() -> None:
        async def opened() -> _AsyncHandle:
            return _AsyncHandle()

        f = await ap._adapt_async_file(opened())
        assert await f.read() == "x"
        await f.close()

    _run(go())


def test_adapt_async_file_plain_handle() -> None:
    async def go() -> None:
        f = await ap._adapt_async_file(_AsyncHandle())
        assert await f.read() == "x"
        await f.close()  # closes via aclose

    _run(go())


def test_adapt_async_file_context_manager() -> None:
    class _CM:
        exited = False

        async def __aenter__(self) -> _AsyncHandle:
            return _AsyncHandle()

        async def __aexit__(self, *exc: object) -> None:
            self.exited = True

    async def go() -> None:
        cm = _CM()
        f = await ap._adapt_async_file(cm)
        assert await f.read() == "x"
        await f.close()  # closes via the context manager
        assert cm.exited is True

    _run(go())


# -- a real anyio.Path ------------------------------------------------------
def test_anyio_driver_end_to_end(tmp_path: pathlib.Path) -> None:
    anyio = pytest.importorskip("anyio")

    async def go() -> None:
        p = AsyncPath(anyio.Path(tmp_path) / "a.txt")
        assert await p.exists() is False
        await p.write_text("hi")
        assert await p.read_text() == "hi"
        assert await p.is_file() is True
        root = AsyncPath(anyio.Path(tmp_path))
        names = [e.name async for e in root.iterdir()]
        assert "a.txt" in names
        resolved = await p.resolve()
        assert type(resolved.wrapped).__module__.startswith("anyio")
        async with await p.open() as handle:
            assert await handle.read() == "hi"

    _run(go())
