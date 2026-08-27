"""A natively-async path over an fsspec ``AsyncFileSystem``.

This is the driver behind an async cloud path: ``AsyncPath("s3://...")`` with a
real coroutine I/O surface, rather than a synchronous driver run in a worker
thread. It maps the pathlib surface onto fsspec's own async methods
(``_cat_file``, ``_pipe_file``, ``_ls``, ``_info``, ``_walk``, ...).

The path is a **wrapped driver object**, not a wrapper itself: ``AsyncPath``
holds one of these in ``_wrapped`` and reaches it through the native-async
seam. So this class presents the shape that seam expects -- synchronous
lexical members (``name``, ``parent``, ``/``) and coroutine I/O members whose
names match the pathlib surface.

The filesystem is resolved **per running event loop**, not captured once:
fsspec's own instance cache is keyed by options and thread, not by loop, so a
session built under one loop is dead under another. Each operation asks for the
filesystem for the loop it is running on, building one (with
``skip_instance_cache=True``) the first time and caching it weakly against that
loop.

fsspec is an optional dependency, imported lazily: importing this module never
imports fsspec, so the dependency-free core still imports on the 3.8 floor.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import posixpath
import stat as _statmod
import weakref
from pathlib import PurePosixPath

import typing_extensions as tx

from ._errors import UnsupportedPathOperation

# loop -> {(scheme, options-token): AsyncFileSystem}. Weak on the loop so a
# finished loop's filesystem (and any session it holds) becomes collectable.
_FS_BY_LOOP: weakref.WeakKeyDictionary[tx.Any, tx.Dict[tx.Any, tx.Any]] = (
    weakref.WeakKeyDictionary()
)
# A per-loop cache holds its build lock under this sentinel key, apart from
# the (scheme, options-token) keys that map to filesystems.
_LOCK_KEY = object()


def _freeze(value: tx.Any) -> tx.Any:
    """A hashable stand-in for storage options, for use as a cache key.

    Distinct-but-equal-``repr`` objects (two credential objects, say) must
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
    """Whether the installed fsspec backend for ``scheme`` is natively async.

    Answered from the class, so it needs no filesystem instance and no event
    loop. A scheme with no installed backend is not async.
    """
    try:
        return bool(getattr(_filesystem_class(scheme), "async_impl", False))
    except (ImportError, ValueError):
        return False


def _strip(scheme: str, url: str) -> str:
    """The filesystem path of a URL, by the backend's own convention.

    Uses the backend's ``_strip_protocol`` (a classmethod on every fsspec
    filesystem), so ``s3://bucket/key`` becomes ``bucket/key`` and a leading
    slash is added or not exactly as that backend expects -- and exactly as
    ``UPath`` does it, so the two agree on identity. Falls back to a lexical
    strip if the backend cannot answer without an instance.
    """
    try:
        return _filesystem_class(scheme)._strip_protocol(url)
    except Exception:  # pragma: no cover - exotic backend without class strip
        return url.split("://", 1)[1] if "://" in url else url


def _sweep_closed_loops() -> None:
    """Drop cache entries whose loop has closed.

    The cache is weak on the loop, but a backend's session (aiohttp, on
    s3fs/HTTP) captures the loop, so the value keeps the key alive and the
    weak reference never fires. Sweeping closed loops here -- on the next
    resolve -- releases the filesystem so its session can be finalized.
    """
    for loop in list(_FS_BY_LOOP.keys()):
        if loop.is_closed():
            _FS_BY_LOOP.pop(loop, None)


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
    """An ``os.stat_result`` synthesized from an fsspec info dict.

    Only size, type and modification time are meaningful on an object store;
    the rest are zero, as they are on ``UPath``'s own synthesized stat.
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


async def _resolve_fs(scheme: str, options: tx.Mapping[str, tx.Any]) -> tx.Any:
    """The async filesystem for this scheme on the running loop."""
    import fsspec

    _sweep_closed_loops()
    loop = asyncio.get_running_loop()
    per_loop = _FS_BY_LOOP.get(loop)
    if per_loop is None:
        per_loop = {}
        _FS_BY_LOOP[loop] = per_loop
    token = (scheme, _freeze(dict(options)))
    fs = per_loop.get(token)
    if fs is not None:
        return fs
    # One build per (loop, token): without this, a startup gather of N first
    # operations would build N filesystems (and N sessions). setdefault-style
    # lock creation is race-free because there is no await between the get and
    # the assignment.
    lock = per_loop.get(_LOCK_KEY)
    if lock is None:
        lock = per_loop[_LOCK_KEY] = asyncio.Lock()
    async with lock:
        fs = per_loop.get(token)
        if fs is None:
            # asynchronous=True runs coroutines on the awaiting loop (no loop=
            # to pass); skip_instance_cache=True is mandatory, or fsspec's own
            # cache (keyed by options and thread, not loop) would hand back a
            # filesystem bound to a different loop's session.
            # We set asynchronous/skip_instance_cache ourselves; drop them
            # from the user options so they do not collide.
            safe = {
                k: v for k, v in options.items()
                if k not in ("asynchronous", "skip_instance_cache")
            }
            fs = fsspec.filesystem(
                scheme, asynchronous=True, skip_instance_cache=True, **safe
            )
            # s3fs/HTTP need their aiohttp session started on this loop; the
            # base AsyncFileSystem has no set_session, so only call it when
            # present.
            set_session = getattr(fs, "set_session", None)
            if set_session is not None:  # pragma: no cover - s3fs/HTTP only
                result = set_session()
                if inspect.isawaitable(result):
                    await result
            per_loop[token] = fs
    return fs


class _AsyncFSFile:
    """A minimal async file over ``_cat_file`` / ``_pipe_file``.

    fsspec's own ``open_async`` is per-backend and refuses text modes, so the
    read/write surface is synthesized: a read loads the object once, a write
    buffers and is flushed to the store on close. The buffer is a ``StringIO``
    in text mode and a ``BytesIO`` in binary mode, so a chunked text read
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
    """A path on an fsspec async filesystem, wrapped by ``AsyncPath``."""

    __slots__ = ("_scheme", "_path", "_options", "_pure")

    def __init__(
        self,
        scheme: str,
        path: str,
        options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> None:
        self._scheme = scheme
        self._path = path
        self._options = dict(options or {})
        self._pure = PurePosixPath(path)

    @classmethod
    def from_url(
        cls,
        url: str,
        scheme: str,
        options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> AsyncFSPath:
        """Build a path from a full URL, stripping it to the fs path."""
        return cls(scheme, _strip(scheme, url), options)

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
        # Match UPath's spelling: scheme://<path without a leading slash>.
        return "{}://{}".format(self._scheme, self._path.lstrip("/"))

    def __repr__(self) -> str:
        return f"AsyncFSPath({str(self)!r})"

    def _derive(self, pure: PurePosixPath) -> AsyncFSPath:
        return AsyncFSPath(self._scheme, str(pure), self._options)

    def _child(self, stripped: str) -> AsyncFSPath:
        """A child path from a filesystem path an fsspec method returned."""
        return AsyncFSPath(self._scheme, stripped, self._options)

    # -- lexical members (synchronous, like PurePath) -----------------------
    @property
    def name(self) -> str:
        return self._pure.name

    @property
    def stem(self) -> str:
        return self._pure.stem

    @property
    def suffix(self) -> str:
        return self._pure.suffix

    @property
    def suffixes(self) -> tx.List[str]:
        return self._pure.suffixes

    @property
    def parts(self) -> tx.Tuple[str, ...]:
        return self._pure.parts

    @property
    def parent(self) -> AsyncFSPath:
        return self._derive(self._pure.parent)

    @property
    def parents(self) -> tx.Tuple[AsyncFSPath, ...]:
        return tuple(self._derive(p) for p in self._pure.parents)

    def joinpath(self, *segments: tx.Any) -> AsyncFSPath:
        return self._derive(self._pure.joinpath(*[str(s) for s in segments]))

    def __truediv__(self, other: tx.Any) -> AsyncFSPath:
        return self.joinpath(other)

    def with_name(self, name: str) -> AsyncFSPath:
        return self._derive(self._pure.with_name(name))

    def with_suffix(self, suffix: str) -> AsyncFSPath:
        return self._derive(self._pure.with_suffix(suffix))

    def with_segments(self, *segments: tx.Any) -> AsyncFSPath:
        # A single scheme-ful segment names an absolute path on this backend
        # (a rename/copy target given as a URL); rebuild it through the same
        # stripping the constructor uses, so its path matches this one's.
        if len(segments) == 1 and "://" in str(segments[0]):
            return AsyncFSPath.from_url(
                str(segments[0]), self._scheme, self._options
            )
        return self._derive(PurePosixPath(*[str(s) for s in segments]))

    def as_posix(self) -> str:
        return self._pure.as_posix()

    def as_uri(self) -> str:
        return str(self)

    def is_absolute(self) -> bool:
        return self._pure.is_absolute()

    def _other_pure(self, other: tx.Any) -> PurePosixPath:
        if isinstance(other, AsyncFSPath):
            return other._pure
        text = str(other)
        if "://" in text:
            return PurePosixPath(_strip(self._scheme, text))
        return PurePosixPath(text)

    def relative_to(self, other: tx.Any, walk_up: bool = False) -> AsyncFSPath:
        base = self._other_pure(other)
        if walk_up:
            return self._derive(self._pure.relative_to(base, walk_up=True))
        return self._derive(self._pure.relative_to(base))

    def is_relative_to(self, other: tx.Any) -> bool:
        try:
            self._pure.relative_to(self._other_pure(other))
            return True
        except ValueError:
            return False

    # -- filesystem access --------------------------------------------------
    async def _fs(self) -> tx.Any:
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
        for entry in await fs._ls(self._path, detail=False):
            yield self._child(entry)

    async def glob(
        self, pattern: str, **_: tx.Any
    ) -> tx.AsyncIterator[AsyncFSPath]:
        fs = await self._fs()
        joined = "{}/{}".format(self._path.rstrip("/"), pattern)
        for entry in await fs._glob(joined):
            yield self._child(entry)

    async def rglob(
        self, pattern: str, **_: tx.Any
    ) -> tx.AsyncIterator[AsyncFSPath]:
        fs = await self._fs()
        joined = "{}/**/{}".format(self._path.rstrip("/"), pattern)
        for entry in await fs._glob(joined):
            yield self._child(entry)

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
        result = fs._walk(self._path, topdown=top_down)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "__aiter__"):  # pragma: no cover - real async _walk
            async for root, dirs, files in result:
                yield self._child(root), list(dirs), list(files)
        else:
            for root, dirs, files in result:
                yield self._child(root), list(dirs), list(files)

    def _check_scheme(self, scheme: str) -> None:
        from ._protocols import canonical_scheme

        if canonical_scheme(scheme) != canonical_scheme(self._scheme):
            raise ValueError(
                f"a target of scheme {scheme!r} names a different store; "
                "pass a path of the same scheme"
            )

    def _as_fs_path(self, target: tx.Any) -> str:
        """A move/copy target as a filesystem path for this backend."""
        if isinstance(target, AsyncFSPath):
            self._check_scheme(target._scheme)
            return target._path
        text = str(target)
        if "://" in text:
            self._check_scheme(text.split("://", 1)[0])
            return _strip(self._scheme, text)
        # A bare name is resolved against this path's parent, like pathlib.
        return str(self._pure.parent / text)

    def _into(self, target_dir: tx.Any) -> AsyncFSPath:
        """This path's name placed inside ``target_dir`` (for *_into)."""
        if isinstance(target_dir, AsyncFSPath):
            self._check_scheme(target_dir._scheme)
            return target_dir._derive(target_dir._pure / self._pure.name)
        text = str(target_dir)
        if "://" in text:
            self._check_scheme(text.split("://", 1)[0])
            base = AsyncFSPath.from_url(text, self._scheme, self._options)
            return base._derive(base._pure / self._pure.name)
        directory = PurePosixPath(text)
        if not directory.is_absolute():
            directory = self._pure.parent / directory
        return self._child(str(directory / self._pure.name))
