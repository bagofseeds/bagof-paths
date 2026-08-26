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

import os

import typing_extensions as tx

from . import _bridge as bridge
from ._base import BaseWrapper
from ._path import Path
from ._purepath import PurePathMixin


class AsyncPath(PurePathMixin, BaseWrapper):
    """A path-like object exposed through one uniform, async pathlib surface.

    >>> import asyncio
    >>> from bagof.paths import AsyncPath
    >>> async def main() -> None:
    ...     p = AsyncPath("/etc/hostname")
    ...     if await p.exists():
    ...         print(await p.read_text())
    """

    __slots__ = ()

    _family: tx.ClassVar[str] = "async"

    # -- bridge plumbing ---------------------------------------------------
    def _sync(self) -> Path:
        """A synchronous view over the same wrapped driver."""
        return Path(self._wrapped)

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

    # -- reading and writing -----------------------------------------------
    async def open(
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> tx.IO[tx.Any]:
        """Open the path and return a file object (opened in a thread)."""
        return await bridge.run(
            self._sync().open, mode,
            buffering=buffering, encoding=encoding,
            errors=errors, newline=newline,
        )

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
        """Walk the tree, yielding ``(path, dirnames, filenames)`` per dir."""
        sync = self._sync()
        rows = await bridge.run(
            lambda: list(sync.walk(top_down, on_error, follow_symlinks))
        )
        for dirpath, dirnames, filenames in rows:
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
