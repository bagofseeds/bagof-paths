"""Scoped local copies of remote paths, and the stages that hold them.

Many libraries open a file by name only. `local_copy()` gives a local path
for the content of any path: a local path is used as it is, and a remote file
or directory is downloaded into a private temporary directory under its exact
remote name.

A *stage* (`Path.local_stage()`) is one such directory shared by several
copies. While it is active it is the default of every `local_copy()` made in
the same thread or asyncio task, and it lays each copy out as
`<stage>/<scheme>/<bucket-or-host>/<key>`, so copies from one store keep their
relative positions -- a slide file finds the tile directory beside it. Each
copy lives as long as its own `with` block; a path held by two overlapping
blocks is downloaded once and removed when the last one ends. The stage
removes whatever is left when it closes. A copy taken without `with` stays
until its own `cleanup()` (or until its stage closes), and is never removed by
garbage collection: a library may still be reading the file.

The active stage is a `contextvars.ContextVar`: each thread and each asyncio
task sees its own, and the async bridge runs its worker-thread calls in a copy
of the caller's context so the active stage is seen there too.
"""

from __future__ import annotations

import contextvars
import os
import shutil
import tempfile
import threading
import uuid
import weakref
from pathlib import Path as LocalPath
from pathlib import PurePosixPath

import typing_extensions as tx

from . import _bridge as bridge
from ._detect import is_async_driver
from ._protocols import canonical_scheme, path_part

_ACTIVE: contextvars.ContextVar[tx.Optional[LocalStage]] = (
    contextvars.ContextVar("bagof_paths_local_stage", default=None)
)

# Read a remote file in chunks of this size when the driver has no download.
_CHUNK = 1 << 20


def _remove(path: str) -> None:
    """Remove a file or a directory tree, best effort."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        # Clean-up never raises: a file still open on Windows, say, is
        # left behind for the stage (or the system) to remove later.
        pass


class _Entry:
    """One local copy in a stage: where it is and who holds it."""

    __slots__ = ("source", "local", "holders", "ready", "lock")

    def __init__(self, source: str, local: str) -> None:
        self.source = source
        self.local = local
        self.holders = 0
        self.ready = False
        self.lock = threading.Lock()


class LocalStage:
    """A local directory that holds the local copies of remote paths.

    Make one with `Path.local_stage()`. Used with `with` (or `async with`),
    it is the default stage of every `local_copy()` made inside the block,
    and it is removed, with anything still in it, when the block ends. Copies
    in one stage keep the relative positions they have in their store, so a
    file finds the companion files a library expects beside it -- as long as
    they are held at the same time:

    ```python
    with Path.local_stage():
        with hdr.local_copy() as h, img.local_copy() as i:
            image = nibabel.load(h)       # finds the .img beside the .hdr
    ```

    A stage can also be used without `with`: pass it as
    `local_copy(stage=...)` and call `cleanup()` when done. A stage that is
    never cleaned up is removed when it is garbage collected, or at exit. A
    copy taken without `with` inside a stage stays until its own `cleanup()`
    or until the stage closes.
    """

    def __init__(self, *, _finalize: bool = True) -> None:
        self._dir = tempfile.mkdtemp(prefix="bagof-stage-")
        self._lock = threading.Lock()
        self._entries: tx.Dict[str, _Entry] = {}
        self._tokens: tx.List[contextvars.Token] = []
        self._closed = False
        # A stage made by the user is removed if it is garbage collected (or
        # at exit), like a TemporaryDirectory. The private stage of a single
        # copy is not: its file may still be in use by a library after the
        # handle is dropped, so only an explicit cleanup removes it.
        self._finalizer: tx.Optional[weakref.finalize] = (
            weakref.finalize(self, shutil.rmtree, self._dir, True)
            if _finalize else None
        )

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"LocalStage({self._dir!r}, {state})"

    @property
    def path(self) -> tx.Any:
        """The stage's local directory."""
        from ._path import Path

        return Path(self._dir)

    @property
    def closed(self) -> bool:
        """Whether the stage has been cleaned up."""
        return self._closed

    # -- activation ---------------------------------------------------------
    def __enter__(self) -> LocalStage:
        self._check_open()
        self._tokens.append(_ACTIVE.set(self))
        return self

    def __exit__(self, *exc: tx.Any) -> None:
        self._deactivate()
        if not self._tokens:
            self.cleanup()

    async def __aenter__(self) -> LocalStage:
        return self.__enter__()

    async def __aexit__(self, *exc: tx.Any) -> None:
        self._deactivate()
        if not self._tokens:
            await bridge.run(self.cleanup)

    def _deactivate(self) -> None:
        if self._tokens:
            _ACTIVE.reset(self._tokens.pop())

    def cleanup(self) -> None:
        """Remove the stage's directory and every copy still in it.

        Calling it again does nothing. A copy that is still held keeps its
        open handles, but its file is gone.
        """
        with self._lock:
            self._closed = True
            self._entries.clear()
        if self._finalizer is not None:
            self._finalizer()
        else:
            shutil.rmtree(self._dir, ignore_errors=True)

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("this local stage has been cleaned up")

    # -- layout -------------------------------------------------------------
    def _slot(self, wrapper: tx.Any) -> str:
        """Where a remote path's copy goes, relative to the stage.

        `<scheme>/<bucket-or-host>/<key>`, from the path without its query,
        so that copies from one store keep their relative positions.
        """
        text = path_part(str(wrapper._wrapped))
        scheme, _, rest = text.rpartition("://")
        outer = scheme.split("::", 1)[0] or wrapper.protocol or "remote"
        parts = [
            part
            for part in PurePosixPath(rest).parts
            if part not in ("/", "", ".", "..")
        ]
        if not parts:
            raise ValueError(
                f"cannot make a local copy of {str(wrapper)!r}: it names no "
                "file or directory"
            )
        return "/".join([canonical_scheme(outer), *parts])

    # -- holding and releasing ----------------------------------------------
    def _hold(self, wrapper: tx.Any) -> _Entry:
        """Register one more holder of `wrapper`'s copy (not downloaded)."""
        slot = self._slot(wrapper)
        source = str(wrapper)
        with self._lock:
            self._check_open()
            entry = self._entries.get(slot)
            if entry is None:
                local = os.path.join(self._dir, *slot.split("/"))
                entry = self._entries[slot] = _Entry(source, local)
            elif entry.source != source:
                raise ValueError(
                    f"this local stage already holds a copy of "
                    f"{entry.source!r} where {source!r} would go; use "
                    "another stage for it"
                )
            entry.holders += 1
        return entry

    def _release(self, entry: _Entry) -> None:
        """Drop one holder; the last one removes the copy."""
        with self._lock:
            entry.holders -= 1
            if entry.holders > 0:
                return
            slot = os.path.relpath(entry.local, self._dir)
            slot = slot.replace(os.sep, "/")
            if self._entries.get(slot) is entry:
                del self._entries[slot]
            _remove(entry.local)
            self._prune(os.path.dirname(entry.local))

    def _prune(self, directory: str) -> None:
        """Remove now-empty directories above a removed copy (lock held).

        A directory another live copy sits under is kept, even if it is
        empty because that copy is still downloading.
        """
        live = [entry.local for entry in self._entries.values()]
        top = os.path.normpath(self._dir)
        while os.path.normpath(directory) != top:
            prefix = directory + os.sep
            if any(local.startswith(prefix) for local in live):
                return
            try:
                os.rmdir(directory)
            except OSError:
                return
            directory = os.path.dirname(directory)

    def _fetch(self, wrapper: tx.Any) -> _Entry:
        """Hold `wrapper`'s copy, downloading it if it is not there yet."""
        entry = self._hold(wrapper)
        try:
            with entry.lock:
                if not entry.ready:
                    _download(wrapper, entry.local)
                    entry.ready = True
        except BaseException:
            self._release(entry)
            raise
        return entry

    async def _afetch(self, wrapper: tx.Any) -> _Entry:
        """`_fetch` for a natively-async driver, awaited on the loop."""
        entry = self._hold(wrapper)
        try:
            # A threading lock, acquired off the loop, so a sync and an
            # async caller of one stage never download the same copy twice.
            await bridge.run(entry.lock.acquire)
            try:
                if not entry.ready:
                    await _adownload(wrapper, entry.local)
                    entry.ready = True
            finally:
                entry.lock.release()
        except BaseException:
            self._release(entry)
            raise
        return entry


# -- downloads ---------------------------------------------------------------
def _partial(local: str) -> str:
    """A name beside `local` for a download that is not finished yet.

    A failed download is removed under this name, so a later fetch of the
    same path never mistakes a half file for a finished copy.
    """
    head, tail = os.path.split(local)
    return os.path.join(head, f".{tail}.partial-{uuid.uuid4().hex[:8]}")


def _download(wrapper: tx.Any, local: str) -> None:
    """Download a remote file or directory to the local path `local`."""
    os.makedirs(os.path.dirname(local), exist_ok=True)
    partial = _partial(local)
    try:
        if wrapper.is_dir():
            _download_tree(wrapper, partial)
        else:
            _download_file(wrapper, partial)
        os.replace(partial, local)
    except BaseException:
        _remove(partial)
        raise


def _download_tree(wrapper: tx.Any, local: str) -> None:
    os.makedirs(local)
    depth = len(wrapper.parts)
    for dirpath, dirnames, filenames in wrapper.walk():
        here = os.path.join(local, *dirpath.parts[depth:])
        os.makedirs(here, exist_ok=True)
        for name in dirnames:
            os.makedirs(os.path.join(here, name), exist_ok=True)
        for name in filenames:
            _download_file(dirpath / name, os.path.join(here, name))


def _download_file(wrapper: tx.Any, local: str) -> None:
    """One remote file to `local`, with the driver's own download if any."""
    wrapped = wrapper._wrapped
    if _is_upath(wrapped):
        wrapped.fs.get_file(wrapped.path, local)
        return
    if _is_cloudpath(wrapped):
        wrapped.download_to(local)
        return
    with wrapper.open("rb") as source, open(local, "wb") as target:
        shutil.copyfileobj(source, target, _CHUNK)


async def _adownload(wrapper: tx.Any, local: str) -> None:
    """`_download` for an `AsyncPath` over a natively-async driver."""
    await bridge.run(os.makedirs, os.path.dirname(local), exist_ok=True)
    partial = _partial(local)
    try:
        if await wrapper.is_dir():
            await _adownload_tree(wrapper, partial)
        else:
            await _adownload_file(wrapper, partial)
        await bridge.run(os.replace, partial, local)
    except BaseException:
        _remove(partial)
        raise


async def _adownload_tree(wrapper: tx.Any, local: str) -> None:
    await bridge.run(os.makedirs, local)
    depth = len(wrapper.parts)
    async for dirpath, dirnames, filenames in wrapper.walk():
        here = os.path.join(local, *dirpath.parts[depth:])
        await bridge.run(os.makedirs, here, exist_ok=True)
        for name in dirnames:
            await bridge.run(
                os.makedirs, os.path.join(here, name), exist_ok=True
            )
        for name in filenames:
            await _adownload_file(dirpath / name, os.path.join(here, name))


async def _adownload_file(wrapper: tx.Any, local: str) -> None:
    handle = await wrapper.open("rb")
    target = await bridge.run(open, local, "wb")
    try:
        async with handle as source:
            while True:
                chunk = await source.read(_CHUNK)
                if not chunk:
                    break
                await bridge.run(target.write, chunk)
    finally:
        await bridge.run(target.close)


def _is_upath(wrapped: tx.Any) -> bool:
    try:
        from upath import UPath
    except ImportError:  # pragma: no cover - the backend is optional
        return False
    return isinstance(wrapped, UPath) and hasattr(type(wrapped), "fs")


def _is_cloudpath(wrapped: tx.Any) -> bool:
    try:
        from cloudpathlib import CloudPath
    except ImportError:  # pragma: no cover - the backend is optional
        return False
    return isinstance(wrapped, CloudPath)


# -- the copies --------------------------------------------------------------
def _choose_stage(
    explicit: tx.Optional[LocalStage],
) -> tx.Tuple[LocalStage, bool]:
    """The explicit stage, else the active one, else a private one.

    The flag says whether the stage is private (made for one copy).
    """
    if explicit is not None:
        explicit._check_open()
        return explicit, False
    active = _ACTIVE.get()
    if active is not None and not active.closed:
        return active, False
    return LocalStage(_finalize=False), True


def _let_go(
    stage: tx.Optional[LocalStage], entry: tx.Optional[_Entry], private: bool
) -> None:
    """Drop one hold on a copy: a private stage goes, a shared one counts."""
    if stage is None or entry is None:
        return
    if private:
        stage.cleanup()
    else:
        stage._release(entry)


class LocalCopy(os.PathLike):
    """A local copy of a path, for a library that opens files by name.

    `path.local_copy()` downloads a remote file or directory at once and
    returns this handle; a local path is used as it is. The handle is itself
    path-like (`os.fspath` gives the local file), so it can be handed
    straight to a library:

    ```python
    image = nibabel.load(Path("s3://bucket/scan.nii.gz").local_copy())
    ```

    Used with `with`, the block gets the local path and the copy is removed
    when the block ends. Taken without `with`, the copy stays until
    `cleanup()` is called, or until the active stage it was taken in closes;
    it is never removed just because the handle is dropped, since a library
    may still be reading the file.
    """

    def __init__(
        self, source: tx.Any, stage: tx.Optional[LocalStage]
    ) -> None:
        self._stage: tx.Optional[LocalStage] = None
        self._entry: tx.Optional[_Entry] = None
        self._private = False
        self._done = False
        if source.is_local():
            self._path = source
            return
        chosen, private = _choose_stage(stage)
        try:
            entry = chosen._fetch(source)
        except BaseException:
            if private:
                chosen.cleanup()
            raise
        self._stage, self._entry, self._private = chosen, entry, private
        self._path = source.with_wrapped(LocalPath(entry.local))

    def __repr__(self) -> str:
        return f"LocalCopy({str(self._path)!r})"

    @property
    def path(self) -> tx.Any:
        """The local path of the copy."""
        return self._path

    def __fspath__(self) -> str:
        return os.fspath(self._path)

    def cleanup(self) -> None:
        """Let the copy go. Idempotent.

        Inside a stage, the copy is removed when its last holder lets it go;
        a copy taken outside any stage is removed at once. A local path is
        never removed.
        """
        if self._done:
            return
        self._done = True
        _let_go(self._stage, self._entry, self._private)

    def __enter__(self) -> tx.Any:
        return self._path

    def __exit__(self, *exc: tx.Any) -> None:
        self.cleanup()


class AsyncLocalCopy(os.PathLike):
    """The `AsyncPath` version of `LocalCopy`.

    A plain call cannot download, so the handle is lazy:
    `copy = await path.local_copy()` downloads and returns it, and
    `async with path.local_copy() as local:` downloads on entry and yields
    the local path. `await copy.cleanup()` lets it go.
    """

    def __init__(
        self, source: tx.Any, stage: tx.Optional[LocalStage]
    ) -> None:
        self._source = source
        self._explicit = stage
        self._path: tx.Any = None
        self._sync_copy: tx.Optional[LocalCopy] = None
        self._stage: tx.Optional[LocalStage] = None
        self._entry: tx.Optional[_Entry] = None
        self._private = False
        self._done = False

    def __repr__(self) -> str:
        where = "not fetched" if self._path is None else str(self._path)
        return f"AsyncLocalCopy({where!r})"

    async def _fetch(self) -> AsyncLocalCopy:
        if self._path is not None:
            return self
        source = self._source
        if source.is_local():
            self._path = source
        elif is_async_driver(source._wrapped):
            chosen, private = _choose_stage(self._explicit)
            try:
                entry = await chosen._afetch(source)
            except BaseException:
                if private:
                    await bridge.run(chosen.cleanup)
                raise
            self._stage, self._entry, self._private = chosen, entry, private
            self._path = source.with_wrapped(LocalPath(entry.local))
        else:
            # A synchronous driver: the synchronous copy runs in a worker
            # thread, where it finds the caller's active stage through the
            # bridge's copy of the context.
            sync = await bridge.run(
                source._sync().local_copy, self._explicit
            )
            self._sync_copy = sync
            self._path = source.with_wrapped(sync.path._wrapped)
        return self

    def __await__(self) -> tx.Generator[tx.Any, None, AsyncLocalCopy]:
        return self._fetch().__await__()

    @property
    def path(self) -> tx.Any:
        """The local path of the copy (after it has been fetched)."""
        if self._path is None:
            raise ValueError(
                "this local copy has not been fetched; await it first"
            )
        return self._path

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    async def cleanup(self) -> None:
        """Let the copy go (see `LocalCopy.cleanup`). Idempotent."""
        if self._done or self._path is None:
            self._done = True
            return
        self._done = True
        if self._sync_copy is not None:
            await bridge.run(self._sync_copy.cleanup)
            return
        await bridge.run(_let_go, self._stage, self._entry, self._private)

    async def __aenter__(self) -> tx.Any:
        await self._fetch()
        return self._path

    async def __aexit__(self, *exc: tx.Any) -> None:
        await self.cleanup()


class LocalCopyMixin:
    """`local_copy()`, written once for `Path` and `AsyncPath`."""

    __slots__ = ()

    def local_copy(self, stage: tx.Optional[LocalStage] = None) -> tx.Any:
        """A local copy of this path, for a library that opens files by name.

        A local path is used as it is. A remote file or directory is
        downloaded under its exact name into a temporary directory:

        ```python
        with Path("s3://bucket/slides/case1.svs").local_copy() as local:
            slide = openslide.OpenSlide(local)
            ...                                # use it inside the block
        # the copy is removed here
        ```

        Without `with`, the copy stays until its `cleanup()`, or until the
        active stage it was taken in closes. Inside `Path.local_stage()`,
        copies share the stage's directory and keep the relative positions
        they have in their store, so a library that opens companion files
        finds them -- hold them at the same time:

        ```python
        with Path.local_stage():
            with hdr.local_copy() as h, img.local_copy() as i:
                image = nibabel.load(h)
        ```

        `stage` picks a stage explicitly, over the active one. Changes to
        the copy are thrown away: to write a file back, stage the write
        instead. On an `AsyncPath`, `await` it or use `async with`.
        """
        if self._family == "async":  # type: ignore[attr-defined]
            return AsyncLocalCopy(self, stage)
        return LocalCopy(self, stage)
