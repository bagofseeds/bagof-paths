"""Driver adapters: where operations that diverge across backends are
reconciled in one place.

Most of the surface is uniform and goes through the engine. A few operations
are not -- `rmdir`'s recursion, native `copy`/`move`, `walk` -- because
the backends genuinely disagree:

- `UPath`'s bare `rmdir()` deletes a *non-empty* tree (its `recursive`
  defaults to `True`), so the wrapper must always pass `recursive=False`;
- `cloudpathlib` has no recursive `rmdir` at all, but a separate
  `rmtree()`;
- `pathlib` gained `copy` in 3.14 and `walk` in 3.12.

Each such operation lives on an adapter: a `GenericAdapter` that works
on any (including unknown) driver by duck-typing, and named subclasses that
override only the specific points a known driver diverges on. Adapters are
selected by driver type through `adapter_for`; an unknown driver gets
the generic one with no registration, and a later
[`register_driver`][bagof.paths.register_driver] wins over an earlier
one for the same object.
"""

from __future__ import annotations

import errno
import inspect
import os
import pathlib
import shutil

import typing_extensions as tx

from ._errors import UnsupportedPathOperation


def _accepts(func: tx.Any, name: str) -> bool:
    """Whether `func` accepts a keyword argument called `name`."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


class ListedType:
    """A child's type as an fsspec listing reported it (an info dict).

    The same shape as `os.DirEntry.is_dir`, so a walk reads either one the
    same way. A point-in-time answer: the listing's, not a fresh query.
    """

    __slots__ = ("_dir",)

    def __init__(self, info: tx.Mapping[str, tx.Any]) -> None:
        self._dir = info.get("type") == "directory"

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._dir

    @classmethod
    def of(cls, info: tx.Any) -> tx.Optional[ListedType]:
        """The type in a listing entry, or None where it does not say.

        A listing that ignored `detail` gives no type. A symlink's listed
        type is the link's own (fsspec's local listing calls a link to a
        directory "other"), so a symlink is asked, not trusted.
        """
        if not isinstance(info, dict) or "type" not in info:
            return None
        if info.get("islink"):
            return None
        return cls(info)


def _is_stock_local(wrapped: tx.Any) -> bool:
    """Whether `wrapped` is a stdlib local path listed the stdlib way.

    Then `os.scandir` gives the same children as its `iterdir`, with their
    types. A subclass that redefines how it lists or classifies is not.
    """
    if not isinstance(wrapped, pathlib.Path):
        return False
    kind = type(wrapped)
    return all(
        getattr(kind, name, None) is getattr(pathlib.Path, name)
        for name in ("iterdir", "is_dir", "is_symlink")
    )


def _lists_with_fsspec(wrapped: tx.Any) -> bool:
    """Whether `wrapped` is a universal-pathlib path over an fsspec store.

    Its local flavor (a stdlib path subclass) is listed the stdlib way.
    """
    return not isinstance(wrapped, pathlib.Path) and hasattr(
        type(wrapped), "fs"
    )


class GenericAdapter:
    """Divergent operations for any path-like object, by duck-typing.

    The behavior here is the safe default; a named adapter overrides only
    where its driver needs different handling. Copy/move perform the
    operation and return nothing -- the wrapper builds the returned path.
    """

    def _target_is_local(self, wrapper: tx.Any, target: tx.Any) -> bool:
        return wrapper.with_wrapped(target).is_local()

    def rmdir(self, wrapper: tx.Any, *, recursive: bool) -> None:
        wrapped = wrapper._wrapped
        if not recursive:
            # A driver whose rmdir takes `recursive` (UPath and its family)
            # defaults it to True, i.e. to deleting a non-empty tree; pass
            # False explicitly so an unregistered such driver is safe too.
            if _accepts(wrapped.rmdir, "recursive"):
                wrapped.rmdir(recursive=False)
            else:
                wrapped.rmdir()
            return
        if wrapper.is_local():
            shutil.rmtree(os.fspath(wrapped))
            return
        raise UnsupportedPathOperation(
            "rmdir(recursive=True)",
            driver=wrapped,
            hint="this driver has no recursive rmdir; remove entries first",
        )

    def copy(
        self,
        wrapper: tx.Any,
        target: tx.Any,
        *,
        follow_symlinks: bool,
        preserve_metadata: bool,
    ) -> None:
        # os.fspath on a non-local target is a download-to-cache side channel;
        # never write through it. Cross-driver copy is out of scope here.
        source_local = wrapper.is_local()
        if not source_local or not self._target_is_local(wrapper, target):
            raise UnsupportedPathOperation(
                "copy",
                driver=wrapper._wrapped,
                hint="the generic adapter copies only between local paths",
            )
        src = os.fspath(wrapper._wrapped)
        dst = os.fspath(target)
        if wrapper.is_dir():
            shutil.copytree(src, dst, symlinks=not follow_symlinks)
        elif wrapper.is_file():
            do_copy = shutil.copy2 if preserve_metadata else shutil.copy
            do_copy(src, dst, follow_symlinks=follow_symlinks)
        else:
            raise FileNotFoundError(src)

    def move(self, wrapper: tx.Any, target: tx.Any) -> None:
        wrapped = wrapper._wrapped
        replace = getattr(wrapped, "replace", None)
        if replace is not None:
            try:
                replace(target)
                return
            except OSError as error:
                # Only a cross-device rename falls back to copy+delete; any
                # other OSError (e.g. target is a directory) is a real error.
                if error.errno != errno.EXDEV:
                    raise
        self.copy(
            wrapper, target, follow_symlinks=True, preserve_metadata=True
        )
        if wrapper.is_dir():
            self.rmdir(wrapper, recursive=True)
        else:
            wrapped.unlink()

    def walk(
        self,
        wrapper: tx.Any,
        *,
        top_down: bool,
        on_error: tx.Any,
        follow_symlinks: bool,
    ) -> tx.Iterator[tx.Tuple[tx.Any, tx.List[str], tx.List[str]]]:
        native = getattr(wrapper._wrapped, "walk", None)
        if callable(native):
            # Forward only non-default keywords, so a driver whose walk takes
            # none still works on a default call.
            kwargs = {}
            if not top_down:
                kwargs["top_down"] = top_down
            if on_error is not None:
                kwargs["on_error"] = on_error
            if follow_symlinks:
                kwargs["follow_symlinks"] = follow_symlinks
            for dirpath, dirnames, filenames in native(**kwargs):
                yield wrapper.with_wrapped(dirpath), dirnames, filenames
            return
        yield from self._walk_fallback(
            wrapper,
            top_down=top_down,
            on_error=on_error,
            follow_symlinks=follow_symlinks,
        )

    def _listing(
        self, wrapper: tx.Any, *, known_dir: bool
    ) -> tx.List[tx.Any]:
        """The children of `wrapper`, in one listing.

        A child carries the listing's own type answer where the driver's
        listing gives one (`os.scandir` for a stdlib local path); otherwise
        it is a plain `iterdir()` child, classified by asking it. A known
        directory (one a parent listing reported) skips any check that the
        path is a directory.
        """
        wrapped = wrapper._wrapped
        if _is_stock_local(wrapped):
            with os.scandir(wrapped) as scan:
                return [
                    wrapper._listed(wrapped / entry.name, entry)
                    for entry in scan
                ]
        return list(wrapper.iterdir())

    def _walk_fallback(
        self,
        wrapper: tx.Any,
        *,
        top_down: bool,
        on_error: tx.Any,
        follow_symlinks: bool,
        known_dir: bool = False,
    ) -> tx.Iterator[tx.Tuple[tx.Any, tx.List[str], tx.List[str]]]:
        # Synthesized for pathlib < 3.12, and for drivers whose own walk asks
        # every child for its type.
        try:
            entries = self._listing(wrapper, known_dir=known_dir)
        except OSError as error:
            if on_error is not None:
                on_error(error)
            return
        dirnames: tx.List[str] = []
        filenames: tx.List[str] = []
        listed_dirs = set()
        for entry in entries:
            snapshot = getattr(entry, "_snapshot", None)
            try:
                if snapshot is not None:
                    # The listing's answer: no query per child.
                    is_dir = snapshot.is_dir(follow_symlinks=follow_symlinks)
                else:
                    is_dir = entry.is_dir()
            except OSError as error:
                if on_error is not None:
                    on_error(error)
                continue
            # Mirror native walk: with follow_symlinks=False a symlinked
            # directory is classified as a file (is_dir(follow_symlinks=False)
            # is what pathlib uses), which is also what stops symlink cycles.
            if (
                snapshot is None and is_dir and not follow_symlinks
                and entry.is_symlink()
            ):
                is_dir = False
            # The store's own names, as a native walk reports them, so that
            # entries whose names differ only after a "?" stay apart.
            name = entry._store_name()
            (dirnames if is_dir else filenames).append(name)
            if is_dir and snapshot is not None:
                listed_dirs.add(name)

        if top_down:
            yield wrapper, dirnames, filenames
        # Re-reads dirnames after the yield, so caller pruning is honored.
        # Joined through the driver: a directory whose key holds a "?" is
        # entered at that key, not at the key without its query.
        for name in dirnames:
            yield from self._walk_fallback(
                wrapper.with_wrapped(wrapper._wrapped / name),
                top_down=top_down,
                on_error=on_error,
                follow_symlinks=follow_symlinks,
                known_dir=name in listed_dirs,
            )
        if not top_down:
            yield wrapper, dirnames, filenames


class UPathAdapter(GenericAdapter):
    """universal-pathlib: rmdir recurses by default, copy/move are native."""

    def rmdir(self, wrapper: tx.Any, *, recursive: bool) -> None:
        # Bare rmdir() would delete a non-empty tree; always be explicit.
        wrapper._wrapped.rmdir(recursive=recursive)

    def copy(self, wrapper: tx.Any, target: tx.Any, **_: tx.Any) -> None:
        wrapper._wrapped.copy(target)

    def move(self, wrapper: tx.Any, target: tx.Any) -> None:
        wrapper._wrapped.move(target)

    def walk(
        self,
        wrapper: tx.Any,
        *,
        top_down: bool,
        on_error: tx.Any,
        follow_symlinks: bool,
    ) -> tx.Iterator[tx.Tuple[tx.Any, tx.List[str], tx.List[str]]]:
        # UPath's own walk lists a directory, drops the listing's types, and
        # then asks every child whether it is a directory. Walking from one
        # detailed listing per directory gives the same tree for one call.
        if not _lists_with_fsspec(wrapper._wrapped):
            yield from super().walk(
                wrapper,
                top_down=top_down,
                on_error=on_error,
                follow_symlinks=follow_symlinks,
            )
            return
        yield from self._walk_fallback(
            wrapper,
            top_down=top_down,
            on_error=on_error,
            follow_symlinks=follow_symlinks,
        )

    def _listing(
        self, wrapper: tx.Any, *, known_dir: bool
    ) -> tx.List[tx.Any]:
        wrapped = wrapper._wrapped
        if not _lists_with_fsspec(wrapped):
            return super()._listing(wrapper, known_dir=known_dir)
        # The children UPath's iterdir gives, from fs.ls(detail=True): the
        # directory itself, "."/".." and a repeated name are not children.
        sep = getattr(getattr(wrapped, "parser", None), "sep", "/")
        base = wrapped.parent if wrapped.parts[-1:] == ("",) else wrapped
        fs = base.fs
        base_path = base.path
        base_name = _drop_suffix(base_path, sep)
        found: tx.Dict[str, tx.Any] = {}
        try:
            listing = fs.ls(base_path, detail=True)
        except FileNotFoundError:
            # As UPath's iterdir reports a missing directory.
            raise NotADirectoryError(str(wrapped)) from None
        for info in listing:
            snapshot = ListedType.of(info)
            name = info.get("name", "") if isinstance(info, dict) else info
            name = _drop_suffix(name, sep)
            if name == base_name:
                continue
            name = name.rpartition(sep)[2]
            if name in (".", ".."):
                continue
            # A file and a directory can share a name on an object store;
            # one child, a directory if either is (as isdir() answers).
            if name in found and (
                found[name] is None or found[name].is_dir()
            ):
                continue
            found[name] = snapshot
        if not found and not known_dir and not fs.isdir(base_path):
            # An empty listing is an empty directory, or no directory at
            # all; ask again without a cached listing, as UPath does.
            fs.invalidate_cache(base_path)
            if not fs.isdir(base_path):
                raise NotADirectoryError(str(wrapped))
        rebuild = getattr(base, "with_segments", None)
        return [
            wrapper._listed(
                rebuild(base_path, name) if callable(rebuild)
                else base / name,
                snapshot,
            )
            for name, snapshot in found.items()
        ]


def _drop_suffix(text: str, suffix: str) -> str:
    return text[: -len(suffix)] if text.endswith(suffix) else text


class CloudPathAdapter(GenericAdapter):
    """cloudpathlib: no recursive rmdir (use rmtree); copy/move are native."""

    def rmdir(self, wrapper: tx.Any, *, recursive: bool) -> None:
        if recursive:
            wrapper._wrapped.rmtree()
        else:
            wrapper._wrapped.rmdir()

    def copy(
        self,
        wrapper: tx.Any,
        target: tx.Any,
        *,
        follow_symlinks: bool,
        preserve_metadata: bool,
    ) -> None:
        wrapper._wrapped.copy(
            target,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )

    def move(self, wrapper: tx.Any, target: tx.Any) -> None:
        wrapper._wrapped.move(target)


_GENERIC = GenericAdapter()
_REGISTRY: tx.List[tx.Tuple[type, GenericAdapter]] = []
_CACHE: tx.Dict[type, GenericAdapter] = {}


def register_driver(base: type, adapter: GenericAdapter) -> None:
    """Register `adapter` for a driver base class and its subclasses.

    A later registration wins over an earlier one for an object matching
    both, so a small adapter for a specific class overrides a family default.
    """
    _REGISTRY.insert(0, (base, adapter))
    _CACHE.clear()


def adapter_for(wrapped: tx.Any) -> GenericAdapter:
    """The adapter for a wrapped object; the generic one if none registered."""
    kind = type(wrapped)
    adapter = _CACHE.get(kind)
    if adapter is None:
        adapter = _GENERIC
        for base, candidate in _REGISTRY:
            if isinstance(wrapped, base):
                adapter = candidate
                break
        _CACHE[kind] = adapter
    return adapter


def _register_known_drivers() -> None:
    try:
        from upath import UPath

        register_driver(UPath, UPathAdapter())
    except ImportError:  # pragma: no cover
        pass
    try:
        # cloudpathlib's local test doubles subclass CloudPath too, so one
        # registration covers both the real and the local backends.
        from cloudpathlib import CloudPath

        register_driver(CloudPath, CloudPathAdapter())
    except ImportError:  # pragma: no cover
        pass


_register_known_drivers()
