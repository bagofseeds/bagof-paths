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
import weakref
from pathlib import PurePosixPath

import typing_extensions as tx

# loop -> {(scheme, options-token): AsyncFileSystem}. Weak on the loop so a
# finished loop's filesystem (and any session it holds) becomes collectable.
_FS_BY_LOOP: weakref.WeakKeyDictionary[tx.Any, tx.Dict[tx.Any, tx.Any]] = (
    weakref.WeakKeyDictionary()
)


def _freeze(value: tx.Any) -> tx.Any:
    """A hashable stand-in for storage options, for use as a cache key."""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
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


async def _resolve_fs(scheme: str, options: tx.Mapping[str, tx.Any]) -> tx.Any:
    """The async filesystem for this scheme on the running loop."""
    import fsspec

    loop = asyncio.get_running_loop()
    per_loop = _FS_BY_LOOP.get(loop)
    if per_loop is None:
        per_loop = {}
        _FS_BY_LOOP[loop] = per_loop
    token = (scheme, _freeze(dict(options)))
    fs = per_loop.get(token)
    if fs is None:
        # asynchronous=True runs coroutines on the awaiting loop (no loop= to
        # pass); skip_instance_cache=True is mandatory, or fsspec's own cache
        # (keyed by options and thread, not loop) would hand back a filesystem
        # bound to a different loop's session.
        fs = fsspec.filesystem(
            scheme, asynchronous=True, skip_instance_cache=True, **options
        )
        # s3fs/HTTP need their aiohttp session started on this loop; the base
        # AsyncFileSystem has no set_session, so only call it when present.
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
    buffers and is flushed to the store on close.
    """

    def __init__(
        self,
        fs: tx.Any,
        path: str,
        buffer: io.BytesIO,
        *,
        writable: bool,
        text: bool,
        encoding: tx.Optional[str],
        errors: tx.Optional[str],
        newline: tx.Optional[str],
    ) -> None:
        self._fs = fs
        self._path = path
        self._buffer = buffer
        self._writable = writable
        self._text = text
        self._encoding = encoding or "utf-8"
        self._errors = errors or "strict"
        self._newline = newline

    def _decode(self, data: bytes) -> str:
        return data.decode(self._encoding, self._errors)

    async def read(self, *args: tx.Any) -> tx.Any:
        data = self._buffer.read(*args)
        return self._decode(data) if self._text else data

    async def readline(self, *args: tx.Any) -> tx.Any:
        line = self._buffer.readline(*args)
        return self._decode(line) if self._text else line

    async def write(self, data: tx.Any) -> int:
        if self._text:
            data = data.encode(self._encoding, self._errors)
        return self._buffer.write(data)

    async def flush(self) -> None:
        if self._writable:
            await self._fs._pipe_file(self._path, self._buffer.getvalue())

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

    def is_absolute(self) -> bool:
        return self._pure.is_absolute()

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
        payload = bytes(data)
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

    async def open(self, mode: str = "r", **_: tx.Any) -> _AsyncFSFile:
        fs = await self._fs()
        text = "b" not in mode
        writable = any(flag in mode for flag in ("w", "a", "x"))
        if writable:
            buffer = io.BytesIO()
            if "a" in mode and await fs._exists(self._path):
                buffer.write(await fs._cat_file(self._path))
        else:
            buffer = io.BytesIO(await fs._cat_file(self._path))
        return _AsyncFSFile(
            fs, self._path, buffer, writable=writable, text=text,
            encoding=None, errors=None, newline=None,
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
        exist_ok = bool(kwargs.get("exist_ok", False))
        await fs._makedirs(self._path, exist_ok=exist_ok)

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

    async def resolve(self, **_: tx.Any) -> AsyncFSPath:
        # A remote path is already absolute; there are no symlinks to resolve.
        return self

    async def walk(
        self, *args: tx.Any, **kwargs: tx.Any
    ) -> tx.AsyncIterator[tx.Tuple[AsyncFSPath, tx.List[str], tx.List[str]]]:
        fs = await self._fs()
        result = fs._walk(self._path)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "__aiter__"):  # pragma: no cover - real async _walk
            async for root, dirs, files in result:
                yield self._child(root), list(dirs), list(files)
        else:
            for root, dirs, files in result:
                yield self._child(root), list(dirs), list(files)

    def _as_fs_path(self, target: tx.Any) -> str:
        """A move/copy target as a filesystem path for this backend."""
        if isinstance(target, AsyncFSPath):
            return target._path
        text = str(target)
        if "://" in text:
            return _strip(self._scheme, text)
        # A bare name is resolved against this path's parent, like pathlib.
        return str(self._pure.parent / text)

    def _into(self, target_dir: tx.Any) -> AsyncFSPath:
        """This path's name placed inside ``target_dir`` (for *_into)."""
        if isinstance(target_dir, AsyncFSPath):
            return target_dir._derive(target_dir._pure / self._pure.name)
        base = PurePosixPath(str(target_dir))
        return self._child(str(base / self._pure.name))
