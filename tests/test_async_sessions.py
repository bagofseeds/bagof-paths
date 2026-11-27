"""The native async cloud driver closes the client sessions it opens.

In async mode, s3fs, gcsfs and fsspec's HTTP filesystem leave closing their
aiohttp session to the caller, so a loop that used one must close it before
it ends.
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

    # How many closes are under way at once, and the most there have been.
    closing = 0
    most_closing = 0

    def __init__(self, fail: bool = False) -> None:
        self.closed = False
        self.closes = 0
        self.fail = fail

    async def close(self) -> None:
        self.closes += 1
        _Session.closing += 1
        _Session.most_closing = max(_Session.most_closing, _Session.closing)
        try:
            await asyncio.sleep(0.01)
        finally:
            _Session.closing -= 1
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
    """A filesystem that keeps its session in ``_session``, as HTTP does.

    It reads and writes a shared store through that session, taking a
    round trip to do so, and fails as aiohttp does once it is closed.
    """

    store: dict = {}

    def __init__(self, **options: object) -> None:
        self.options = options
        self._session = None

    async def set_session(self) -> _Session:
        if self._session is None:
            self._session = _Session(fail=bool(self.options.get("fail")))
        return self._session

    async def _round_trip(self) -> None:
        # Longer than a close takes, so a close started meanwhile is seen.
        await asyncio.sleep(0.05)
        if self._session is None or self._session.closed:
            raise RuntimeError("Session is closed")

    async def _cat_file(self, path: str, **kwargs: object) -> bytes:
        await self._round_trip()
        return _HTTPLikeFS.store[path]

    async def _pipe_file(
        self, path: str, data: bytes, **kwargs: object
    ) -> None:
        await self._round_trip()
        _HTTPLikeFS.store[path] = data


class _S3LikeFS:
    """A filesystem that keeps its client context in ``_s3creator``."""

    def __init__(self, **options: object) -> None:
        self._s3creator = None

    async def set_session(self) -> None:
        self._s3creator = _Creator()


class _ServiceClient:
    """An async Azure-like service client, closed by awaiting ``close()``."""

    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1


class _AzureLikeFS:
    """A filesystem that keeps an async ``service_client``, as adlfs does."""

    def __init__(self, **options: object) -> None:
        self.service_client = _ServiceClient()


class _PlainFS:
    """A filesystem with no session at all (an in-memory store)."""

    def __init__(self, **options: object) -> None:
        pass


class _GCSLikeFS:
    """A filesystem that opens its ``_session`` on its first request."""

    def __init__(self, **options: object) -> None:
        self._session = None

    async def _cat_file(self, path: str, **kwargs: object) -> bytes:
        if self._session is None:
            self._session = _Session()
        return b""


class _GatedFS(_HTTPLikeFS):
    """An HTTP-like filesystem whose session opens when the gate does."""

    gate: object = None

    async def set_session(self) -> _Session:
        await _GatedFS.gate  # type: ignore[misc]
        return await super().set_session()


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every filesystem the driver builds, through a fake ``fsspec``."""
    classes = {
        "httplike": _HTTPLikeFS,
        "s3like": _S3LikeFS,
        "azurelike": _AzureLikeFS,
        "plain": _PlainFS,
        "gcslike": _GCSLikeFS,
        "gated": _GatedFS,
    }
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
    _HTTPLikeFS.store = {}
    _Session.most_closing = 0
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
    # Nothing can be awaited on a closed loop, so the entry is only dropped:
    # the session is not closed, nor pretended to be.
    assert loop not in af._FS_BY_LOOP
    assert fs._session.closes == 0 and not fs._session.closed


def test_a_filesystem_without_a_session_starts_no_closer(built: list) -> None:
    async def main() -> object:
        await _resolve("plain")
        return af._FS_BY_LOOP[asyncio.get_running_loop()]

    per_loop = asyncio.run(main())
    assert af._CLOSER_KEY not in per_loop


def test_a_loop_dropped_unclosed_lets_its_entry_go(built: list) -> None:
    # Only a session keeps a loop alive: with none, the weak key fires.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_resolve("plain"))
        del loop
        gc.collect()
        assert len(af._FS_BY_LOOP) == 0
    messages = [str(w.message) for w in caught]
    assert not [m for m in messages if "never awaited" in m], messages
    assert not [m for m in messages if "destroyed" in m], messages


def test_sessions_close_concurrently(built: list) -> None:
    async def main() -> None:
        for key in "abc":
            await _resolve("httplike", key=key)

    asyncio.run(main())
    assert all(fs._session.closes == 1 for fs in built)
    assert _Session.most_closing == 3


def test_an_azure_like_service_client_is_closed(built: list) -> None:
    async def main() -> object:
        return await _resolve("azurelike")

    assert asyncio.run(main()).service_client.closes == 1


def test_a_lazily_opened_session_is_closed(built: list) -> None:
    async def main() -> object:
        fs = await _resolve("gcslike")
        # Nothing is open yet, but the loop will close what opens later.
        assert af._CLOSER_KEY in af._FS_BY_LOOP[asyncio.get_running_loop()]
        await fs._cat_file("/x")
        return fs

    assert asyncio.run(main())._session.closes == 1


def test_a_lazy_session_never_opened_is_left_alone(built: list) -> None:
    async def main() -> object:
        return await _resolve("gcslike")

    assert asyncio.run(main())._session is None


def test_a_finished_loop_refuses_and_builds_nothing(built: list) -> None:
    loop = asyncio.new_event_loop()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            loop.run_until_complete(_resolve("httplike"))
            loop.run_until_complete(loop.shutdown_asyncgens())
            for scheme in ("httplike", "plain"):
                with pytest.raises(RuntimeError, match="has finished") as e:
                    loop.run_until_complete(_resolve(scheme))
                assert "_" not in str(e.value)
    finally:
        loop.close()
    assert len(built) == 1 and built[0]._session.closes == 1
    assert loop not in af._FS_BY_LOOP


def test_a_build_under_way_when_the_loop_finishes_is_closed(
    built: list,
) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_resolve("httplike"))
        _GatedFS.gate = loop.create_future()
        late = loop.create_task(_resolve("gated"))
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(loop.shutdown_asyncgens())
        _GatedFS.gate.set_result(None)  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError, match="has finished"):
            loop.run_until_complete(late)
    finally:
        loop.close()
    first, gated = built
    assert first._session.closes == 1 and gated._session.closes == 1
    assert loop not in af._FS_BY_LOOP


def test_shutdown_waits_for_no_other_generator(built: list) -> None:
    # A generator collected mid-iteration is closed in a task of its own;
    # this one never finishes. shutdown_asyncgens() does not wait for it,
    # and the closer must not either.
    never = None

    async def worker() -> object:
        try:
            yield
        finally:
            await never  # type: ignore[misc]

    async def main() -> object:
        nonlocal never
        never = asyncio.get_running_loop().create_future()
        fs = await _resolve("httplike")
        generator = worker()
        await generator.__anext__()
        del generator
        gc.collect()
        await asyncio.sleep(0.01)
        return fs

    loop = asyncio.new_event_loop()
    try:
        fs = loop.run_until_complete(main())
        loop.run_until_complete(
            asyncio.wait_for(loop.shutdown_asyncgens(), 5)
        )
        assert fs._session.closes == 1
        never.cancel()  # type: ignore[union-attr]
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="eager tasks are new in 3.12"
)
def test_an_eager_task_factory_shuts_down_cleanly(built: list) -> None:
    reported: list = []
    held: list = []

    async def other() -> object:
        try:
            yield
        finally:
            await asyncio.sleep(0)

    async def main() -> object:
        fs = await _resolve("httplike")
        generator = other()
        await generator.__anext__()
        held.append(generator)
        return fs

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _, context: reported.append(context))
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            fs = loop.run_until_complete(main())
            loop.run_until_complete(
                asyncio.wait_for(loop.shutdown_asyncgens(), 5)
            )
    finally:
        loop.close()
    assert fs._session.closes == 1
    assert reported == []


def test_loops_on_many_threads_share_the_cache(built: list) -> None:
    # Each thread keeps many loops open at once, so the cache is large
    # while other threads add to it and sweep it (before 3.13 a weak
    # dictionary is iterated in place, and an unguarded sweep raised).
    errors: list = []

    async def use() -> None:
        await asyncio.gather(
            *[_resolve("httplike") for _ in range(2)], _resolve("plain")
        )

    def worker() -> None:
        loops = []
        try:
            for _ in range(40):
                loop = asyncio.new_event_loop()
                loops.append(loop)
                loop.run_until_complete(use())
            for i, loop in enumerate(loops):
                if i % 2:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
                af._sweep_closed_loops()
        except BaseException as error:  # pragma: no cover - the failure
            errors.append(error)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert errors == []
    sessions = [fs._session for fs in built if isinstance(fs, _HTTPLikeFS)]
    # One filesystem per loop; the loops that shut down closed theirs.
    assert len(sessions) == 8 * 40
    assert sum(session.closes for session in sessions) == 8 * 20
    assert len(af._FS_BY_LOOP) == 0


def test_the_closer_is_named_plainly(built: list) -> None:
    # asyncio's own warnings about the closer (a loop dropped unclosed, say)
    # show this name, so it names no private helper.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_resolve("httplike"))
        closer = af._FS_BY_LOOP[loop][af._CLOSER_KEY]
        assert closer.__qualname__ == af._CLOSER_NAME
        assert "_" not in af._CLOSER_NAME
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()


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
