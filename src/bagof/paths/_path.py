"""The synchronous path wrapper."""

from __future__ import annotations

import os

import typing_extensions as tx

from . import _drivers
from . import _engine as engine
from ._base import BaseWrapper
from ._purepath import PurePathMixin
from ._spec import BY_NAME


class Path(PurePathMixin, BaseWrapper):
    """A path-like object exposed through one uniform, pathlib-style surface.

    Construct one around a path string or any path-like object:

    ```pycon
    >>> from bagof.paths import Path
    >>> p = Path("/data/sets/train.zarr")
    >>> p.name
    'train.zarr'
    >>> p.parent
    Path('/data/sets')
    >>> (p / "chunks").suffix
    ''
    ```

    Each member either delegates to the wrapped object, falls back to a
    synthesized implementation, or raises `UnsupportedPathOperation`.
    """

    __slots__ = ()

    # -- status queries ----------------------------------------------------
    def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists."""
        return engine.invoke(
            self, BY_NAME["exists"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a regular file."""
        return engine.invoke(
            self, BY_NAME["is_file"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a directory."""
        return engine.invoke(
            self, BY_NAME["is_dir"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def is_symlink(self) -> bool:
        """Whether the path is a symbolic link."""
        return engine.invoke(self, BY_NAME["is_symlink"])

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        """The result of ``stat`` on the path."""
        return engine.invoke(
            self, BY_NAME["stat"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def lstat(self) -> os.stat_result:
        """Like :meth:`stat`, without following symbolic links."""
        return engine.invoke(self, BY_NAME["lstat"])

    def samefile(self, other: tx.Any) -> bool:
        """Whether the path and ``other`` refer to the same file."""
        return engine.invoke(self, BY_NAME["samefile"], (other,))

    # -- extended status queries -------------------------------------------
    def is_mount(self) -> bool:
        """Whether the path is a mount point."""
        return engine.invoke(self, BY_NAME["is_mount"])

    def is_socket(self) -> bool:
        """Whether the path is a Unix domain socket."""
        return engine.invoke(self, BY_NAME["is_socket"])

    def is_fifo(self) -> bool:
        """Whether the path is a FIFO (named pipe)."""
        return engine.invoke(self, BY_NAME["is_fifo"])

    def is_block_device(self) -> bool:
        """Whether the path is a block device."""
        return engine.invoke(self, BY_NAME["is_block_device"])

    def is_char_device(self) -> bool:
        """Whether the path is a character device."""
        return engine.invoke(self, BY_NAME["is_char_device"])

    def is_junction(self) -> bool:
        """Whether the path is a junction (a Windows concept; else False)."""
        return engine.invoke(self, BY_NAME["is_junction"])

    # -- reading and writing -----------------------------------------------
    def open(
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> tx.IO[tx.Any]:
        """Open the path and return a file object, like :func:`open`."""
        return engine.invoke(
            self, BY_NAME["open"], (mode,),
            {
                "buffering": buffering,
                "encoding": encoding,
                "errors": errors,
                "newline": newline,
            },
        )

    def read_bytes(self) -> bytes:
        """Read the whole file as bytes."""
        return engine.invoke(self, BY_NAME["read_bytes"])

    def read_text(
        self,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> str:
        """Read the whole file as text."""
        return engine.invoke(
            self, BY_NAME["read_text"], (),
            {"encoding": encoding, "errors": errors, "newline": newline},
        )

    def write_bytes(self, data: tx.Any) -> int:
        """Write ``data`` to the file as bytes, replacing any content."""
        return engine.invoke(self, BY_NAME["write_bytes"], (data,))

    def write_text(
        self,
        data: str,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
    ) -> int:
        """Write ``data`` to the file as text, replacing any content."""
        return engine.invoke(
            self, BY_NAME["write_text"], (data,),
            {"encoding": encoding, "errors": errors, "newline": newline},
        )

    # -- directory iteration -----------------------------------------------
    def iterdir(self) -> tx.Iterator[tx.Self]:
        """Yield the paths of the directory's entries."""
        return engine.invoke(self, BY_NAME["iterdir"])

    def glob(
        self,
        pattern: str,
        *,
        case_sensitive: tx.Optional[bool] = None,
        recurse_symlinks: bool = False,
    ) -> tx.Iterator[tx.Self]:
        """Yield the paths matching ``pattern`` under this directory."""
        return engine.invoke(
            self, BY_NAME["glob"], (pattern,),
            {
                "case_sensitive": case_sensitive,
                "recurse_symlinks": recurse_symlinks,
            },
        )

    def rglob(
        self,
        pattern: str,
        *,
        case_sensitive: tx.Optional[bool] = None,
        recurse_symlinks: bool = False,
    ) -> tx.Iterator[tx.Self]:
        """Like :meth:`glob`, recursively."""
        return engine.invoke(
            self, BY_NAME["rglob"], (pattern,),
            {
                "case_sensitive": case_sensitive,
                "recurse_symlinks": recurse_symlinks,
            },
        )

    # -- creation ----------------------------------------------------------
    def mkdir(
        self, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        """Create a directory at the path."""
        return engine.invoke(
            self, BY_NAME["mkdir"], (),
            {"mode": mode, "parents": parents, "exist_ok": exist_ok},
        )

    def touch(self, mode: int = 0o666, exist_ok: bool = True) -> None:
        """Create the file at the path, or update its modification time."""
        return engine.invoke(
            self, BY_NAME["touch"], (),
            {"mode": mode, "exist_ok": exist_ok},
        )

    # -- removal -----------------------------------------------------------
    def unlink(self, *, missing_ok: bool = False) -> None:
        """Remove the file at the path."""
        return engine.invoke(
            self, BY_NAME["unlink"], (), {"missing_ok": missing_ok}
        )

    def rmdir(self, *, recursive: bool = False) -> None:
        """Remove the directory at the path.

        With ``recursive=True`` the whole tree is removed. The default is
        non-recursive and stays safe even on drivers whose own ``rmdir``
        recurses by default (universal-pathlib).
        """
        _drivers.adapter_for(self._wrapped).rmdir(self, recursive=recursive)

    # -- copying and moving ------------------------------------------------
    def copy(
        self,
        target: tx.Any,
        *,
        follow_symlinks: bool = True,
        preserve_metadata: bool = False,
    ) -> tx.Self:
        """Copy this file or directory to ``target``; return the new path."""
        driver_target = self._coerce_target(target)
        _drivers.adapter_for(self._wrapped).copy(
            self, driver_target,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )
        return self.with_wrapped(driver_target)

    def copy_into(
        self,
        target_dir: tx.Any,
        *,
        follow_symlinks: bool = True,
        preserve_metadata: bool = False,
    ) -> tx.Self:
        """Copy into ``target_dir``, keeping this path's name."""
        dest = self._coerce_target(target_dir) / self.name
        return self.copy(
            dest,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )

    def move(self, target: tx.Any) -> tx.Self:
        """Move this path to ``target``; return the new path."""
        driver_target = self._coerce_target(target)
        _drivers.adapter_for(self._wrapped).move(self, driver_target)
        return self.with_wrapped(driver_target)

    def move_into(self, target_dir: tx.Any) -> tx.Self:
        """Move into ``target_dir``, keeping this path's name."""
        dest = self._coerce_target(target_dir) / self.name
        return self.move(dest)

    # -- traversal ---------------------------------------------------------
    def walk(
        self,
        top_down: bool = True,
        on_error: tx.Optional[tx.Callable] = None,
        follow_symlinks: bool = False,
    ) -> tx.Iterator[tx.Tuple[tx.Self, tx.List[str], tx.List[str]]]:
        """Walk the tree, yielding ``(path, dirnames, filenames)`` per dir."""
        return _drivers.adapter_for(self._wrapped).walk(
            self,
            top_down=top_down,
            on_error=on_error,
            follow_symlinks=follow_symlinks,
        )

    # -- resolving and expanding -------------------------------------------
    def resolve(self, strict: bool = False) -> tx.Self:
        """The absolute path, with symlinks resolved."""
        return engine.invoke(
            self, BY_NAME["resolve"], (), {"strict": strict}
        )

    def absolute(self) -> tx.Self:
        """The absolute path, without resolving symlinks."""
        return engine.invoke(self, BY_NAME["absolute"])

    def expanduser(self) -> tx.Self:
        """The path with a leading ``~`` expanded."""
        return engine.invoke(self, BY_NAME["expanduser"])

    def readlink(self) -> tx.Self:
        """The path a symbolic link points to."""
        return engine.invoke(self, BY_NAME["readlink"])

    def rename(self, target: tx.Any) -> tx.Self:
        """Rename the path to ``target`` and return the new path."""
        return engine.invoke(
            self, BY_NAME["rename"], (self._coerce_target(target),)
        )

    def replace(self, target: tx.Any) -> tx.Self:
        """Rename the path to ``target``, replacing any existing file."""
        return engine.invoke(
            self, BY_NAME["replace"], (self._coerce_target(target),)
        )

    # -- permissions and ownership -----------------------------------------
    def chmod(self, mode: int, *, follow_symlinks: bool = True) -> None:
        """Change the file mode and permission bits."""
        return engine.invoke(
            self, BY_NAME["chmod"], (mode,),
            {"follow_symlinks": follow_symlinks},
        )

    def lchmod(self, mode: int) -> None:
        """Like :meth:`chmod`, without following symbolic links."""
        return engine.invoke(self, BY_NAME["lchmod"], (mode,))

    def owner(self, *, follow_symlinks: bool = True) -> str:
        """The login name of the file's owner."""
        return engine.invoke(
            self, BY_NAME["owner"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def group(self, *, follow_symlinks: bool = True) -> str:
        """The group name of the file."""
        return engine.invoke(
            self, BY_NAME["group"], (),
            {"follow_symlinks": follow_symlinks},
        )

    # -- links -------------------------------------------------------------
    def symlink_to(
        self, target: tx.Any, target_is_directory: bool = False
    ) -> None:
        """Make this path a symbolic link to ``target``."""
        return engine.invoke(
            self, BY_NAME["symlink_to"], (target,),
            {"target_is_directory": target_is_directory},
        )

    def hardlink_to(self, target: tx.Any) -> None:
        """Make this path a hard link to ``target``."""
        return engine.invoke(self, BY_NAME["hardlink_to"], (target,))

    def link_to(self, target: tx.Any) -> None:
        """Make ``target`` a hard link to this path.

        .. deprecated::
           ``link_to`` takes the *reverse* argument order of
           :meth:`hardlink_to` and was removed from ``pathlib`` in Python
           3.12. Prefer :meth:`hardlink_to`; this is kept, and synthesized
           where the driver dropped it, only for backward compatibility.
        """
        return engine.invoke(self, BY_NAME["link_to"], (target,))

    # -- cloud transfer and cache ------------------------------------------
    # Driver-specific (cloudpathlib). They delegate or raise, and return the
    # driver's native result (a URL string, a local destination path).
    def as_url(self, **kwargs: tx.Any) -> str:
        """A URL for the path; keyword arguments pass to the driver."""
        return engine.invoke(self, BY_NAME["as_url"], (), kwargs)

    def download_to(self, destination: tx.Any) -> tx.Any:
        """Download the path's contents to a local ``destination``."""
        return engine.invoke(self, BY_NAME["download_to"], (destination,))

    def upload_from(self, source: tx.Any, **kwargs: tx.Any) -> tx.Any:
        """Upload a local ``source`` to the path."""
        return engine.invoke(
            self, BY_NAME["upload_from"], (source,), kwargs
        )

    def clear_cache(self) -> None:
        """Discard any locally cached copy of the path (cloudpathlib)."""
        return engine.invoke(self, BY_NAME["clear_cache"])

    # -- recursive copy / remove aliases -----------------------------------
    # Familiar names from cloudpathlib and shutil; both route through the
    # adapter-backed copy / rmdir so their divergence is handled in one place.
    def rmtree(self) -> None:
        """Remove the directory tree at the path. Alias of ``rmdir``."""
        self.rmdir(recursive=True)

    def copytree(
        self,
        target: tx.Any,
        *,
        follow_symlinks: bool = True,
        preserve_metadata: bool = False,
    ) -> tx.Self:
        """Copy a directory tree to ``target``. Alias of ``copy``."""
        return self.copy(
            target,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )
