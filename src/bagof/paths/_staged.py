"""Staged writes: build a new version beside the target, then commit it.

`Path.staged()` gives a staging area: a private directory with an entry named
like the target, which does not exist yet. The caller creates a file or a
directory there; committing moves whatever was created onto the target, and
discarding removes it, leaving the target as it was.

For a local target the staging directory sits beside the target (resolved
through symbolic links), so the final move stays on one file system: a file
is replaced with one `os.replace` (atomic on POSIX), and a directory, or a
change of type, by moving the old entry aside into the staging directory and
the new one in (all-or-nothing, but not atomic). For a remote target the
staging directory is local and committing uploads its content: a file in one
request, a directory file by file.

The design and the measurements behind it are recorded in
`design/path-wrapper.md` (§5, "Staged writes").
"""

from __future__ import annotations

import errno
import os
import posixpath
import shutil
import stat
import tempfile
import weakref
from pathlib import Path as LocalPath

import typing_extensions as tx

from . import _bridge as bridge
from ._detect import is_async_driver
from ._errors import CommitError

_OPEN = "open"
_COMMITTED = "committed"
_DISCARDED = "discarded"
_FAILED = "failed"

# Streamed copies move this much at a time.
_CHUNK = 1 << 20


# -- the staging directory ---------------------------------------------------
def _make_staging_dir(parent: tx.Optional[str], name: str) -> str:
    """A new, private staging directory: hidden, named after the target."""
    try:
        return tempfile.mkdtemp(prefix=f".{name}.staging-", dir=parent)
    except OSError as error:
        if error.errno != errno.ENAMETOOLONG:
            raise
        # The target's name leaves no room for the prefix; the staged entry
        # inside still carries the full name.
        return tempfile.mkdtemp(prefix=".staging-", dir=parent)


def _remove(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _resolve_local(target: tx.Any) -> tx.Tuple[str, str, str]:
    """`(real target, parent, name)` for a local target, links followed."""
    real = os.path.realpath(os.fspath(target))
    parent, name = os.path.split(real)
    return real, parent, name


# -- local: copy in, commit out -----------------------------------------------
def _is_dir(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _fill_local(real: str, entry: str) -> None:
    """Start the staged entry as a copy of the local target, if it exists."""
    if _is_dir(real):
        shutil.copytree(real, entry, symlinks=True)
    elif os.path.lexists(real):
        shutil.copy2(real, entry)


def _fsync_file(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - an unreadable file stays unsynced
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - fsync on a read-only fd (Windows)
        pass
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    if os.name == "nt":  # pragma: no cover - directories cannot be fsynced
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - not every file system allows it
        pass
    finally:
        os.close(fd)


def _commit_local(entry: str, real: str, staging: str) -> None:
    """Move what exists at `entry` onto the local target `real`."""
    if not os.path.lexists(entry):
        return
    new_is_dir = _is_dir(entry)
    old_exists = os.path.lexists(real)
    old_is_dir = old_exists and _is_dir(real)
    if not new_is_dir and not old_is_dir:
        # A file over a file (or over nothing): one atomic replace.
        if old_exists and os.path.isfile(real):
            mode = stat.S_IMODE(os.stat(real).st_mode)
            os.chmod(entry, mode)
        _fsync_file(entry)
        os.replace(entry, real)
        _fsync_dir(os.path.dirname(real))
        return
    # A directory, or a change of type: move the old entry aside (into the
    # staging directory, so on the same file system), then the new one in.
    aside = os.path.join(tempfile.mkdtemp(dir=staging), "old")
    if old_exists:
        os.rename(real, aside)
    try:
        os.rename(entry, real)
    except BaseException:
        if old_exists:
            os.rename(aside, real)
        raise
    _fsync_dir(os.path.dirname(real))


# -- remote: download in, upload out ------------------------------------------
def _relative(child: str, base: str) -> str:
    """`child`'s location below `base`, as a POSIX string ("" at the base)."""
    return child[len(base):].strip("/")


def _join(base: tx.Any, relative: str) -> tx.Any:
    """`base / relative`, joined through the driver (keys stay whole)."""
    return base.with_wrapped(base._wrapped / relative)


def _remote_kind(target: tx.Any) -> tx.Optional[str]:
    if target.is_dir():
        return "dir"
    if target.is_file():
        return "file"
    return None


def _download_file(source: tx.Any, local: str) -> None:
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with source.open("rb") as stream, open(local, "wb") as out:
        shutil.copyfileobj(stream, out, _CHUNK)


def _fill_remote(target: tx.Any, entry: str) -> None:
    """Start the staged entry as a download of the remote target."""
    kind = _remote_kind(target)
    if kind == "file":
        _download_file(target, entry)
    elif kind == "dir":
        os.makedirs(entry, exist_ok=True)
        base = str(target).rstrip("/")
        for directory, _, files in target.walk():
            where = _relative(str(directory).rstrip("/"), base)
            for name in files:
                _download_file(
                    _join(directory, name),
                    os.path.join(entry, *where.split("/"), name),
                )


def _upload_file(local: str, target: tx.Any) -> None:
    """Upload one local file over `target`, never leaving a partial object.

    The driver's own upload is used where there is one: universal-pathlib's
    filesystem `put_file` (s3fs/gcsfs/adlfs create the object only when the
    upload completes), cloudpathlib's `upload_from`. Anything else gets the
    whole content in one `write_bytes`.
    """
    wrapped = target._wrapped
    put_file = getattr(getattr(wrapped, "fs", None), "put_file", None)
    if callable(put_file) and isinstance(getattr(wrapped, "path", None), str):
        put_file(local, wrapped.path)
        return
    upload_from = getattr(wrapped, "upload_from", None)
    if callable(upload_from):
        upload_from(local, force_overwrite_to_cloud=True)
        return
    with open(local, "rb") as stream:
        target.write_bytes(stream.read())


def _local_files(root: str) -> tx.List[str]:
    """Every file below `root`, as POSIX paths relative to it."""
    found = []
    for directory, _, files in os.walk(root):
        where = os.path.relpath(directory, root)
        for name in files:
            relative = name if where == "." else posixpath.join(
                where.replace(os.sep, "/"), name
            )
            found.append(relative)
    return found


def _remote_files(base: tx.Any) -> tx.Set[str]:
    """Every file below the remote directory `base`, relative to it."""
    found = set()
    root = str(base).rstrip("/")
    for directory, _, files in base.walk():
        where = _relative(str(directory).rstrip("/"), root)
        for name in files:
            found.add(posixpath.join(where, name) if where else name)
    return found


def _commit_remote(entry: str, target: tx.Any) -> None:
    """Upload what exists at `entry` over the remote target."""
    if not os.path.lexists(entry):
        return
    old = _remote_kind(target)
    if not _is_dir(entry):
        if old == "dir":
            target.rmdir(recursive=True)
        _upload_file(entry, target)
        return
    if old == "file":
        target.unlink()
    base = target._query_free()
    new = _local_files(entry)
    for relative in new:
        _upload_file(os.path.join(entry, *relative.split("/")),
                     _join(base, relative))
    if old == "dir":
        for stale in sorted(_remote_files(base) - set(new)):
            _join(base, stale).unlink()


# -- remote, natively async ---------------------------------------------------
async def _aremote_kind(target: tx.Any) -> tx.Optional[str]:
    if await target.is_dir():
        return "dir"
    if await target.is_file():
        return "file"
    return None


def _write_local(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        out.write(data)


def _read_local(path: str) -> bytes:
    with open(path, "rb") as stream:
        return stream.read()


async def _afill_remote(target: tx.Any, entry: str) -> None:
    kind = await _aremote_kind(target)
    if kind == "file":
        await bridge.run(_write_local, entry, await target.read_bytes())
    elif kind == "dir":
        await bridge.run(os.makedirs, entry, exist_ok=True)
        base = str(target).rstrip("/")
        async for directory, _, files in target.walk():
            where = _relative(str(directory).rstrip("/"), base)
            for name in files:
                data = await _join(directory, name).read_bytes()
                local = os.path.join(entry, *where.split("/"), name)
                await bridge.run(_write_local, local, data)


async def _aremote_files(base: tx.Any) -> tx.Set[str]:
    found = set()
    root = str(base).rstrip("/")
    async for directory, _, files in base.walk():
        where = _relative(str(directory).rstrip("/"), root)
        for name in files:
            found.add(posixpath.join(where, name) if where else name)
    return found


async def _acommit_remote(entry: str, target: tx.Any) -> None:
    if not await bridge.run(os.path.lexists, entry):
        return
    old = await _aremote_kind(target)
    if not await bridge.run(_is_dir, entry):
        if old == "dir":
            await target.rmdir(recursive=True)
        # One request: the native driver writes the object whole.
        await target.write_bytes(await bridge.run(_read_local, entry))
        return
    if old == "file":
        await target.unlink()
    base = target._query_free()
    new = await bridge.run(_local_files, entry)
    for relative in new:
        local = os.path.join(entry, *relative.split("/"))
        await _join(base, relative).write_bytes(
            await bridge.run(_read_local, local)
        )
    if old == "dir":
        for stale in sorted(await _aremote_files(base) - set(new)):
            await _join(base, stale).unlink()


# -- the staging areas ------------------------------------------------------
class _StageBase:
    """What a synchronous and an asynchronous staging area share."""

    def __init__(self, target: tx.Any, copy: bool) -> None:
        self._target = target
        self._copy = copy
        self._state = _OPEN
        self._dir: tx.Optional[str] = None
        self._entry: tx.Optional[str] = None
        self._real: tx.Optional[str] = None
        self._local = target.is_local()
        self._finalizer: tx.Optional[weakref.finalize] = None
        self._path: tx.Any = None

    def _create(self) -> None:
        """Make the staging directory (blocking)."""
        if self._local:
            self._real, parent, name = _resolve_local(self._target)
            if name and not os.path.isdir(parent):
                # As pathlib would, name the path being written, not the
                # staging directory that could not be made beside it.
                code = errno.ENOENT
                raise FileNotFoundError(
                    code, os.strerror(code), str(self._target)
                )
        else:
            parent, name = None, self._target.name
        if not name:
            raise ValueError(
                f"cannot stage {str(self._target)!r}: it names no file or "
                "directory"
            )
        self._dir = _make_staging_dir(parent, name)
        self._finalizer = weakref.finalize(self, _remove, self._dir)
        self._entry = os.path.join(self._dir, name)
        self._path = self._target.with_wrapped(LocalPath(self._entry))

    def _cleanup(self) -> None:
        if self._finalizer is not None:
            self._finalizer.detach()
        if self._dir is not None:
            _remove(self._dir)

    def _check_open(self) -> None:
        if self._state == _COMMITTED:
            raise ValueError(
                f"the staged version of {str(self._target)!r} was already "
                "committed"
            )
        if self._state == _DISCARDED:
            raise ValueError(
                f"the staged version of {str(self._target)!r} was discarded"
            )

    def _failed(self, error: BaseException) -> CommitError:
        # The target is as it was (or restored); keep the staged data, past
        # garbage collection too, so the work is not lost.
        self._state = _FAILED
        if self._finalizer is not None:
            self._finalizer.detach()
        return CommitError(self._target, self._entry, error)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self._target)!r})"


class Stage(_StageBase):
    """A staging area for a new version of a path.

    Returned by [`Path.staged`][bagof.paths.Path.staged]; use it as a
    context manager, or call `commit` or `discard` yourself. The staging
    directory exists from the moment the stage is made.
    """

    def __init__(self, target: tx.Any, *, copy: bool = False) -> None:
        super().__init__(target, copy)
        self._create()
        if copy:
            try:
                self._fill()
            except BaseException:
                self._cleanup()
                raise

    @property
    def path(self) -> tx.Any:
        """Where to create the new version: named like the target.

        A local path, even for a remote target. It does not exist until
        your code creates a file or a directory there (or `copy=True`
        started it as a copy of the target).
        """
        return self._path

    def _fill(self) -> None:
        if self._local:
            _fill_local(self._real, self._entry)
        else:
            _fill_remote(self._target, self._entry)

    def commit(self) -> None:
        """Move what was created at `path` onto the target.

        Nothing created means nothing changes. If the move or the upload
        fails, the target is left as it was (or restored), the staged data
        is kept, and [`CommitError`][bagof.paths.CommitError] says where;
        `commit` may be called again, or `discard` removes it.
        """
        self._check_open()
        try:
            if self._local:
                _commit_local(self._entry, self._real, self._dir)
            else:
                _commit_remote(self._entry, self._target)
        except Exception as error:
            raise self._failed(error) from error
        self._state = _COMMITTED
        self._cleanup()

    def discard(self) -> None:
        """Remove the staged version; the target is never touched.

        Discarding after a commit does nothing.
        """
        if self._state == _COMMITTED:
            return
        self._state = _DISCARDED
        self._cleanup()

    def __enter__(self) -> tx.Any:
        return self.path

    def __exit__(self, exc_type: tx.Any, exc: tx.Any, tb: tx.Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.discard()


class AsyncStage(_StageBase):
    """The asynchronous staging area returned by
    [`AsyncPath.staged`][bagof.paths.AsyncPath.staged].

    Use it with `async with`, or `await` it to prepare it and then
    `await` its `commit` or `discard`. The staging directory is made when
    it is entered or awaited.
    """

    def __init__(self, target: tx.Any, *, copy: bool = False) -> None:
        super().__init__(target, copy)
        self._prepared = False

    async def _prepare(self) -> AsyncStage:
        if not self._prepared:
            await bridge.run(self._create)
            self._prepared = True
            if self._copy:
                try:
                    await self._fill()
                except BaseException:
                    self._cleanup()
                    raise
        return self

    def __await__(self) -> tx.Generator[tx.Any, None, AsyncStage]:
        return self._prepare().__await__()

    @property
    def path(self) -> tx.Any:
        """Where to create the new version: named like the target.

        Available once the stage is entered or awaited.
        """
        if not self._prepared:
            raise RuntimeError(
                "enter the staging area with `async with`, or await it, "
                "before using its path"
            )
        return self._path

    def _native_remote(self) -> bool:
        return not self._local and is_async_driver(self._target._wrapped)

    async def _fill(self) -> None:
        if self._local:
            await bridge.run(_fill_local, self._real, self._entry)
        elif self._native_remote():
            await _afill_remote(self._target, self._entry)
        else:
            await bridge.run(
                _fill_remote, self._target._sync(), self._entry
            )

    async def commit(self) -> None:
        """Move what was created at `path` onto the target.

        The same rules as [`Stage.commit`][bagof.paths.Stage.commit].
        """
        self._check_open()
        if not self._prepared:
            # Never entered: nothing can have been staged.
            self._state = _COMMITTED
            return
        try:
            if self._local:
                await bridge.run(
                    _commit_local, self._entry, self._real, self._dir
                )
            elif self._native_remote():
                await _acommit_remote(self._entry, self._target)
            else:
                await bridge.run(
                    _commit_remote, self._entry, self._target._sync()
                )
        except Exception as error:
            raise self._failed(error) from error
        self._state = _COMMITTED
        await bridge.run(self._cleanup)

    async def discard(self) -> None:
        """Remove the staged version; the target is never touched."""
        if self._state == _COMMITTED:
            return
        self._state = _DISCARDED
        await bridge.run(self._cleanup)

    async def __aenter__(self) -> tx.Any:
        await self._prepare()
        return self.path

    async def __aexit__(
        self, exc_type: tx.Any, exc: tx.Any, tb: tx.Any
    ) -> None:
        if exc_type is None:
            await self.commit()
        else:
            await self.discard()


# -- atomic writes ----------------------------------------------------------
def check_atomic_mode(mode: str) -> bool:
    """Whether an atomic `open` must start from a copy of the target.

    Raises for a reading mode: there is nothing to commit.
    """
    if not any(flag in mode for flag in "wax+"):
        raise ValueError(
            f"atomic=True needs a mode that writes, not {mode!r}"
        )
    return "a" in mode or ("r" in mode and "+" in mode)


def exists_error(target: tx.Any) -> FileExistsError:
    code = errno.EEXIST
    return FileExistsError(code, os.strerror(code), str(target))


class AtomicFile:
    """A file object whose clean close commits a staged write."""

    def __init__(self, handle: tx.Any, stage: Stage) -> None:
        self._handle = handle
        self._stage = stage
        self._done = False

    def __getattr__(self, name: str) -> tx.Any:
        return getattr(self._handle, name)

    def __iter__(self) -> tx.Iterator[tx.Any]:
        return iter(self._handle)

    @property
    def closed(self) -> bool:
        return self._done or self._handle.closed

    def close(self) -> None:
        if self._done:
            return
        self._done = True
        self._handle.close()
        self._stage.commit()

    def _abort(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self._handle.close()
        finally:
            self._stage.discard()

    def __enter__(self) -> AtomicFile:
        return self

    def __exit__(self, exc_type: tx.Any, exc: tx.Any, tb: tx.Any) -> None:
        if exc_type is None:
            self.close()
        else:
            self._abort()


class AsyncAtomicFile:
    """The async counterpart of `AtomicFile`."""

    def __init__(self, handle: tx.Any, stage: AsyncStage) -> None:
        self._handle = handle
        self._stage = stage
        self._done = False

    def __getattr__(self, name: str) -> tx.Any:
        return getattr(self._handle, name)

    def __aiter__(self) -> tx.Any:
        return self._handle.__aiter__()

    async def close(self) -> None:
        if self._done:
            return
        self._done = True
        await self._handle.close()
        await self._stage.commit()

    async def _abort(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            await self._handle.close()
        finally:
            await self._stage.discard()

    async def __aenter__(self) -> AsyncAtomicFile:
        return self

    async def __aexit__(
        self, exc_type: tx.Any, exc: tx.Any, tb: tx.Any
    ) -> None:
        if exc_type is None:
            await self.close()
        else:
            await self._abort()

