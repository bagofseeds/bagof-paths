"""A natively-async path over an fsspec `AsyncFileSystem`.

This is the driver behind an async cloud path: `AsyncPath("s3://...")` with a
real coroutine I/O surface, rather than a synchronous driver run in a worker
thread. It maps the pathlib surface onto fsspec's own async methods
(`_cat_file`, `_pipe_file`, `_ls`, `_info`, `_walk`, ...).

The path is a **wrapped driver object**, not a wrapper itself: `AsyncPath`
holds one of these in `_wrapped` and reaches it through the native-async
seam. So this class presents the shape that seam expects -- synchronous
lexical members (`name`, `parent`, `/`) and coroutine I/O members whose
names match the pathlib surface.

The filesystem is resolved **per running event loop**, not captured once:
fsspec's own instance cache is keyed by options and thread, not by loop, so a
session built under one loop is dead under another. Each operation asks for the
filesystem for the loop it is running on, building one (with
`skip_instance_cache=True`) the first time and caching it weakly against that
loop.

In async mode s3fs, gcsfs, fsspec's HTTP filesystem and adlfs leave closing
their client session to the caller. So the first filesystem with a session
cached on a loop starts an async generator there, which the loop closes in
`shutdown_asyncgens()` (`asyncio.run` calls it); its `finally` closes every
session the loop's filesystems hold. After that the loop has finished for
this module: an operation on it raises `RuntimeError`. Cloud I/O from another
async generator's cleanup during that shutdown is not supported (it may find
its connection closed, or raise). A loop closed without `shutdown_asyncgens()`
leaks its sessions' connections, as it leaks any other async generator's
cleanup.

Most backends strip the scheme off a URL to get their filesystem path
(`s3://b/k` is `b/k`), but some keep the whole URL as the path -- fsspec's
HTTP filesystem does, since a URL is the only name it has. For such a backend
the lexical members split off the `scheme://authority` *anchor* and work on
the remainder, so `https://h/dir/x` has the parts `("https://h/", "dir",
"x")` and the parent `https://h/dir`, as universal-pathlib gives them. A
trailing slash is kept there too, as an empty last part (`https://h/dir/` has
the name `""` and the parent `https://h/dir`), since it names a different
resource on an HTTP server.

fsspec is an optional dependency, imported lazily: importing this module never
imports fsspec, so the dependency-free core still imports on the 3.8 floor.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import posixpath
import re
import stat as _statmod
import threading
import weakref
from pathlib import PurePosixPath

import typing_extensions as tx

from ._args import is_url, path_text
from ._errors import UnsupportedPathOperation
from ._pathinfo import info_view, listed_facts

# loop -> {(scheme, options-token): AsyncFileSystem}. Weak on the loop so a
# finished loop's filesystem becomes collectable -- unless the filesystem holds
# a client session, which keeps the loop alive (see `_sweep_closed_loops`).
_FS_BY_LOOP: weakref.WeakKeyDictionary[tx.Any, tx.Dict[tx.Any, tx.Any]] = (
    weakref.WeakKeyDictionary()
)
# A per-loop cache holds its build lock under this sentinel key, apart from
# the (scheme, options-token) keys that map to filesystems.
_LOCK_KEY = object()
# ... and, under this one, the async generator that closes the loop's
# client sessions when the loop shuts its async generators down. It is only
# started once a filesystem holding a session is cached on the loop.
_CLOSER_KEY = object()
# The loops whose closer has run: an operation on one is refused.
_FINISHED: weakref.WeakSet[tx.Any] = weakref.WeakSet()
# Guards `_FS_BY_LOOP` and `_FINISHED`, which every thread's loops share.
# Never held across an await; a per-loop cache is only touched on its loop.
_CACHE_LOCK = threading.Lock()
# Where a filesystem keeps its client session: s3fs's client context, the
# aiohttp session of fsspec's HTTP filesystem and of gcsfs, adlfs's async
# Azure service client.
_SESSION_ATTRIBUTES = ("_s3creator", "_session", "service_client")
# What the closer generator is called in asyncio's own warnings about it.
_CLOSER_NAME = "bagof.paths cloud connections"

# filesystem class -> whether its filesystem path keeps the URL scheme.
_KEEPS_SCHEME: tx.Dict[tx.Any, bool] = {}

# The `scheme://authority` a scheme-keeping filesystem path starts with; the
# authority ends at the first "/", "?" or "#" (RFC 3986).
_ANCHOR_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*")


def _freeze(value: tx.Any) -> tx.Any:
    """A hashable stand-in for storage options, for use as a cache key.

    Distinct-but-equal-`repr` objects (two credential objects, say) must
    not collide, or one caller would be handed a filesystem built for
    another's credentials -- so an unhashable leaf falls back to its
    *identity*, which only ever causes a cache miss (a fresh, correct
    filesystem), never a wrong-store hit. A list and a tuple are tagged
    apart, and dict keys are sorted by type-then-repr so a mixed-key dict
    does not raise.
    """
    if isinstance(value, dict):
        items = sorted(
            value.items(), key=lambda kv: (type(kv[0]).__name__, repr(kv[0]))
        )
        return ("dict", tuple((k, _freeze(v)) for k, v in items))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(_freeze(v) for v in value))
    try:
        hash(value)
    except TypeError:
        return ("__id__", id(value))
    return value


def _filesystem_class(scheme: str) -> tx.Any:
    """fsspec's filesystem class for a scheme (raises if none is installed)."""
    import fsspec

    return fsspec.get_filesystem_class(scheme)


def is_async_filesystem(scheme: str) -> bool:
    """Whether the installed fsspec backend for `scheme` is natively async.

    Answered from the class, so it needs no filesystem instance and no event
    loop. A scheme with no installed backend is not async.
    """
    try:
        return bool(getattr(_filesystem_class(scheme), "async_impl", False))
    except (ImportError, ValueError):
        return False


def _strip(scheme: str, url: str) -> str:
    """The filesystem path of a URL, by the backend's own convention.

    Uses the backend's `_strip_protocol` (a classmethod on every fsspec
    filesystem), so `s3://bucket/key` becomes `bucket/key` and a leading
    slash is added or not exactly as that backend expects -- and exactly as
    `UPath` does it, so the two agree on identity. Falls back to a lexical
    strip if the backend cannot answer without an instance.
    """
    try:
        return _filesystem_class(scheme)._strip_protocol(url)
    except Exception:  # pragma: no cover - exotic backend without class strip
        return url.split("://", 1)[1] if "://" in url else url


def _keeps_scheme(scheme: str) -> bool:
    """Whether the backend's filesystem path for `scheme` is the whole URL.

    Most backends strip the scheme (`s3://b/k` becomes `b/k`); fsspec's HTTP
    filesystem keeps it, since a URL is its only name. Probed once per
    backend class from its own `_strip_protocol`, so any backend that keeps
    the scheme is handled, not just HTTP. A scheme with no installed backend
    does not keep it.
    """
    try:
        cls = _filesystem_class(scheme)
    except (ImportError, ValueError):
        return False
    known = _KEEPS_SCHEME.get(cls)
    if known is None:
        prefix = scheme + "://"
        known = _strip(scheme, prefix + "host/key").startswith(prefix)
        _KEEPS_SCHEME[cls] = known
    return known


def _url_options(scheme: str, url: str) -> tx.Dict[str, tx.Any]:
    """The storage options a backend reads out of a URL.

    fsspec's `_get_kwargs_from_urls`, which `fsspec.filesystem` does not
    apply by itself: s3fs, adlfs and gcsfs turn `version_aware` on here
    when the URL names an object version.
    """
    try:
        return dict(_filesystem_class(scheme)._get_kwargs_from_urls(url))
    except Exception:  # pragma: no cover - exotic backend without the hook
        return {}


def _filesystems(per_loop: tx.Mapping[tx.Any, tx.Any]) -> tx.List[tx.Any]:
    """The filesystems in one loop's cache, without its bookkeeping."""
    return [
        fs for key, fs in per_loop.items()
        if key is not _LOCK_KEY and key is not _CLOSER_KEY
    ]


def _holds_session(fs: tx.Any) -> bool:
    """Whether an async filesystem holds, or may open, a client session.

    The attribute is looked for, not its value: gcsfs opens its `_session`
    on its first request, after the filesystem has been cached.
    """
    return any(hasattr(fs, name) for name in _SESSION_ATTRIBUTES)


def _client_session(fs: tx.Any) -> tx.Any:
    """The client session an async filesystem holds now, or None."""
    for name in _SESSION_ATTRIBUTES:
        session = getattr(fs, name, None)
        if session is not None:
            return session
    return None


async def _close_session(fs: tx.Any) -> None:
    """Close the client session an async filesystem holds, on its loop.

    In async mode the backends register no close of their own: they leave
    it to the caller, and an unclosed aiohttp session is reported when it
    is collected.
    """
    session = _client_session(fs)
    if session is None:
        return
    if session is getattr(fs, "_s3creator", None):
        # s3fs's client context (or its region cache): leaving it closes
        # the aiobotocore client and its aiohttp session.
        await session.__aexit__(None, None, None)
        return
    if getattr(session, "closed", False):
        return
    close = getattr(session, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


async def _close_on_shutdown(
    per_loop: tx.Dict[tx.Any, tx.Any],
) -> tx.AsyncIterator[None]:
    """Hold a loop's filesystems; close their sessions when it shuts down.

    Started on a loop when the first filesystem that holds a client session
    is cached there, and kept in that loop's cache, so the loop tracks it as
    a live async generator. `loop.shutdown_asyncgens()` -- which
    `asyncio.run` calls after cancelling the remaining tasks and before
    closing the loop -- closes it, together with every other live
    generator. The `finally` marks the loop finished, takes its cache out of
    service and closes every session its filesystems hold, concurrently, on
    the loop they were opened on. A failed close is passed over, so shutdown
    never raises from here.

    The frame keeps no reference to the loop: the loop is the running one
    when the `finally` runs.
    """
    try:
        yield
    finally:
        loop = asyncio.get_running_loop()
        with _CACHE_LOCK:
            _FINISHED.add(loop)
            if _FS_BY_LOOP.get(loop) is per_loop:
                del _FS_BY_LOOP[loop]
        filesystems = _filesystems(per_loop)
        per_loop.clear()
        await asyncio.gather(
            *[_close_session(fs) for fs in filesystems],
            return_exceptions=True,
        )


def _sweep_closed_loops() -> None:
    """Drop cache entries whose loop has closed.

    The cache is weak on the loop, but a backend's session (aiohttp, on
    s3fs/HTTP) captures the loop, and so does the closer generator, so the
    value keeps the key alive and the weak reference never fires. A loop
    shut down by `asyncio.run` has already closed its sessions and left the
    cache; one closed without `loop.shutdown_asyncgens()` is found here, on
    the next resolve, and only forgotten. Its sessions cannot be closed any
    more -- nothing can be awaited on a closed loop -- so their connections
    leak, as asyncio warns they will when a loop is closed that way.
    """
    with _CACHE_LOCK:
        loops = list(_FS_BY_LOOP.keys())
        for loop in [loop for loop in loops if loop.is_closed()]:
            _FS_BY_LOOP.pop(loop, None)


def _finished(scheme: str) -> RuntimeError:
    """The error for an operation on a loop whose sessions are closed."""
    return RuntimeError(
        f"cannot reach {scheme}:// on this event loop: the loop has "
        "finished, and cloud I/O can no longer open connections on it. Do "
        "the I/O before the loop ends, or on another loop."
    )


def _as_timestamp(value: tx.Any) -> float:
    """A POSIX timestamp from an fsspec mtime (number, datetime, or none)."""
    if isinstance(value, (int, float)):
        return float(value)
    stamp = getattr(value, "timestamp", None)
    if callable(stamp):
        try:
            return float(stamp())
        except Exception:  # pragma: no cover - exotic mtime types
            return 0.0
    return 0.0


def _stat_from_info(info: tx.Mapping[str, tx.Any]) -> os.stat_result:
    """An [os.stat_result][] synthesized from an fsspec info dict.

    Only size, type and modification time are meaningful on an object store;
    the rest are zero, as they are on `UPath`'s own synthesized stat.
    """
    size = int(info.get("size") or 0)
    is_dir = info.get("type") == "directory"
    mode = (_statmod.S_IFDIR | 0o755) if is_dir else (_statmod.S_IFREG | 0o644)
    mtime = _as_timestamp(
        info.get("mtime")
        or info.get("LastModified")
        or info.get("last_modified")
        or info.get("created")
        or 0
    )
    return os.stat_result(
        (mode, 0, 0, 0, 0, 0, size, mtime, mtime, mtime)
    )


async def _keep(
    loop: tx.Any,
    per_loop: tx.Dict[tx.Any, tx.Any],
    token: tx.Any,
    scheme: str,
    fs: tx.Any,
) -> None:
    """Cache a filesystem just built on `loop`, where its session is closed.

    The first filesystem that holds a session starts the loop's closer. One
    built while the closer ran is closed at once, and the operation refused.
    """
    with _CACHE_LOCK:
        live = loop not in _FINISHED and _FS_BY_LOOP.get(loop) is per_loop
    if not live:
        try:
            await _close_session(fs)
        except Exception:  # pragma: no cover - best effort, as at shutdown
            pass
        raise _finished(scheme)
    if _holds_session(fs) and _CLOSER_KEY not in per_loop:
        # The closer runs to its first `yield` without suspending, so no
        # other task sees the cache between the check and the assignment.
        closer = _close_on_shutdown(per_loop)
        closer.__name__ = closer.__qualname__ = _CLOSER_NAME
        await closer.__anext__()
        per_loop[_CLOSER_KEY] = closer
    per_loop[token] = fs


async def _resolve_fs(scheme: str, options: tx.Mapping[str, tx.Any]) -> tx.Any:
    """The async filesystem for this scheme on the running loop."""
    _sweep_closed_loops()
    loop = asyncio.get_running_loop()
    with _CACHE_LOCK:
        if loop in _FINISHED:
            raise _finished(scheme)
        per_loop = _FS_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = _FS_BY_LOOP[loop] = {}
    token = (scheme, _freeze(dict(options)))
    fs = per_loop.get(token)
    if fs is not None:
        return fs
    # One build per (loop, token): without this, a startup gather of N first
    # operations would build N filesystems (and N sessions). setdefault-style
    # lock creation is race-free because there is no await between the get and
    # the assignment. The lock is kept only while a build is waited for:
    # before Python 3.10 an asyncio.Lock holds its loop, so a lock left in the
    # cache would keep the entry alive after the loop is dropped.
    entry = per_loop.get(_LOCK_KEY)
    if entry is None:
        entry = per_loop[_LOCK_KEY] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            fs = per_loop.get(token)
            if fs is None:
                fs = await _build(scheme, options)
                await _keep(loop, per_loop, token, scheme, fs)
    finally:
        entry[1] -= 1
        if entry[1] == 0 and per_loop.get(_LOCK_KEY) is entry:
            del per_loop[_LOCK_KEY]
    return fs


async def _build(scheme: str, options: tx.Mapping[str, tx.Any]) -> tx.Any:
    """A new async filesystem for this scheme, its session started."""
    import fsspec

    # asynchronous=True runs coroutines on the awaiting loop (no loop= to
    # pass); skip_instance_cache=True is mandatory, or fsspec's own cache
    # (keyed by options and thread, not loop) would hand back a filesystem
    # bound to a different loop's session. We set asynchronous/
    # skip_instance_cache ourselves; drop them from the user options so they
    # do not collide.
    safe = {
        k: v for k, v in options.items()
        if k not in ("asynchronous", "skip_instance_cache")
    }
    fs = fsspec.filesystem(
        scheme, asynchronous=True, skip_instance_cache=True, **safe
    )
    # s3fs/HTTP need their aiohttp session started on this loop; the base
    # AsyncFileSystem has no set_session, so only call it when present.
    set_session = getattr(fs, "set_session", None)
    if set_session is not None:  # pragma: no cover - s3fs/HTTP only
        result = set_session()
        if inspect.isawaitable(result):
            await result
    return fs


class _AsyncFSFile:
    """A minimal async file over `_cat_file` / `_pipe_file`.

    fsspec's own `open_async` is per-backend and refuses text modes, so the
    read/write surface is synthesized: a read loads the object once, a write
    buffers and is flushed to the store on close. The buffer is a `StringIO`
    in text mode and a `BytesIO` in binary mode, so a chunked text read
    never splits a multi-byte character.
    """

    def __init__(
        self,
        fs: tx.Any,
        path: str,
        buffer: tx.Any,
        *,
        writable: bool,
        text: bool,
        encoding: str,
        errors: str,
    ) -> None:
        self._fs = fs
        self._path = path
        self._buffer = buffer
        self._writable = writable
        self._text = text
        self._encoding = encoding
        self._errors = errors

    async def read(self, *args: tx.Any) -> tx.Any:
        return self._buffer.read(*args)

    async def readline(self, *args: tx.Any) -> tx.Any:
        return self._buffer.readline(*args)

    async def write(self, data: tx.Any) -> int:
        return self._buffer.write(data)

    async def flush(self) -> None:
        if self._writable:
            data = self._buffer.getvalue()
            if self._text:
                data = data.encode(self._encoding, self._errors)
            await self._fs._pipe_file(self._path, data)

    async def aclose(self) -> None:
        await self.flush()


class AsyncFSPath:
    """A path on an fsspec async filesystem, wrapped by `AsyncPath`."""

    __slots__ = (
        "_scheme",
        "_path",
        "_options",
        "_pure",
        "_anchor",
        "_slash",
        "_relative",
        "_snapshot",
    )

    def __init__(
        self,
        scheme: str,
        path: str,
        options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
        *,
        relative: bool = False,
    ) -> None:
        self._scheme = scheme
        self._options = dict(options or {})
        # A relative path (what `relative_to` returns) names no object on the
        # store until it is joined onto one: it prints, joins and compares as
        # a bare relative path, as universal-pathlib's does. A backend that
        # strips `s3://b/k` to `b/k` has no leading slash to tell the two
        # kinds apart, so the kind is carried here, not read off the path.
        self._relative = relative
        if relative:
            self._anchor = ""
            self._slash = False
            self._pure = PurePosixPath(path)
            self._path = str(self._pure)
            return
        # The `scheme://authority` prefix of a backend whose filesystem path
        # is the whole URL (HTTP), else "". The lexical members run on the
        # remainder only, so `//` and the authority are never parsed as path
        # segments.
        self._anchor = ""
        # Whether an anchored path ends in "/" (an empty last part).
        self._slash = False
        self._pure = PurePosixPath(path)
        match = _ANCHOR_RE.match(path) if _keeps_scheme(scheme) else None
        if match is not None:
            self._anchor = match.group(0)
            rest = path[match.end():]
            if not rest:
                # A bare authority names its root, as universal-pathlib
                # spells it (`https://h` has the path `https://h/`).
                path = self._anchor + "/"
                rest = "/"
            self._pure = PurePosixPath(rest if rest[0] == "/" else "/" + rest)
            # The root counts too: universal-pathlib gives `https://h/` an
            # empty name after its anchor, and the root as its parent.
            self._slash = rest.endswith("/")
        self._path = path
        # What a listing said about this path (its `pathinfo` record), set
        # only on a child a listing yielded; every other path (a derived one
        # included) is built fresh, without one.
        self._snapshot: tx.Any = None

    @classmethod
    def from_url(
        cls,
        url: str,
        scheme: str,
        options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> AsyncFSPath:
        """Build a path from a full URL, stripping it to the fs path.

        The options the backend reads out of the URL itself (`version_aware`
        for an s3 `?versionId=`, ...) come first and the caller's on top,
        as universal-pathlib does, so both drivers reach the same version.
        """
        merged = dict(_url_options(scheme, url))
        merged.update(options or {})
        return cls(scheme, _strip(scheme, url), merged)

    # -- identity / display -------------------------------------------------
    @property
    def protocol(self) -> str:
        return self._scheme

    @property
    def path(self) -> str:
        return self._path

    @property
    def storage_options(self) -> tx.Dict[str, tx.Any]:
        # The live connection mapping, matching the documented accessor: it
        # returns any credentials the path was built with.
        return self._options

    def __str__(self) -> str:
        if self._relative:
            return self._path
        if self._anchor:
            # The filesystem path is already the whole URL.
            return self._path
        # Match UPath's spelling: scheme://<path without a leading slash>.
        return "{}://{}".format(self._scheme, self._path.lstrip("/"))

    def __repr__(self) -> str:
        return f"AsyncFSPath({str(self)!r})"

    def _derive(self, pure: PurePosixPath) -> AsyncFSPath:
        if self._relative and not pure.is_absolute():
            return AsyncFSPath(
                self._scheme, str(pure), self._options, relative=True
            )
        if self._anchor:
            sep = "" if pure.is_absolute() else "/"
            path = self._anchor + sep + str(pure)
            return AsyncFSPath(self._scheme, path, self._options)
        return AsyncFSPath(self._scheme, str(pure), self._options)

    def _child(self, stripped: str) -> AsyncFSPath:
        """A child path from a filesystem path an fsspec method returned."""
        return AsyncFSPath(self._scheme, stripped, self._options)

    def _listed(self, entry: tx.Any, name: tx.Any = None) -> AsyncFSPath:
        """A child from one entry of a detailed listing, with what the
        listing said about it."""
        if not isinstance(entry, dict):  # a backend that ignores detail
            return self._child(entry if name is None else name)
        child = self._child(entry["name"] if name is None else name)
        child._snapshot = listed_facts(entry)
        return child

    # -- lexical members (synchronous, like PurePath) -----------------------
    # A trailing slash on an anchored path is an empty last part, so the
    # name and its pieces are empty and the parent drops only the slash.
    @property
    def name(self) -> str:
        return "" if self._slash else self._pure.name

    @property
    def stem(self) -> str:
        return "" if self._slash else self._pure.stem

    @property
    def suffix(self) -> str:
        return "" if self._slash else self._pure.suffix

    @property
    def suffixes(self) -> tx.List[str]:
        return [] if self._slash else self._pure.suffixes

    @property
    def parts(self) -> tx.Tuple[str, ...]:
        if self._anchor:
            # The anchor is the first part, as universal-pathlib gives it.
            last = ("",) if self._slash else ()
            return (self._anchor + "/",) + self._pure.parts[1:] + last
        return self._pure.parts

    @property
    def parent(self) -> AsyncFSPath:
        if self._slash:
            return self._derive(self._pure)
        return self._derive(self._pure.parent)

    @property
    def parents(self) -> tx.Tuple[AsyncFSPath, ...]:
        parents = tuple(self._derive(p) for p in self._pure.parents)
        if self._slash:
            return (self._derive(self._pure),) + parents
        return parents

    def joinpath(self, *segments: tx.Any) -> AsyncFSPath:
        # A URL segment, or an absolute path of this driver, names a location
        # of its own and restarts the join, as an absolute path does in
        # pathlib; a relative one is joined by its parts.
        result = self
        for segment in segments:
            if isinstance(segment, AsyncFSPath) and not segment._relative:
                self._check_scheme(segment._scheme)
                result = segment
                continue
            text = path_text(segment)
            if is_url(text):
                self._check_scheme(text.split("://", 1)[0])
                result = AsyncFSPath.from_url(
                    text, self._scheme, self._options
                )
            else:
                result = result._derive(result._pure / text)
        return result

    def __truediv__(self, other: tx.Any) -> AsyncFSPath:
        return self.joinpath(other)

    def with_name(self, name: str) -> AsyncFSPath:
        if self._slash:
            # The empty last part is the one replaced.
            return self._derive(self._pure / name)
        return self._derive(self._pure.with_name(name))

    def with_suffix(self, suffix: str) -> AsyncFSPath:
        if self._slash:
            raise ValueError(f"{str(self)!r} has an empty name")
        return self._derive(self._pure.with_suffix(suffix))

    def with_segments(self, *segments: tx.Any) -> AsyncFSPath:
        # A scheme-ful first segment names an absolute path on this backend
        # (a rename/copy target given as a URL); rebuild it through the same
        # stripping the constructor uses, so its path matches this one's.
        texts = [path_text(s) for s in segments]
        if texts and "://" in texts[0]:
            return AsyncFSPath.from_url(
                "/".join(texts), self._scheme, self._options
            )
        return self._derive(PurePosixPath(*texts))

    def as_posix(self) -> str:
        if self._anchor:
            return self._path
        return self._pure.as_posix()

    def as_uri(self) -> str:
        if self._relative:
            raise ValueError("relative path can't be expressed as a URI")
        return str(self)

    def is_absolute(self) -> bool:
        return not self._relative

    def _other_locus(
        self, other: tx.Any
    ) -> tx.Tuple[str, bool, PurePosixPath]:
        """`other` as an (anchor, relative, lexical path) triple.

        A bare, scheme-less text is read under this path's own anchor, as a
        path of this path's own kind.
        """
        if isinstance(other, AsyncFSPath):
            return other._anchor, other._relative, other._pure
        text = path_text(other)
        if "://" in text:
            parsed = AsyncFSPath(self._scheme, _strip(self._scheme, text))
            return parsed._anchor, False, parsed._pure
        return self._anchor, self._relative, PurePosixPath(text)

    def relative_to(self, other: tx.Any, walk_up: bool = False) -> AsyncFSPath:
        anchor, other_relative, base = self._other_locus(other)
        if anchor != self._anchor or other_relative != self._relative:
            # A different authority (another host) is never a prefix, nor is
            # a relative path one of an absolute path, or the other way round.
            raise ValueError(
                f"{str(self)!r} is not relative to {str(other)!r}"
            )
        if walk_up:
            relative = self._pure.relative_to(base, walk_up=True)
        else:
            relative = self._pure.relative_to(base)
        # A relative path of this backend, as universal-pathlib returns: it
        # has no scheme or authority in its string, but keeps its protocol
        # and options for when it is joined back onto a location.
        return AsyncFSPath(
            self._scheme, str(relative), self._options, relative=True
        )

    def is_relative_to(self, other: tx.Any) -> bool:
        try:
            self.relative_to(other)
            return True
        except ValueError:
            return False

    # -- filesystem access --------------------------------------------------
    async def _fs(self) -> tx.Any:
        if self._relative:
            # A relative key would reach whatever the backend reads it as
            # (the root, for an in-memory store): refuse it, as a relative
            # universal-pathlib path refuses I/O.
            raise UnsupportedPathOperation(
                "I/O on a relative path",
                hint=(
                    f"{self._path!r} names no object on the store; join it "
                    "to an absolute path first"
                ),
            )
        return await _resolve_fs(self._scheme, self._options)

    # -- status -------------------------------------------------------------
    async def exists(self, **_: tx.Any) -> bool:
        fs = await self._fs()
        return await fs._exists(self._path)

    async def _kind(self, fs: tx.Any) -> tx.Optional[str]:
        try:
            info = await fs._info(self._path)
        except FileNotFoundError:
            return None
        return info.get("type")

    async def is_file(self, **_: tx.Any) -> bool:
        fs = await self._fs()
        return (await self._kind(fs)) == "file"

    async def is_dir(self, **_: tx.Any) -> bool:
        fs = await self._fs()
        return (await self._kind(fs)) == "directory"

    async def stat(self, **_: tx.Any) -> os.stat_result:
        fs = await self._fs()
        return _stat_from_info(await fs._info(self._path))

    async def _store_facts(self, view: str) -> tx.Dict[str, tx.Any]:
        """One view of this path from one `fs._info()`, for `pathinfo`."""
        fs = await self._fs()
        try:
            info = await fs._info(self._path)
        except (OSError, ValueError):
            return {"exists": False}
        return info_view(info, view) if isinstance(info, dict) else {}

    async def touch(self, **_: tx.Any) -> None:
        # Create an empty object when absent; an object store cannot bump the
        # modification time of one that exists, so that case is a no-op.
        fs = await self._fs()
        if not await fs._exists(self._path):
            await fs._pipe_file(self._path, b"")

    # -- reading and writing ------------------------------------------------
    async def read_bytes(self) -> bytes:
        fs = await self._fs()
        return await fs._cat_file(self._path)

    async def read_text(
        self,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> str:
        data = await self.read_bytes()
        return data.decode(encoding or "utf-8", errors or "strict")

    async def write_bytes(self, data: tx.Any) -> int:
        fs = await self._fs()
        # memoryview rejects an int (as pathlib does), so write_bytes(5) is a
        # TypeError rather than five NUL bytes.
        payload = bytes(memoryview(data))
        await fs._pipe_file(self._path, payload)
        return len(payload)

    async def write_text(
        self,
        data: str,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> int:
        encoded = data.encode(encoding or "utf-8", errors or "strict")
        await self.write_bytes(encoded)
        return len(data)

    async def open(
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
        **_: tx.Any,
    ) -> _AsyncFSFile:
        if "+" in mode:
            raise UnsupportedPathOperation(
                "open(mode='+')",
                driver=self,
                hint="read-write mode is not supported on an async cloud path",
            )
        fs = await self._fs()
        text = "b" not in mode
        writable = any(flag in mode for flag in ("w", "a", "x"))
        enc = encoding or "utf-8"
        err = errors or "strict"
        if "x" in mode and await fs._exists(self._path):
            raise FileExistsError(str(self))
        raw = b""
        if not writable or ("a" in mode and await fs._exists(self._path)):
            raw = await fs._cat_file(self._path)
        if text:
            buffer: tx.Any = io.StringIO(raw.decode(enc, err))
        else:
            buffer = io.BytesIO(raw)
        if "a" in mode:
            buffer.seek(0, io.SEEK_END)
        return _AsyncFSFile(
            fs, self._path, buffer, writable=writable, text=text,
            encoding=enc, errors=err,
        )

    # -- directory iteration ------------------------------------------------
    async def iterdir(self) -> tx.AsyncIterator[AsyncFSPath]:
        fs = await self._fs()
        # The detailed listing costs the same call and keeps each child's
        # type, so a walk over these children asks no child again.
        here = self._path.rstrip("/")
        for entry in await fs._ls(self._path, detail=True):
            name = entry.get("name") if isinstance(entry, dict) else entry
            if str(name).rstrip("/") == here:
                continue  # an object store can list the directory itself
            yield self._listed(entry)

    async def _glob_tree(self) -> tx.Optional[tx.Tuple[tx.Any, ...]]:
        """The subtree below this path in one `fs._find()`, for the shared
        glob: its base, how a path is keyed, and `(parent, key, child)`."""
        fs = await self._fs()
        base = self._path.rstrip("/")
        try:
            found = await fs._find(base, withdirs=True, detail=True)
        except (OSError, ValueError):
            return None
        if not isinstance(found, dict):  # a backend that ignores detail
            return None
        entries = []
        for name, info in found.items():
            name = str(name).rstrip("/")
            if not name.startswith(base + "/"):
                continue  # the directory itself
            parent, _, leaf = name.rpartition("/")
            if leaf not in ("", ".", ".."):
                entries.append((parent, name, self._listed(info, name)))
        return base, _fs_key, entries

    # -- creation / removal -------------------------------------------------
    async def mkdir(self, **kwargs: tx.Any) -> None:
        fs = await self._fs()
        parents = bool(kwargs.get("parents", False))
        exist_ok = bool(kwargs.get("exist_ok", False))
        if await fs._exists(self._path):
            if exist_ok:
                return
            raise FileExistsError(str(self))
        if parents:
            await fs._makedirs(self._path, exist_ok=True)
        else:
            # parents=False must not silently create intermediates.
            await fs._mkdir(self._path, create_parents=False)

    async def unlink(self, *, missing_ok: bool = False) -> None:
        fs = await self._fs()
        try:
            await fs._rm_file(self._path)
        except FileNotFoundError:
            if not missing_ok:
                raise

    async def rmdir(self, *, recursive: bool = False) -> None:
        # recursive is wired through explicitly: fsspec's own _rm defaults to
        # non-recursive, and the wrapper never lets a bare rmdir() delete a
        # whole tree.
        fs = await self._fs()
        await fs._rm(self._path, recursive=recursive)

    # -- moving and copying -------------------------------------------------
    async def rename(self, target: tx.Any) -> AsyncFSPath:
        fs = await self._fs()
        dest = self._as_fs_path(target)
        await fs._mv_file(self._path, dest)
        return self._child(dest)

    # replace has the same effect here: an object store move overwrites.
    replace = rename

    async def copy(self, target: tx.Any, **_: tx.Any) -> AsyncFSPath:
        fs = await self._fs()
        dest = self._as_fs_path(target)
        await fs._cp_file(self._path, dest)
        return self._child(dest)

    async def move(self, target: tx.Any) -> AsyncFSPath:
        return await self.rename(target)

    async def copy_into(self, target_dir: tx.Any, **_: tx.Any) -> AsyncFSPath:
        return await self.copy(self._into(target_dir))

    async def move_into(self, target_dir: tx.Any) -> AsyncFSPath:
        return await self.rename(self._into(target_dir))

    async def absolute(self) -> AsyncFSPath:
        return self

    async def resolve(self, strict: bool = False, **_: tx.Any) -> AsyncFSPath:
        # A remote path has no symlinks, but ``..`` is still collapsed (so the
        # result compares equal to the normalized spelling), and strict still
        # means the path must exist.
        if self._anchor:
            normalized = self._derive(
                PurePosixPath(posixpath.normpath(str(self._pure)))
            )._path
            if self._slash and not normalized.endswith("/"):
                # A directory URL stays one.
                normalized += "/"
        else:
            normalized = posixpath.normpath(self._path)
        result = self if normalized == self._path else self._child(normalized)
        if strict:
            fs = await self._fs()
            if not await fs._exists(result._path):
                raise FileNotFoundError(str(result))
        return result

    async def walk(
        self, top_down: bool = True, *args: tx.Any, **kwargs: tx.Any
    ) -> tx.AsyncIterator[tx.Tuple[AsyncFSPath, tx.List[str], tx.List[str]]]:
        fs = await self._fs()
        # One detailed listing per directory: fsspec classifies each child
        # from it (the names are the keys, in listing order), so no child is
        # asked for its type.
        result = fs._walk(self._path, topdown=top_down, detail=True)
        if inspect.isawaitable(result):
            result = await result
        # A directory the walk enters holds what its parent's listing said.
        listed: tx.Dict[str, tx.Any] = {}

        def row(root: str, dirs: tx.Any, files: tx.Any) -> tx.Any:
            path = self._child(root)
            path._snapshot = listed.pop(root.rstrip("/"), None)
            if isinstance(dirs, dict):
                for info in dirs.values():
                    if isinstance(info, dict) and "name" in info:
                        name = str(info["name"]).rstrip("/")
                        listed[name] = listed_facts(info)
            return path, list(dirs), list(files)

        if hasattr(result, "__aiter__"):  # pragma: no cover - real async _walk
            async for root, dirs, files in result:
                yield row(root, dirs, files)
        else:
            for root, dirs, files in result:
                yield row(root, dirs, files)

    def _check_scheme(self, scheme: str) -> None:
        from ._protocols import canonical_scheme

        if canonical_scheme(scheme) != canonical_scheme(self._scheme):
            raise ValueError(
                f"a target of scheme {scheme!r} names a different store; "
                "pass a path of the same scheme"
            )

    def _as_fs_path(self, target: tx.Any) -> str:
        """A move/copy target as a filesystem path for this backend."""
        if isinstance(target, AsyncFSPath) and not target._relative:
            self._check_scheme(target._scheme)
            return target._path
        text = path_text(target)
        if "://" in text:
            self._check_scheme(text.split("://", 1)[0])
            return _strip(self._scheme, text)
        # A bare name is resolved against this path's parent, like pathlib.
        return self._derive(self._pure.parent / text)._path

    def _into(self, target_dir: tx.Any) -> AsyncFSPath:
        """This path's name placed inside `target_dir` (for *_into)."""
        if isinstance(target_dir, AsyncFSPath) and not target_dir._relative:
            self._check_scheme(target_dir._scheme)
            return target_dir._derive(target_dir._pure / self._pure.name)
        text = path_text(target_dir)
        if "://" in text:
            self._check_scheme(text.split("://", 1)[0])
            base = AsyncFSPath.from_url(text, self._scheme, self._options)
            return base._derive(base._pure / self._pure.name)
        directory = PurePosixPath(text)
        if not directory.is_absolute():
            directory = self._pure.parent / directory
        return self._derive(directory / self._pure.name)


def _fs_key(path: AsyncFSPath) -> str:
    """A path's place in its store, as fsspec's listings name it."""
    return path._path.rstrip("/")
