"""The synchronous path wrapper."""

from __future__ import annotations

import os

import typing_extensions as tx

from . import _drivers, _staged
from . import _engine as engine
from ._base import BaseWrapper
from ._local_copy import LocalCopyMixin
from ._purepath import PurePathMixin
from ._spec import BY_NAME


class Path(PurePathMixin, LocalCopyMixin, BaseWrapper):
    """A path that works like [pathlib.Path][], local or in the cloud.

    Make one from a path string, a URL, or any path object:

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

    Each method uses the underlying library when it can, builds the method
    from simpler ones when it cannot, or raises `UnsupportedPathOperation`
    when neither is possible.
    """

    __slots__ = ()

    # -- status queries ----------------------------------------------------
    def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists.

        On a local path this follows Python 3.14 on every version: an
        error returns False rather than raising. A name too long for the
        file system is not there, and neither is one whose parent you may
        not search. To see the error, call `stat()`, which still raises.
        The other status checks (`is_file`, `is_dir`, `is_symlink`, ...)
        do the same.

        `follow_symlinks=False` (also on `is_file` and `is_dir`) asks about
        a symbolic link itself, on every Python version.

        ```pycon
        >>> Path("a" * 300).exists()
        False
        ```
        """
        return engine.invoke(
            self, BY_NAME["exists"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a regular file.

        Like `exists`, it returns False on an error on a local path.
        """
        return engine.invoke(
            self, BY_NAME["is_file"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a directory.

        Like `exists`, it returns False on an error on a local path.
        """
        return engine.invoke(
            self, BY_NAME["is_dir"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def is_symlink(self) -> bool:
        """Whether the path is a symbolic link."""
        return engine.invoke(self, BY_NAME["is_symlink"])

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        """The result of `stat` on the path."""
        return engine.invoke(
            self, BY_NAME["stat"], (),
            {"follow_symlinks": follow_symlinks},
        )

    def lstat(self) -> os.stat_result:
        """Like `stat`, without following symbolic links."""
        return engine.invoke(self, BY_NAME["lstat"])

    def samefile(self, other: tx.Any, *, strict: bool = True) -> bool:
        """Whether the path and `other` refer to the same file.

        As in `pathlib`, both files must exist. With `strict=False` they
        need not: the answer is whether writing one would write the other,
        which is what a writer checks before it replaces the file it reads.

        ```pycon
        >>> import tempfile
        >>> from bagof.paths import Path
        >>> folder = Path(tempfile.mkdtemp())
        >>> (folder / "sub").mkdir()
        >>> new = folder / "new.bin"
        >>> new.samefile(folder / "sub" / ".." / "new.bin", strict=False)
        True
        >>> new.samefile(folder / "other.bin", strict=False)
        False
        ```

        A local path resolves symbolic links and `..`, and a hard link to an
        existing file is the same file. A name that does not exist yet
        compares as it is written below its nearest existing directory, so
        on a case-insensitive file system (macOS) two spellings of a new
        name count as different. A remote path is the same file as another
        remote path when the two are equal, and never the same as a local
        one.
        """
        if not strict:
            return self._names_same_file(other)
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
        *,
        atomic: bool = False,
    ) -> tx.IO[tx.Any]:
        """Open the path and return a file object, like `open`.

        With `atomic=True` (a writing mode only) the file is written in a
        staging area and moved onto the path when it is closed, so readers
        see the old content or the new, never a half-written file; leaving
        a `with` block with an error discards it. An appending or `r+`
        mode starts from a copy of the current content. See
        [`staged`][bagof.paths.Path.staged].
        """
        if atomic:
            copy = _staged.check_atomic_mode(mode)
            if "x" in mode and self.exists():
                raise _staged.exists_error(self)
            stage = self.staged(copy=copy)
            try:
                handle = stage.path.open(
                    mode, buffering=buffering, encoding=encoding,
                    errors=errors, newline=newline,
                )
            except BaseException:
                stage.discard()
                raise
            return _staged.AtomicFile(handle, stage)
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

    def write_bytes(self, data: tx.Any, *, atomic: bool = False) -> int:
        """Write `data` to the file as bytes, replacing any content.

        With `atomic=True` the new content is written beside the file and
        moved over it in one step, so readers never see a partial file
        (see [`staged`][bagof.paths.Path.staged]).
        """
        if atomic:
            with self.staged() as staged:
                return staged.write_bytes(data)
        return engine.invoke(self, BY_NAME["write_bytes"], (data,))

    def write_text(
        self,
        data: str,
        encoding: tx.Optional[str] = None,
        errors: tx.Optional[str] = None,
        newline: tx.Optional[str] = None,
        *,
        atomic: bool = False,
    ) -> int:
        """Write `data` to the file as text, replacing any content.

        `atomic=True` works as in
        [`write_bytes`][bagof.paths.Path.write_bytes].
        """
        if atomic:
            with self.staged() as staged:
                return staged.write_text(
                    data, encoding=encoding, errors=errors, newline=newline
                )
        return engine.invoke(
            self, BY_NAME["write_text"], (data,),
            {"encoding": encoding, "errors": errors, "newline": newline},
        )

    # -- staged writes -----------------------------------------------------
    def staged(self, *, copy: bool = False) -> _staged.Stage:
        """A staging area for a new version of this path.

        Create a file or a directory at the staged path; on leaving the
        `with` block it replaces this path. An error inside the block
        leaves this path as it was, and staging nothing changes nothing.

        ```pycon
        >>> import tempfile
        >>> from bagof.paths import Path
        >>> target = Path(tempfile.mkdtemp()) / "scan.txt"
        >>> with target.staged() as tmp:
        ...     tmp.name == target.name
        ...     _ = tmp.write_text("new")
        ...     target.exists()
        True
        False
        >>> target.read_text()
        'new'
        ```

        The staged path has the same name as this one, so a library that
        saves by file name (and reads the extension) works on it. It does
        not exist until your code creates it, as a file or a directory:

        ```python
        with Path("out/scan.nii.gz").staged() as tmp:
            nibabel.save(image, tmp)

        with Path("s3://bucket/store.zarr").staged() as tmp:
            zarr.save(tmp, data)       # a local directory, uploaded on exit
        ```

        `copy=True` starts the staged version as a copy of this path (a
        file, or a whole directory), for jobs that update it in place: zarr
        in `"r+"` mode, h5py in `"a"` mode, appending. On a remote path that
        is a full download. If this path does not exist, nothing is copied.

        Outside a `with` block, call `commit()` or `discard()` on the
        returned [`Stage`][bagof.paths.Stage]; its `path` is the staged path.

        What committing guarantees:

        - **A file** replaces a local file in one atomic step on POSIX
          (readers that have the old file open, even memory-mapped, keep
          reading it), and a remote object in one upload.
        - **A directory**, or a change between file and directory, is
          all-or-nothing but not atomic: the old version is moved aside and
          the new one moved in, so it is briefly missing. On a remote path it
          is uploaded file by file, and readers can see a mixture meanwhile.
        - If committing fails, this path is left as it was (or restored), the
          staged data is kept, and [`CommitError`][bagof.paths.CommitError]
          says where.

        The staging area is a hidden directory beside this path (beside the
        file a symbolic link names), so the move stays on one file system; a
        remote path is staged in a local temporary directory. A crash can
        leave the hidden directory behind. Replacing a file gives it a new
        inode, so other hard links keep the old content; the old file's
        permissions are kept. On Windows, the replacement fails while
        another process has the file open.
        """
        return _staged.Stage(self, copy=copy)

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
        """Yield the paths matching `pattern` under this directory."""
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
        """Like `glob`, recursively."""
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

        With `recursive=True` the whole tree is removed. The default is
        non-recursive and stays safe even on drivers whose own `rmdir`
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
        """Copy this file or directory to `target`; return the new path."""
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
        """Copy into `target_dir`, keeping this path's name.

        Within the same store the copy keeps the store's own name for the
        object, query and all, so two objects whose names differ only after
        a `?` stay apart. Into another store it takes `name`, which leaves
        the query out.
        """
        dest = self._into(target_dir)
        return self.copy(
            dest,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )

    def move(self, target: tx.Any) -> tx.Self:
        """Move this path to `target`; return the new path."""
        driver_target = self._coerce_target(target)
        _drivers.adapter_for(self._wrapped).move(self, driver_target)
        return self.with_wrapped(driver_target)

    def move_into(self, target_dir: tx.Any) -> tx.Self:
        """Move into `target_dir`, keeping this path's name.

        The name follows the same rule as in `copy_into`.
        """
        return self.move(self._into(target_dir))

    def _into(self, target_dir: tx.Any) -> tx.Any:
        """This path's name placed in `target_dir`, as a driver path."""
        directory = self._coerce_target(target_dir)
        if self._same_store(directory):
            return directory / self._store_name()
        return directory / self.name

    # -- traversal ---------------------------------------------------------
    def walk(
        self,
        top_down: bool = True,
        on_error: tx.Optional[tx.Callable] = None,
        follow_symlinks: bool = False,
    ) -> tx.Iterator[tx.Tuple[tx.Self, tx.List[str], tx.List[str]]]:
        """Walk the tree, yielding `(path, dirnames, filenames)` per dir."""
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
        """The path with a leading `~` expanded."""
        return engine.invoke(self, BY_NAME["expanduser"])

    def readlink(self) -> tx.Self:
        """The path a symbolic link points to."""
        return engine.invoke(self, BY_NAME["readlink"])

    def rename(self, target: tx.Any) -> tx.Self:
        """Rename the path to `target` and return the new path."""
        return engine.invoke(
            self, BY_NAME["rename"], (self._coerce_target(target),)
        )

    def replace(self, target: tx.Any) -> tx.Self:
        """Rename the path to `target`, replacing any existing file."""
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
        """Like `chmod`, without following symbolic links."""
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
        """Make this path a symbolic link to `target`."""
        return engine.invoke(
            self, BY_NAME["symlink_to"], (target,),
            {"target_is_directory": target_is_directory},
        )

    def hardlink_to(self, target: tx.Any) -> None:
        """Make this path a hard link to `target`."""
        return engine.invoke(self, BY_NAME["hardlink_to"], (target,))

    def link_to(self, target: tx.Any) -> None:
        """Make `target` a hard link to this path.

        .. deprecated::
           `link_to` takes the *reverse* argument order of
           `hardlink_to` and was removed from `pathlib` in Python
           3.12. Prefer `hardlink_to`; this is kept, and synthesized
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
        """Download the path's contents to a local `destination`."""
        return engine.invoke(self, BY_NAME["download_to"], (destination,))

    def upload_from(self, source: tx.Any, **kwargs: tx.Any) -> tx.Any:
        """Upload a local `source` to the path."""
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
        """Remove the directory tree at the path. Alias of `rmdir`."""
        self.rmdir(recursive=True)

    def copytree(
        self,
        target: tx.Any,
        *,
        follow_symlinks: bool = True,
        preserve_metadata: bool = False,
    ) -> tx.Self:
        """Copy a directory tree to `target`. Alias of `copy`."""
        return self.copy(
            target,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )
