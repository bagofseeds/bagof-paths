"""The asynchronous path wrapper.

``AsyncPath`` exposes the same surface as :class:`~bagof.paths.Path`, but its
concrete, I/O-touching members are coroutines. It handles two kinds of driver:

- a **synchronous** driver (``pathlib``, ``UPath``, ``cloudpathlib``) is run on
  a sync view of the path in a worker thread, so the event loop is never
  stalled and the whole synchronous implementation -- engine, fallbacks,
  adapters -- is reused rather than written a second time;
- a **natively asynchronous** driver (one whose methods are coroutines, such
  as ``anyio.Path``) is awaited directly, with no thread. For the few members
  such a driver does not have (``copy``, ``walk``, ...), a local stdlib view is
  run in a thread as a fallback, so the whole surface still works.

The pure-path (lexical) members are inherited unchanged: they never block, so
they stay synchronous, exactly as on ``Path``.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path as LocalPath

import typing_extensions as tx

from . import _bridge as bridge
from . import _engine as engine
from ._base import BaseWrapper
from ._constants import LOCAL_PROTOCOLS
from ._detect import is_async_driver as _is_async_driver
from ._errors import UnsupportedPathOperation
from ._path import Path
from ._purepath import PurePathMixin
from ._spec import BY_NAME

_WALK_DONE = object()


class AsyncFile:
    """An async view over a synchronous file object.

    Each operation is one thread hop, so reads and writes on a blocking
    driver handle never stall the event loop. Use it as an async context
    manager and iterate it with ``async for``.
    """

    def __init__(self, handle: tx.IO[tx.Any]) -> None:
        self._handle = handle

    async def read(self, *args: tx.Any) -> tx.Any:
        return await bridge.run(self._handle.read, *args)

    async def readline(self, *args: tx.Any) -> tx.Any:
        return await bridge.run(self._handle.readline, *args)

    async def write(self, data: tx.Any) -> int:
        return await bridge.run(self._handle.write, data)

    async def flush(self) -> None:
        await bridge.run(self._handle.flush)

    async def close(self) -> None:
        await bridge.run(self._handle.close)

    async def __aenter__(self) -> AsyncFile:
        return self

    async def __aexit__(self, *exc: tx.Any) -> None:
        await self.close()

    def __aiter__(self) -> AsyncFile:
        return self

    async def __anext__(self) -> tx.Any:
        line = await bridge.run(self._handle.readline)
        if not line:
            raise StopAsyncIteration
        return line


class _NativeAsyncFile:
    """An async view over a file handle that is *already* asynchronous.

    A native driver's ``open`` returns a handle whose reads and writes are
    coroutines (``anyio``), or an async context manager that yields one
    (``aiopath``). Either way its operations are awaited directly, with no
    thread. The context manager, when there is one, is held so that closing
    finalizes it.
    """

    def __init__(self, handle: tx.Any, cm: tx.Any = None) -> None:
        self._handle = handle
        self._cm = cm

    async def read(self, *args: tx.Any) -> tx.Any:
        return await self._handle.read(*args)

    async def readline(self, *args: tx.Any) -> tx.Any:
        return await self._handle.readline(*args)

    async def write(self, data: tx.Any) -> int:
        return await self._handle.write(data)

    async def flush(self) -> None:
        flush = getattr(self._handle, "flush", None)
        if flush is not None:
            result = flush()
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            return
        # Prefer the async close; never await a plain sync close (some drivers
        # leak the underlying object's sync close through delegation).
        aclose = getattr(self._handle, "aclose", None)
        if aclose is not None:
            result = aclose()
            if inspect.isawaitable(result):
                await result

    async def __aenter__(self) -> _NativeAsyncFile:
        return self

    async def __aexit__(self, *exc: tx.Any) -> None:
        await self.close()

    def __aiter__(self) -> _NativeAsyncFile:
        return self

    async def __anext__(self) -> tx.Any:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


async def _adapt_async_file(opened: tx.Any) -> _NativeAsyncFile:
    """Turn what a native driver's ``open`` returned into an async file."""
    if inspect.isawaitable(opened):
        # anyio: a coroutine that resolves to the handle.
        return _NativeAsyncFile(await opened)
    if hasattr(opened, "__aenter__") and not hasattr(opened, "read"):
        # aiopath: an async context manager; enter it and keep it to close.
        handle = await opened.__aenter__()
        return _NativeAsyncFile(handle, cm=opened)
    return _NativeAsyncFile(opened)


class AsyncPath(PurePathMixin, BaseWrapper):
    """The ``await`` version of :class:`Path`: the same methods, as coroutines.

    The parts that only describe a path (``name``, ``parent``, ``/``) stay
    plain; the parts that touch storage are awaited.

    ```pycon
    >>> import asyncio
    >>> from bagof.paths import AsyncPath
    >>> async def main() -> None:
    ...     p = AsyncPath("/etc/hostname")
    ...     if await p.exists():
    ...         print(await p.read_text())
    ```
    """

    __slots__ = ()

    _family: tx.ClassVar[str] = "async"
    # An AsyncPath subclass that customizes behavior sets this to its paired
    # sync Path subclass, so those overrides are honored inside the worker
    # thread where the synchronous implementation actually runs.
    _sync_type: tx.ClassVar[tx.Type[Path]] = Path

    # -- delegation plumbing ----------------------------------------------
    def _sync(self) -> Path:
        """A synchronous view to run in a thread.

        For a sync driver, a view over the driver itself. For a native async
        driver there is no such view, so a local stdlib view over the same
        filesystem path stands in -- used only for the members the driver
        lacks. A non-local async driver has no local stand-in.
        """
        wrapped = self._wrapped
        if _is_async_driver(wrapped):
            if self.protocol in LOCAL_PROTOCOLS:
                return self._sync_type(LocalPath(os.fspath(wrapped)))
            raise UnsupportedPathOperation(
                "this operation",
                driver=wrapped,
                hint=(
                    "this async driver has no synchronous view; only the "
                    "methods it implements itself are available"
                ),
            )
        return self._sync_type(wrapped)

    def _wrap(self, result: tx.Any) -> tx.Any:
        """Re-wrap a sync path result as an async one of this path's type."""
        if isinstance(result, Path):
            driver = result._wrapped
            if _is_async_driver(self._wrapped) and not isinstance(
                driver, type(self._wrapped)
            ):
                # A result from the local stdlib fallback view: put it back in
                # the native driver's family so derived paths stay native.
                driver = type(self._wrapped)(str(driver))
            return self.with_wrapped(driver)
        return result

    async def _call(
        self,
        name: str,
        args: tx.Tuple[tx.Any, ...] = (),
        kwargs: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> tx.Any:
        """Run one engine member: await the driver, or bridge a sync view."""
        kwargs = dict(kwargs or {})
        if _is_async_driver(self._wrapped):
            method = getattr(self._wrapped, name, None)
            if method is not None:
                member = BY_NAME[name]
                call_kwargs = engine._drop_defaults(kwargs, member.normalize)
                call_args = tuple(engine._unwrap(a) for a in args)
                result = method(*call_args, **call_kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return engine._finish(self, result, member.result)
        # Sync driver, or a native driver that lacks this member.
        result = await bridge.run(getattr(self._sync(), name), *args, **kwargs)
        return self._wrap(result)

    async def _aiter(
        self,
        name: str,
        args: tx.Tuple[tx.Any, ...] = (),
        kwargs: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> tx.AsyncIterator[tx.Self]:
        """Iterate a directory member: async on the driver, or bridged."""
        kwargs = dict(kwargs or {})
        if _is_async_driver(self._wrapped):
            method = getattr(self._wrapped, name, None)
            if method is not None:
                member = BY_NAME[name]
                call_kwargs = engine._drop_defaults(kwargs, member.normalize)
                call_args = tuple(engine._unwrap(a) for a in args)
                iterator = method(*call_args, **call_kwargs)
                if inspect.isawaitable(iterator):
                    iterator = await iterator
                if hasattr(iterator, "__aiter__"):
                    async for item in iterator:
                        yield self.with_wrapped(item)
                else:
                    for item in iterator:
                        yield self.with_wrapped(item)
                return
        sync = self._sync()
        items = await bridge.run(
            lambda: list(getattr(sync, name)(*args, **kwargs))
        )
        for item in items:
            yield self._wrap(item)

    # -- status queries ----------------------------------------------------
    async def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists."""
        return await self._call(
            "exists", (), {"follow_symlinks": follow_symlinks}
        )

    async def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a regular file."""
        return await self._call(
            "is_file", (), {"follow_symlinks": follow_symlinks}
        )

    async def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a directory."""
        return await self._call(
            "is_dir", (), {"follow_symlinks": follow_symlinks}
        )

    async def is_symlink(self) -> bool:
        """Whether the path is a symbolic link."""
        return await self._call("is_symlink")

    async def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        """The result of ``stat`` on the path."""
        return await self._call(
            "stat", (), {"follow_symlinks": follow_symlinks}
        )

    async def lstat(self) -> os.stat_result:
        """Like :meth:`stat`, without following symbolic links."""
        return await self._call("lstat")

    async def samefile(self, other: tx.Any) -> bool:
        """Whether the path and ``other`` refer to the same file."""
        return await self._call("samefile", (other,))

    # -- extended status queries -------------------------------------------
    async def is_mount(self) -> bool:
        """Whether the path is a mount point."""
        return await self._call("is_mount")

    async def is_socket(self) -> bool:
        """Whether the path is a Unix domain socket."""
        return await self._call("is_socket")

    async def is_fifo(self) -> bool:
        """Whether the path is a FIFO (named pipe)."""
        return await self._call("is_fifo")

    async def is_block_device(self) -> bool:
        """Whether the path is a block device."""
        return await self._call("is_block_device")

    async def is_char_device(self) -> bool:
        """Whether the path is a character device."""
        return await self._call("is_char_device")

    async def is_junction(self) -> bool:
        """Whether the path is a junction (a Windows concept; else False)."""
        return await self._call("is_junction")

    # -- reading and writing -----------------------------------------------
    async def open(
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> tx.Any:
        """Open the path and return an async file object.

        Use it with ``async with`` / ``async for``. The handle's reads and
        writes are awaited directly on a native driver, or run in a worker
        thread on a synchronous one.
        """
        kwargs = {
            "buffering": buffering, "encoding": encoding,
            "errors": errors, "newline": newline,
        }
        if _is_async_driver(self._wrapped):
            driver_open = getattr(self._wrapped, "open", None)
            if driver_open is not None:
                call_kwargs = engine._drop_defaults(
                    kwargs, BY_NAME["open"].normalize
                )
                opened = driver_open(mode, **call_kwargs)
                return await _adapt_async_file(opened)
        handle = await bridge.run(
            self._sync().open, mode,
            buffering=buffering, encoding=encoding,
            errors=errors, newline=newline,
        )
        return AsyncFile(handle)

    async def read_bytes(self) -> bytes:
        """Read the whole file as bytes."""
        return await self._call("read_bytes")

    async def read_text(
        self,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> str:
        """Read the whole file as text."""
        return await self._call(
            "read_text", (),
            {"encoding": encoding, "errors": errors, "newline": newline},
        )

    async def write_bytes(self, data: tx.Any) -> int:
        """Write ``data`` to the file as bytes, replacing any content."""
        return await self._call("write_bytes", (data,))

    async def write_text(
        self,
        data: str,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> int:
        """Write ``data`` to the file as text, replacing any content."""
        return await self._call(
            "write_text", (data,),
            {"encoding": encoding, "errors": errors, "newline": newline},
        )

    # -- directory iteration -----------------------------------------------
    async def iterdir(self) -> tx.AsyncIterator[tx.Self]:
        """Yield the paths of the directory's entries."""
        async for item in self._aiter("iterdir"):
            yield item

    async def glob(
        self,
        pattern: str,
        *,
        case_sensitive: tx.Optional[bool] = None,
        recurse_symlinks: bool = False,
    ) -> tx.AsyncIterator[tx.Self]:
        """Yield the paths matching ``pattern`` under this directory."""
        async for item in self._aiter(
            "glob", (pattern,),
            {
                "case_sensitive": case_sensitive,
                "recurse_symlinks": recurse_symlinks,
            },
        ):
            yield item

    async def rglob(
        self,
        pattern: str,
        *,
        case_sensitive: tx.Optional[bool] = None,
        recurse_symlinks: bool = False,
    ) -> tx.AsyncIterator[tx.Self]:
        """Like :meth:`glob`, recursively."""
        async for item in self._aiter(
            "rglob", (pattern,),
            {
                "case_sensitive": case_sensitive,
                "recurse_symlinks": recurse_symlinks,
            },
        ):
            yield item

    # -- creation ----------------------------------------------------------
    async def mkdir(
        self, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """Create a directory at the path."""
        await self._call(
            "mkdir", (),
            {"mode": mode, "parents": parents, "exist_ok": exist_ok},
        )

    async def touch(self, mode: int = 0o666, exist_ok: bool = True) -> None:
        """Create the file at the path, or update its modification time."""
        await self._call("touch", (), {"mode": mode, "exist_ok": exist_ok})

    # -- removal -----------------------------------------------------------
    async def unlink(self, *, missing_ok: bool = False) -> None:
        """Remove the file at the path."""
        await self._call("unlink", (), {"missing_ok": missing_ok})

    async def rmdir(self, *, recursive: bool = False) -> None:
        """Remove the directory at the path."""
        await bridge.run(self._sync().rmdir, recursive=recursive)

    # -- copying and moving ------------------------------------------------
    async def copy(
        self,
        target: tx.Any,
        *,
        follow_symlinks: bool = True,
        preserve_metadata: bool = False,
    ) -> tx.Self:
        """Copy this file or directory to ``target``; return the new path."""
        return self._wrap(
            await bridge.run(
                self._sync().copy, target,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
            )
        )

    async def copy_into(
        self,
        target_dir: tx.Any,
        *,
        follow_symlinks: bool = True,
        preserve_metadata: bool = False,
    ) -> tx.Self:
        """Copy into ``target_dir``, keeping this path's name."""
        return self._wrap(
            await bridge.run(
                self._sync().copy_into, target_dir,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
            )
        )

    async def move(self, target: tx.Any) -> tx.Self:
        """Move this path to ``target``; return the new path."""
        return self._wrap(await bridge.run(self._sync().move, target))

    async def move_into(self, target_dir: tx.Any) -> tx.Self:
        """Move into ``target_dir``, keeping this path's name."""
        return self._wrap(
            await bridge.run(self._sync().move_into, target_dir)
        )

    # -- traversal ---------------------------------------------------------
    async def walk(
        self,
        top_down: bool = True,
        on_error: tx.Optional[tx.Callable] = None,
        follow_symlinks: bool = False,
    ) -> tx.AsyncIterator[tx.Tuple[tx.Self, tx.List[str], tx.List[str]]]:
        """Walk the tree, yielding ``(path, dirnames, filenames)`` per dir.

        One directory per thread hop, so mutating ``dirnames`` in place to
        prune the descent works exactly as it does on the sync wrapper. Note
        that ``on_error`` runs in a worker thread.
        """
        rows = self._sync().walk(top_down, on_error, follow_symlinks)
        while True:
            row = await bridge.run(next, rows, _WALK_DONE)
            if row is _WALK_DONE:
                return
            dirpath, dirnames, filenames = row
            yield self._wrap(dirpath), dirnames, filenames

    # -- resolving and expanding -------------------------------------------
    async def resolve(self, strict: bool = False) -> tx.Self:
        """The absolute path, with symlinks resolved."""
        return await self._call("resolve", (), {"strict": strict})

    async def absolute(self) -> tx.Self:
        """The absolute path, without resolving symlinks."""
        return await self._call("absolute")

    async def expanduser(self) -> tx.Self:
        """The path with a leading ``~`` expanded."""
        return await self._call("expanduser")

    async def readlink(self) -> tx.Self:
        """The path a symbolic link points to."""
        return await self._call("readlink")

    async def rename(self, target: tx.Any) -> tx.Self:
        """Rename the path to ``target`` and return the new path."""
        return await self._call("rename", (self._coerce_target(target),))

    async def replace(self, target: tx.Any) -> tx.Self:
        """Rename the path to ``target``, replacing any existing file."""
        return await self._call("replace", (self._coerce_target(target),))

    # -- permissions and ownership -----------------------------------------
    async def chmod(self, mode: int, *, follow_symlinks: bool = True) -> None:
        """Change the file mode and permission bits."""
        await self._call(
            "chmod", (mode,), {"follow_symlinks": follow_symlinks}
        )

    async def lchmod(self, mode: int) -> None:
        """Like :meth:`chmod`, without following symbolic links."""
        await self._call("lchmod", (mode,))

    async def owner(self, *, follow_symlinks: bool = True) -> str:
        """The login name of the file's owner."""
        return await self._call(
            "owner", (), {"follow_symlinks": follow_symlinks}
        )

    async def group(self, *, follow_symlinks: bool = True) -> str:
        """The group name of the file."""
        return await self._call(
            "group", (), {"follow_symlinks": follow_symlinks}
        )

    # -- links -------------------------------------------------------------
    async def symlink_to(
        self, target: tx.Any, target_is_directory: bool = False
    ) -> None:
        """Make this path a symbolic link to ``target``."""
        await self._call(
            "symlink_to", (target,),
            {"target_is_directory": target_is_directory},
        )

    async def hardlink_to(self, target: tx.Any) -> None:
        """Make this path a hard link to ``target``."""
        await self._call("hardlink_to", (target,))

    async def link_to(self, target: tx.Any) -> None:
        """Make ``target`` a hard link to this path.

        .. deprecated::
           ``link_to`` takes the *reverse* argument order of
           :meth:`hardlink_to` and was removed from ``pathlib`` in Python
           3.12. Prefer :meth:`hardlink_to`; this is kept, and synthesized
           where the driver dropped it, only for backward compatibility.
        """
        await self._call("link_to", (target,))

    # -- cloud transfer and cache ------------------------------------------
    async def as_url(self, **kwargs: tx.Any) -> str:
        """A URL for the path; keyword arguments pass to the driver."""
        return await self._call("as_url", (), kwargs)

    async def download_to(self, destination: tx.Any) -> tx.Any:
        """Download the path's contents to a local ``destination``."""
        return await self._call("download_to", (destination,))

    async def upload_from(self, source: tx.Any, **kwargs: tx.Any) -> tx.Any:
        """Upload a local ``source`` to the path."""
        return await self._call("upload_from", (source,), kwargs)

    async def clear_cache(self) -> None:
        """Discard any locally cached copy of the path (cloudpathlib)."""
        await self._call("clear_cache")

    # -- recursive copy / remove aliases -----------------------------------
    async def rmtree(self) -> None:
        """Remove the directory tree at the path. Alias of ``rmdir``."""
        await self.rmdir(recursive=True)

    async def copytree(
        self,
        target: tx.Any,
        *,
        follow_symlinks: bool = True,
        preserve_metadata: bool = False,
    ) -> tx.Self:
        """Copy a directory tree to ``target``. Alias of ``copy``."""
        return await self.copy(
            target,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )
