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

import contextlib
import errno
import io
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
def _make_staging_dir(
    parent: tx.Optional[str], name: str, kind: str = "staging"
) -> str:
    """A new, private directory: hidden, named after the target.

    `kind` says what it holds: the staged version, or (`"old"`) the old one
    while a directory is swapped.
    """
    try:
        return tempfile.mkdtemp(prefix=f".{name}.{kind}-", dir=parent)
    except OSError as error:
        if error.errno != errno.ENAMETOOLONG:
            raise
        # The target's name leaves no room for the prefix; the entry inside
        # still carries the full name.
        return tempfile.mkdtemp(prefix=f".{kind}-", dir=parent)


def _remove(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _remove_empty(path: str) -> None:
    """Remove a directory only if it is empty: it may hold the user's data."""
    try:
        os.rmdir(path)
    except OSError:  # pragma: no cover - not empty, or already gone
        pass


def _resolve_local(target: tx.Any) -> tx.Tuple[str, str, str]:
    """`(real target, parent, name)` for a local target, links followed."""
    real = os.path.realpath(os.fspath(target))
    parent, name = os.path.split(real)
    return real, parent, name


def _type_error(new_is_dir: bool, target: tx.Any) -> OSError:
    """What `pathlib` raises for writing one type of entry over the other."""
    if new_is_dir:
        code, cls = errno.ENOTDIR, NotADirectoryError
    else:
        code, cls = errno.EISDIR, IsADirectoryError
    return cls(code, os.strerror(code), str(target))


def _inside(name: tx.Any, directory: str) -> bool:
    return isinstance(name, str) and (
        name == directory or name.startswith(directory + os.sep)
    )


def _naming_target(error: BaseException, stage: _StageBase) -> BaseException:
    """`error` as writing the target itself would raise it.

    An `OSError` that names an entry of the staging directory names the
    target instead: the caller never saw the staging directory, and it is
    gone by the time the error is read. Anything else is returned as it is.
    """
    directory = stage._dir
    if (
        directory is None
        or not isinstance(error, OSError)
        or error.errno is None
        or not (
            _inside(error.filename, directory)
            or _inside(error.filename2, directory)
        )
    ):
        return error
    args: tx.Tuple[tx.Any, ...] = (
        error.errno, error.strerror, str(stage._target)
    )
    winerror = getattr(error, "winerror", None)
    if winerror is not None:  # pragma: no cover - Windows only
        args += (winerror,)
    try:
        named = type(error)(*args)
    except Exception:  # pragma: no cover - an OSError with another signature
        return error
    return named.with_traceback(error.__traceback__)


def reraise_naming_target(
    error: BaseException, stage: _StageBase
) -> tx.NoReturn:
    """Re-raise `error` (inside the `except` that caught it), naming the
    target rather than the staging directory."""
    named = _naming_target(error, stage)
    if named is error:
        raise error
    raise named from None


class _TargetChanged(Exception):
    """A commit failed after it had started changing the target.

    The target is no longer as it was, so nothing may be removed: the staged
    data may now be the only complete version, and `previous` is where the
    old version was left when it could not be put back.
    """

    def __init__(
        self, error: BaseException, previous: tx.Optional[str] = None
    ) -> None:
        super().__init__(error)
        self.error = error
        self.previous = previous


@contextlib.contextmanager
def _changing_target() -> tx.Iterator[None]:
    """Mark a failure inside the block as one that changed the target."""
    try:
        yield
    except Exception as error:
        raise _TargetChanged(error) from error


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


def _commit_local(
    entry: str, real: str, target: tx.Any, replace_type: bool
) -> None:
    """Move what exists at `entry` onto the local target `real`."""
    if not os.path.lexists(entry):
        return
    new_is_dir = _is_dir(entry)
    old_exists = os.path.lexists(real)
    old_is_dir = old_exists and _is_dir(real)
    if not replace_type and old_exists and new_is_dir != old_is_dir:
        raise _type_error(new_is_dir, target)
    if not new_is_dir and not old_is_dir:
        # A file over a file (or over nothing): one atomic replace.
        if old_exists and os.path.isfile(real):
            mode = stat.S_IMODE(os.stat(real).st_mode)
            os.chmod(entry, mode)
        _fsync_file(entry)
        os.replace(entry, real)
        _fsync_dir(os.path.dirname(real))
        return
    # A directory, or a change of type: move the old entry aside, then the
    # new one in. Aside is a hidden directory of its own beside the target
    # (so on the same file system), never the staging directory: if the old
    # entry cannot be put back, discarding the stage must not reach it.
    parent, name = os.path.split(real)
    if not old_exists:
        os.rename(entry, real)
        _fsync_dir(parent)
        return
    holder = _make_staging_dir(parent, name, "old")
    aside = os.path.join(holder, name)
    try:
        os.rename(real, aside)
    except BaseException:
        _remove_empty(holder)
        raise
    try:
        os.rename(entry, real)
    except BaseException as error:
        try:
            os.rename(aside, real)
        except BaseException:
            # The old version is neither at the target nor back: leave it
            # where it is, for good, and say where.
            raise _TargetChanged(error, previous=aside) from error
        _remove_empty(holder)
        raise
    _fsync_dir(parent)
    _remove(holder)  # the old version, now replaced


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


def _commit_remote(entry: str, target: tx.Any, replace_type: bool) -> None:
    """Upload what exists at `entry` over the remote target."""
    if not os.path.lexists(entry):
        return
    old = _remote_kind(target)
    new_is_dir = _is_dir(entry)
    if not replace_type and old == ("file" if new_is_dir else "dir"):
        raise _type_error(new_is_dir, target)
    if not new_is_dir:
        if old != "dir":
            # One object, written whole: a failure leaves the old one.
            _upload_file(entry, target)
            return
        with _changing_target():
            target.rmdir(recursive=True)
            _upload_file(entry, target)
        return
    base = target._query_free()
    new = _local_files(entry)
    # A directory is uploaded file by file: from the first change on, the
    # target is neither the old version nor the new one.
    with _changing_target():
        if old == "file":
            target.unlink()
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


async def _acommit_remote(
    entry: str, target: tx.Any, replace_type: bool
) -> None:
    if not await bridge.run(os.path.lexists, entry):
        return
    old = await _aremote_kind(target)
    new_is_dir = await bridge.run(_is_dir, entry)
    if not replace_type and old == ("file" if new_is_dir else "dir"):
        raise _type_error(new_is_dir, target)
    data = None if new_is_dir else await bridge.run(_read_local, entry)
    if not new_is_dir and old != "dir":
        # One request: the native driver writes the object whole.
        await target.write_bytes(data)
        return
    try:
        if not new_is_dir:
            await target.rmdir(recursive=True)
            await target.write_bytes(data)
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
    except Exception as error:
        raise _TargetChanged(error) from error


# -- the staging areas ------------------------------------------------------
class _StageBase:
    """What a synchronous and an asynchronous staging area share."""

    # The stage behind `atomic=True`: it writes a file, so it refuses a
    # directory at the target; a failure leaves nothing behind and raises the
    # error itself, naming the target, since the caller has no stage to
    # commit again and never saw the staging directory.
    _atomic: tx.ClassVar[bool] = False

    def __init__(
        self,
        target: tx.Any,
        copy: bool,
        replace_type: bool,
        keep_on_failure: bool,
    ) -> None:
        self._target = target
        self._copy = copy
        self._replace_type = replace_type and not self._atomic
        self._keep_on_failure = keep_on_failure and not self._atomic
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
            if self._atomic and _is_dir(self._real):
                # Fail before the write, as pathlib does; the commit checks
                # again, since a directory can appear meanwhile.
                raise _type_error(False, self._target)
        else:
            parent, name = None, self._target.name
        if not name:
            raise ValueError(
                f"cannot stage {str(self._target)!r}: it names no file or "
                "directory"
            )
        try:
            self._dir = _make_staging_dir(parent, name)
        except OSError as error:
            if not self._local or error.errno is None:
                raise
            # The directory beside the target could not be made (no write
            # permission, say): name the target, as writing it would.
            raise type(error)(
                error.errno, error.strerror, str(self._target)
            ) from None
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

    def _failed(
        self, error: BaseException, previous: tx.Optional[str] = None
    ) -> CommitError:
        # Keep the staged data, past garbage collection too, for another
        # commit: on request (keep_on_failure), or because the target was
        # changed before the failure and nothing may be removed.
        self._state = _FAILED
        if self._finalizer is not None:
            self._finalizer.detach()
        return CommitError(self._target, self._entry, error, previous=previous)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self._target)!r})"


class Stage(_StageBase):
    """A staging area for a new version of a path.

    Returned by [`Path.staged`][bagof.paths.Path.staged]; use it as a
    context manager, or call `commit` or `discard` yourself. The staging
    directory exists from the moment the stage is made.
    """

    def __init__(
        self,
        target: tx.Any,
        *,
        copy: bool = False,
        replace_type: bool = False,
        keep_on_failure: bool = False,
    ) -> None:
        super().__init__(target, copy, replace_type, keep_on_failure)
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
        is removed, and [`CommitError`][bagof.paths.CommitError] is raised.
        A stage made with `keep_on_failure=True` keeps the data instead and
        the error says where; `commit` may then be called again, or
        `discard` removes it.
        """
        self._check_open()
        try:
            if self._local:
                _commit_local(
                    self._entry, self._real, self._target,
                    self._replace_type,
                )
            else:
                _commit_remote(self._entry, self._target, self._replace_type)
        except _TargetChanged as changed:
            # Whatever the options: removing anything now could lose data.
            raise self._failed(
                changed.error, changed.previous
            ) from changed.error
        except Exception as error:
            if self._keep_on_failure:
                raise self._failed(error) from error
            self.discard()
            if self._atomic:
                reraise_naming_target(error, self)
            raise CommitError(self._target, None, error) from error
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

    def __init__(
        self,
        target: tx.Any,
        *,
        copy: bool = False,
        replace_type: bool = False,
        keep_on_failure: bool = False,
    ) -> None:
        super().__init__(target, copy, replace_type, keep_on_failure)
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
                    _commit_local, self._entry, self._real,
                    self._target, self._replace_type,
                )
            elif self._native_remote():
                await _acommit_remote(
                    self._entry, self._target, self._replace_type
                )
            else:
                await bridge.run(
                    _commit_remote, self._entry, self._target._sync(),
                    self._replace_type,
                )
        except _TargetChanged as changed:
            # Whatever the options: removing anything now could lose data.
            raise self._failed(
                changed.error, changed.previous
            ) from changed.error
        except Exception as error:
            if self._keep_on_failure:
                raise self._failed(error) from error
            await self.discard()
            if self._atomic:
                reraise_naming_target(error, self)
            raise CommitError(self._target, None, error) from error
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
# `atomic=True` on `write_bytes`, `write_text` and `open` is the plain write,
# made atomic: where the staging area would show, it acts as the plain write
# does. It refuses a directory at the target, an error names the target
# (never the staging directory), and a failure, before or during the commit,
# leaves nothing behind and raises the error itself, not `CommitError`. It
# never keeps its data: the caller still holds it, and it never changes the
# type of the target, even with `replace_type=True`.
class AtomicStage(Stage):
    """The staging area behind `atomic=True`."""

    _atomic = True


class AsyncAtomicStage(AsyncStage):
    """The async staging area behind `atomic=True`."""

    _atomic = True


@contextlib.contextmanager
def atomic(target: tx.Any) -> tx.Iterator[tx.Any]:
    """Write a new file at the yielded path; commit it over `target`."""
    stage = AtomicStage(target)
    try:
        yield stage.path
    except BaseException as error:
        stage.discard()
        reraise_naming_target(error, stage)
    stage.commit()


@contextlib.asynccontextmanager
async def aatomic(target: tx.Any) -> tx.AsyncIterator[tx.Any]:
    """The async counterpart of `atomic`."""
    stage = await AsyncAtomicStage(target)
    try:
        yield stage.path
    except BaseException as error:
        await stage.discard()
        reraise_naming_target(error, stage)
    await stage.commit()


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


def _forward(name: str) -> tx.Any:
    def method(self: AtomicFile, *args: tx.Any, **kwargs: tx.Any) -> tx.Any:
        return getattr(self._handle, name)(*args, **kwargs)

    method.__name__ = method.__qualname__ = name
    return method


class AtomicFile:
    """A file object whose clean close commits a staged write.

    It passes as the `io` class of the file it holds (`io.TextIOBase`,
    `io.BufferedIOBase` or `io.RawIOBase`), so a library that tells an open
    file from a file name with `isinstance` takes it as a file (#59). It is
    registered as one rather than derived from one: `io.IOBase` would bring
    methods of its own, and a `__del__` that would commit a half-written
    file.
    """

    def __new__(cls, handle: tx.Any, stage: Stage) -> AtomicFile:
        if cls is AtomicFile:
            for kind, atomic_kind in _ATOMIC_KINDS:
                if isinstance(handle, kind):
                    cls = atomic_kind
                    break
        return super().__new__(cls)

    def __init__(self, handle: tx.Any, stage: Stage) -> None:
        self._handle = handle
        self._stage = stage
        self._done = False

    def __getattr__(self, name: str) -> tx.Any:
        return getattr(self._handle, name)

    # The members every `io` file has are defined here, not only reached
    # through `__getattr__`: a runtime-checkable protocol looks for them on
    # the class (`inspect.getattr_static`, Python 3.12+).
    read = _forward("read")
    readline = _forward("readline")
    readlines = _forward("readlines")
    write = _forward("write")
    writelines = _forward("writelines")
    seek = _forward("seek")
    tell = _forward("tell")
    truncate = _forward("truncate")
    flush = _forward("flush")
    fileno = _forward("fileno")
    isatty = _forward("isatty")
    readable = _forward("readable")
    writable = _forward("writable")
    seekable = _forward("seekable")

    def __iter__(self) -> tx.Iterator[tx.Any]:
        return iter(self._handle)

    def __next__(self) -> tx.Any:
        return next(self._handle)

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


class AtomicTextFile(AtomicFile):
    pass


class AtomicBufferedFile(AtomicFile):
    pass


class AtomicRawFile(AtomicFile):
    pass


io.TextIOBase.register(AtomicTextFile)
io.BufferedIOBase.register(AtomicBufferedFile)
io.RawIOBase.register(AtomicRawFile)

_ATOMIC_KINDS = (
    (io.TextIOBase, AtomicTextFile),
    (io.BufferedIOBase, AtomicBufferedFile),
    (io.RawIOBase, AtomicRawFile),
)


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

