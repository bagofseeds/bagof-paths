"""The asynchronous path wrapper.

``AsyncPath`` exposes the same surface as :class:`~bagof.paths.Path`, but its
concrete, I/O-touching members are coroutines. It wraps a *synchronous* driver
(``pathlib``, ``UPath``, ``cloudpathlib``) by running that driver's blocking
work on a sync view of the path in a worker thread, so the event loop is never
stalled and the whole synchronous implementation -- engine, fallbacks,
adapters -- is reused rather than written a second time. The pure-path
(lexical) members are inherited unchanged: they never block, so they stay
synchronous, exactly as on ``Path``.

Wrapping a *natively asynchronous* driver (one whose methods are coroutines)
is not yet supported and is a planned addition; today such a driver's methods
would be called through the sync view and return un-awaited coroutines.
"""

from __future__ import annotations

import inspect
import os

import typing_extensions as tx

from . import _bridge as bridge
from ._base import BaseWrapper
from ._errors import UnsupportedPathOperation
from ._path import Path
from ._purepath import PurePathMixin

_WALK_DONE = object()


def _is_async_driver(wrapped: tx.Any) -> bool:
    """Whether a wrapped object's members are coroutines (unsupported)."""
    for name in ("exists", "open", "iterdir", "stat"):
        member = getattr(wrapped, name, None)
        if member is not None and (
            inspect.iscoroutinefunction(member)
            or inspect.isasyncgenfunction(member)
        ):
            return True
    return False


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


class AsyncPath(PurePathMixin, BaseWrapper):
    """A path-like object exposed through one uniform, async pathlib surface.

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

    def __init__(self, path: tx.Any, *, driver: tx.Any = None) -> None:
        super().__init__(path, driver=driver)
        if _is_async_driver(self._wrapped):
            raise UnsupportedPathOperation(
                "AsyncPath over an async driver",
                driver=self._wrapped,
                hint=(
                    "this driver is natively asynchronous, which AsyncPath "
                    "does not yet support; wrap a synchronous driver"
                ),
            )

    # -- bridge plumbing ---------------------------------------------------
    def _sync(self) -> Path:
        """A synchronous view over the same wrapped driver."""
        return self._sync_type(self._wrapped)

    def _wrap(self, result: tx.Any) -> tx.Any:
        """Re-wrap a sync path result as an async one of this path's type."""
        if isinstance(result, Path):
            return self.with_wrapped(result._wrapped)
        return result

    # -- status queries ----------------------------------------------------
    async def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists."""
        return await bridge.run(
            self._sync().exists, follow_symlinks=follow_symlinks
        )

    async def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a regular file."""
        return await bridge.run(
            self._sync().is_file, follow_symlinks=follow_symlinks
        )

    async def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a directory."""
        return await bridge.run(
            self._sync().is_dir, follow_symlinks=follow_symlinks
        )

    async def is_symlink(self) -> bool:
        """Whether the path is a symbolic link."""
        return await bridge.run(self._sync().is_symlink)

    async def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        """The result of ``stat`` on the path."""
        return await bridge.run(
            self._sync().stat, follow_symlinks=follow_symlinks
        )

    async def lstat(self) -> os.stat_result:
        """Like :meth:`stat`, without following symbolic links."""
        return await bridge.run(self._sync().lstat)

    async def samefile(self, other: tx.Any) -> bool:
        """Whether the path and ``other`` refer to the same file."""
        return await bridge.run(self._sync().samefile, other)

    # -- extended status queries -------------------------------------------
    async def is_mount(self) -> bool:
        """Whether the path is a mount point."""
        return await bridge.run(self._sync().is_mount)

    async def is_socket(self) -> bool:
        """Whether the path is a Unix domain socket."""
        return await bridge.run(self._sync().is_socket)

    async def is_fifo(self) -> bool:
        """Whether the path is a FIFO (named pipe)."""
        return await bridge.run(self._sync().is_fifo)

    async def is_block_device(self) -> bool:
        """Whether the path is a block device."""
        return await bridge.run(self._sync().is_block_device)

    async def is_char_device(self) -> bool:
        """Whether the path is a character device."""
        return await bridge.run(self._sync().is_char_device)

    async def is_junction(self) -> bool:
        """Whether the path is a junction (a Windows concept; else False)."""
        return await bridge.run(self._sync().is_junction)

    # -- reading and writing -----------------------------------------------
    async def open(
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> AsyncFile:
        """Open the path and return an async file object.

        The handle's own reads and writes are each a thread hop, so they do
        not block the event loop. Use it with ``async with`` / ``async for``.
        """
        handle = await bridge.run(
            self._sync().open, mode,
            buffering=buffering, encoding=encoding,
            errors=errors, newline=newline,
        )
        return AsyncFile(handle)

    async def read_bytes(self) -> bytes:
        """Read the whole file as bytes."""
        return await bridge.run(self._sync().read_bytes)

    async def read_text(
        self,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> str:
        """Read the whole file as text."""
        return await bridge.run(
            self._sync().read_text,
            encoding=encoding, errors=errors, newline=newline,
        )

    async def write_bytes(self, data: tx.Any) -> int:
        """Write ``data`` to the file as bytes, replacing any content."""
        return await bridge.run(self._sync().write_bytes, data)

    async def write_text(
        self,
        data: str,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> int:
        """Write ``data`` to the file as text, replacing any content."""
        return await bridge.run(
            self._sync().write_text, data,
            encoding=encoding, errors=errors, newline=newline,
        )

    # -- directory iteration -----------------------------------------------
    async def iterdir(self) -> tx.AsyncIterator[tx.Self]:
        """Yield the paths of the directory's entries."""
        sync = self._sync()
        for item in await bridge.run(lambda: list(sync.iterdir())):
            yield self._wrap(item)

    async def glob(
        self,
        pattern: str,
        *,
        case_sensitive: tx.Optional[bool] = None,
        recurse_symlinks: bool = False,
    ) -> tx.AsyncIterator[tx.Self]:
        """Yield the paths matching ``pattern`` under this directory."""
        sync = self._sync()
        items = await bridge.run(
            lambda: list(
                sync.glob(
                    pattern,
                    case_sensitive=case_sensitive,
                    recurse_symlinks=recurse_symlinks,
                )
            )
        )
        for item in items:
            yield self._wrap(item)

    async def rglob(
        self,
        pattern: str,
        *,
        case_sensitive: tx.Optional[bool] = None,
        recurse_symlinks: bool = False,
    ) -> tx.AsyncIterator[tx.Self]:
        """Like :meth:`glob`, recursively."""
        sync = self._sync()
        items = await bridge.run(
            lambda: list(
                sync.rglob(
                    pattern,
                    case_sensitive=case_sensitive,
                    recurse_symlinks=recurse_symlinks,
                )
            )
        )
        for item in items:
            yield self._wrap(item)

    # -- creation ----------------------------------------------------------
    async def mkdir(
        self, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """Create a directory at the path."""
        await bridge.run(
            self._sync().mkdir, mode, parents=parents, exist_ok=exist_ok
        )

    async def touch(self, mode: int = 0o666, exist_ok: bool = True) -> None:
        """Create the file at the path, or update its modification time."""
        await bridge.run(self._sync().touch, mode, exist_ok=exist_ok)

    # -- removal -----------------------------------------------------------
    async def unlink(self, *, missing_ok: bool = False) -> None:
        """Remove the file at the path."""
        await bridge.run(self._sync().unlink, missing_ok=missing_ok)

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
        return self._wrap(await bridge.run(self._sync().resolve, strict))

    async def absolute(self) -> tx.Self:
        """The absolute path, without resolving symlinks."""
        return self._wrap(await bridge.run(self._sync().absolute))

    async def expanduser(self) -> tx.Self:
        """The path with a leading ``~`` expanded."""
        return self._wrap(await bridge.run(self._sync().expanduser))

    async def readlink(self) -> tx.Self:
        """The path a symbolic link points to."""
        return self._wrap(await bridge.run(self._sync().readlink))

    async def rename(self, target: tx.Any) -> tx.Self:
        """Rename the path to ``target`` and return the new path."""
        return self._wrap(await bridge.run(self._sync().rename, target))

    async def replace(self, target: tx.Any) -> tx.Self:
        """Rename the path to ``target``, replacing any existing file."""
        return self._wrap(await bridge.run(self._sync().replace, target))

    # -- permissions and ownership -----------------------------------------
    async def chmod(self, mode: int, *, follow_symlinks: bool = True) -> None:
        """Change the file mode and permission bits."""
        await bridge.run(
            self._sync().chmod, mode, follow_symlinks=follow_symlinks
        )

    async def lchmod(self, mode: int) -> None:
        """Like :meth:`chmod`, without following symbolic links."""
        await bridge.run(self._sync().lchmod, mode)

    async def owner(self, *, follow_symlinks: bool = True) -> str:
        """The login name of the file's owner."""
        return await bridge.run(
            self._sync().owner, follow_symlinks=follow_symlinks
        )

    async def group(self, *, follow_symlinks: bool = True) -> str:
        """The group name of the file."""
        return await bridge.run(
            self._sync().group, follow_symlinks=follow_symlinks
        )

    # -- links -------------------------------------------------------------
    async def symlink_to(
        self, target: tx.Any, target_is_directory: bool = False
    ) -> None:
        """Make this path a symbolic link to ``target``."""
        await bridge.run(
            self._sync().symlink_to, target,
            target_is_directory=target_is_directory,
        )

    async def hardlink_to(self, target: tx.Any) -> None:
        """Make this path a hard link to ``target``."""
        await bridge.run(self._sync().hardlink_to, target)

    async def link_to(self, target: tx.Any) -> None:
        """Make ``target`` a hard link to this path.

        .. deprecated::
           ``link_to`` takes the *reverse* argument order of
           :meth:`hardlink_to` and was removed from ``pathlib`` in Python
           3.12. Prefer :meth:`hardlink_to`; this is kept, and synthesized
           where the driver dropped it, only for backward compatibility.
        """
        await bridge.run(self._sync().link_to, target)

    # -- cloud transfer and cache ------------------------------------------
    async def as_url(self, **kwargs: tx.Any) -> str:
        """A URL for the path; keyword arguments pass to the driver."""
        return await bridge.run(self._sync().as_url, **kwargs)

    async def download_to(self, destination: tx.Any) -> tx.Any:
        """Download the path's contents to a local ``destination``."""
        return await bridge.run(self._sync().download_to, destination)

    async def upload_from(self, source: tx.Any, **kwargs: tx.Any) -> tx.Any:
        """Upload a local ``source`` to the path."""
        return self._wrap(
            await bridge.run(self._sync().upload_from, source, **kwargs)
        )

    async def clear_cache(self) -> None:
        """Discard any locally cached copy of the path (cloudpathlib)."""
        await bridge.run(self._sync().clear_cache)

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
