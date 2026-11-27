"""The native async cloud driver closes the client sessions it opens.

In async mode, s3fs and fsspec's HTTP filesystem leave closing their aiohttp
session to the caller, so a loop that used one must close it before it ends.
Most of these tests stand in a fake ``fsspec`` module whose filesystems hold
a recording session, so they run without fsspec installed (the 3.8 core-only
leg too); the last one talks to a local HTTP server through the real HTTP
filesystem.
"""

import asyncio
import functools
import gc
import http.server
import sys
import threading
import types
import warnings

import pytest

import bagof.paths._async_fsspec as af


class _Session:
    """An aiohttp-like session that records being closed."""

    def __init__(self, fail: bool = False) -> None:
        self.closed = False
        self.closes = 0
        self.fail = fail

    async def close(self) -> None:
        self.closes += 1
        if self.fail:
            raise RuntimeError("the server went away")
        self.closed = True


class _Creator:
    """An s3fs-like client context, closed by leaving it."""

    def __init__(self) -> None:
        self.exits = 0

    async def __aexit__(self, *exc: object) -> None:
        self.exits += 1


class _HTTPLikeFS:
    """A filesystem that keeps its session in ``_session``, as HTTP does."""

    dropped: list = []

    def __init__(self, **options: object) -> None:
        self.options = options
        self._session = None

    async def set_session(self) -> _Session:
        if self._session is None:
            self._session = _Session(fail=bool(self.options.get("fail")))
        return self._session

    @staticmethod
    def close_session(loop: object, session: _Session) -> None:
        _HTTPLikeFS.dropped.append((loop, session))
        session.closed = True


class _S3LikeFS:
    """A filesystem that keeps its client context in ``_s3creator``."""

    def __init__(self, **options: object) -> None:
        self._s3creator = None

    async def set_session(self) -> None:
        self._s3creator = _Creator()


class _PlainFS:
    """A filesystem with no session at all (an in-memory store)."""

    def __init__(self, **options: object) -> None:
        pass


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every filesystem the driver builds, through a fake ``fsspec``."""
    classes = {"httplike": _HTTPLikeFS, "s3like": _S3LikeFS, "plain": _PlainFS}
    made: list = []

    def filesystem(scheme: str, **options: object) -> object:
        for drop in ("asynchronous", "skip_instance_cache"):
            options.pop(drop)
        fs = classes[scheme](**options)
        made.append(fs)
        return fs

    fake = types.ModuleType("fsspec")
    fake.filesystem = filesystem  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fsspec", fake)
    _HTTPLikeFS.dropped = []
    yield made
    af._sweep_closed_loops()


def _resolve(scheme: str, **options: object) -> object:
    return af._resolve_fs(scheme, options)


def test_asyncio_run_closes_the_session(built: list) -> None:
    async def main() -> object:
        fs = await _resolve("httplike")
        return asyncio.get_running_loop(), fs

    loop, fs = asyncio.run(main())
    assert fs._session.closed and fs._session.closes == 1
    # The loop's filesystems are forgotten as their sessions close.
    assert loop not in af._FS_BY_LOOP


def test_each_run_gets_and_closes_its_own_session(built: list) -> None:
    async def main() -> object:
        return await _resolve("httplike")

    first = asyncio.run(main())
    second = asyncio.run(main())
    assert first is not second
    assert first._session is not second._session
    assert first._session.closes == 1 and second._session.closes == 1


def test_a_startup_fan_out_builds_and_closes_one_filesystem(
    built: list,
) -> None:
    async def main() -> object:
        return await asyncio.gather(*[_resolve("httplike") for _ in range(8)])

    results = asyncio.run(main())
    assert len(built) == 1
    assert all(fs is built[0] for fs in results)
    assert built[0]._session.closes == 1


def test_every_filesystem_on_the_loop_is_closed(built: list) -> None:
    async def main() -> None:
        await _resolve("httplike", key="a")
        await _resolve("httplike", key="b")
        await _resolve("s3like")

    asyncio.run(main())
    http_a, http_b, s3 = built
    assert http_a._session.closes == 1 and http_b._session.closes == 1
    assert s3._s3creator.exits == 1


def test_a_filesystem_without_a_session_is_left_alone(built: list) -> None:
    async def main() -> object:
        fs = await _resolve("plain")
        assert await _resolve("plain") is fs
        return fs

    assert isinstance(asyncio.run(main()), _PlainFS)


def test_a_failed_close_does_not_break_shutdown(built: list) -> None:
    reported: list = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _, context: reported.append(context))
        await _resolve("httplike", fail=True)
        await _resolve("s3like")

    asyncio.run(main())
    failing, s3 = built
    assert failing._session.closes == 1
    # The other filesystem is still closed, and nothing is reported.
    assert s3._s3creator.exits == 1
    assert reported == []


def test_a_closed_session_is_not_closed_again(built: list) -> None:
    async def main() -> object:
        fs = await _resolve("httplike")
        await fs._session.close()
        return fs

    assert asyncio.run(main())._session.closes == 1


def test_a_hand_run_loop_closes_on_shutdown_asyncgens(built: list) -> None:
    loop = asyncio.new_event_loop()
    try:
        fs = loop.run_until_complete(_resolve("httplike"))
        # Still open while the loop is in use, between two calls.
        assert loop.run_until_complete(_resolve("httplike")) is fs
        assert fs._session.closes == 0
        loop.run_until_complete(loop.shutdown_asyncgens())
        assert fs._session.closes == 1
    finally:
        loop.close()
    assert loop not in af._FS_BY_LOOP


def test_a_loop_closed_without_shutdown_is_swept(built: list) -> None:
    loop = asyncio.new_event_loop()
    fs = loop.run_until_complete(_resolve("httplike"))
    loop.close()
    assert loop in af._FS_BY_LOOP
    af._sweep_closed_loops()
    # Nothing can be awaited on a closed loop: the backend's own
    # loop-less close is used instead.
    assert _HTTPLikeFS.dropped == [(None, fs._session)]
    assert loop not in af._FS_BY_LOOP


def test_http_session_is_closed_at_the_end_of_asyncio_run(
    tmp_path: object,
) -> None:
    pytest.importorskip("aiohttp")
    pytest.importorskip("fsspec.implementations.http")
    from bagof.paths import AsyncPath

    (tmp_path / "hello.txt").write_text("hello")  # type: ignore[operator]

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    handler = functools.partial(Quiet, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/hello.txt"

    async def main() -> object:
        path = AsyncPath(url)
        assert isinstance(path.wrapped, af.AsyncFSPath)
        assert await path.read_text() == "hello"
        (fs,) = af._filesystems(af._FS_BY_LOOP[asyncio.get_running_loop()])
        return fs._session

    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            session = asyncio.run(main())
            assert session.closed
            del session
            gc.collect()
    finally:
        server.shutdown()
        server.server_close()
    unclosed = [w for w in caught if "Unclosed" in str(w.message)]
    assert unclosed == []
