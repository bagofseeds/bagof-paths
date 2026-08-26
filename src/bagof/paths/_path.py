"""The synchronous path wrapper."""

from __future__ import annotations

import os

import typing_extensions as tx

from . import _drivers, _match
from . import _engine as engine
from ._base import BaseWrapper
from ._spec import BY_NAME


class Path(BaseWrapper):
    """A path-like object exposed through one uniform, pathlib-style surface.

    Construct one around a path string or any path-like object::

        >>> from bagof.paths import Path
        >>> p = Path("/data/sets/train.zarr")
        >>> p.name
        'train.zarr'
        >>> p.parent
        Path('/data/sets')
        >>> (p / "chunks").suffix
        ''

    Each member either delegates to the wrapped object, falls back to a
    synthesized implementation, or raises
    :class:`~bagof.paths.UnsupportedPathOperation`.
    """

    __slots__ = ()

    # -- pure-path properties ----------------------------------------------
    @property
    def name(self) -> str:
        """The final path component."""
        return engine.get(self, BY_NAME["name"])

    @property
    def stem(self) -> str:
        """The final component without its suffix."""
        return engine.get(self, BY_NAME["stem"])

    @property
    def suffix(self) -> str:
        """The final component's last suffix, including the leading dot."""
        return engine.get(self, BY_NAME["suffix"])

    @property
    def suffixes(self) -> tx.List[str]:
        """The final component's suffixes, each including its dot."""
        return engine.get(self, BY_NAME["suffixes"])

    @property
    def parts(self) -> tx.Tuple[str, ...]:
        """The path's components."""
        return engine.get(self, BY_NAME["parts"])

    @property
    def parent(self) -> tx.Self:
        """The logical parent of the path."""
        return engine.get(self, BY_NAME["parent"])

    @property
    def parents(self) -> tx.Tuple[tx.Self, ...]:
        """The logical ancestors of the path, closest first."""
        return engine.get(self, BY_NAME["parents"])

    # -- pure-path methods -------------------------------------------------
    def joinpath(self, *segments: tx.Any) -> tx.Self:
        """Combine the path with each of the given segments."""
        return engine.invoke(self, BY_NAME["joinpath"], segments)

    def with_name(self, name: str) -> tx.Self:
        """A new path with the final component changed to ``name``."""
        return engine.invoke(self, BY_NAME["with_name"], (name,))

    def with_stem(self, stem: str) -> tx.Self:
        """A new path with the stem changed to ``stem``."""
        return engine.invoke(self, BY_NAME["with_stem"], (stem,))

    def with_suffix(self, suffix: str) -> tx.Self:
        """A new path with the suffix changed to ``suffix``."""
        return engine.invoke(self, BY_NAME["with_suffix"], (suffix,))

    def as_posix(self) -> str:
        """The path as a string with forward slashes."""
        return engine.invoke(self, BY_NAME["as_posix"])

    def as_uri(self) -> str:
        """The path as a URI. Requires an absolute path."""
        return engine.invoke(self, BY_NAME["as_uri"])

    def is_absolute(self) -> bool:
        """Whether the path is absolute."""
        return engine.invoke(self, BY_NAME["is_absolute"])

    def is_relative_to(self, other: tx.Any) -> bool:
        """Whether the path is relative to ``other``."""
        return engine.invoke(self, BY_NAME["is_relative_to"], (other,))

    def relative_to(self, other: tx.Any, *, walk_up: bool = False) -> tx.Self:
        """The path made relative to ``other``."""
        return engine.invoke(
            self, BY_NAME["relative_to"], (other,), {"walk_up": walk_up}
        )

    def match(
        self, pattern: str, *, case_sensitive: tx.Optional[bool] = None
    ) -> bool:
        """Whether the path matches ``pattern``, anchored from the right.

        Matching is lexical and consistent across drivers: it runs on the
        canonical path, not on the wrapped object.
        """
        return _match.match(self.path, pattern, case_sensitive=case_sensitive)

    def full_match(
        self, pattern: str, *, case_sensitive: tx.Optional[bool] = None
    ) -> bool:
        """Whether the whole path matches ``pattern`` (``**`` spans segments).

        Uses CPython 3.13's glob semantics on every interpreter.
        """
        return _match.full_match(
            self.path, pattern, case_sensitive=case_sensitive
        )

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
