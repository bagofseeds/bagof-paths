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
import sys

import typing_extensions as tx

from . import _glob
from ._errors import UnsupportedPathOperation
from ._pathinfo import (
    LSTAT,
    Facts,
    ask,
    info_view,
    listed_facts,
    stat_view,
    typed_facts,
)


def _accepts(func: tx.Any, name: str) -> bool:
    """Whether `func` accepts a keyword argument called `name`."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


def _is_stock_local(wrapped: tx.Any) -> bool:
    """Whether `wrapped` is a stdlib local path listed the stdlib way.

    Then `os.scandir` gives the same children as its `iterdir`, with their
    types. A subclass that redefines how it lists or classifies is not.
    """
    if not isinstance(wrapped, pathlib.Path):
        return False
    if getattr(wrapped, "_closed", False):  # pragma: no cover - 3.8 only
        # A pathlib path closed by its (deprecated) context manager refuses
        # I/O; leave it to its own methods to say so.
        return False
    kind = type(wrapped)
    shims = _upath_local_shims(kind)
    return all(
        getattr(kind, name, None)
        in (getattr(pathlib.Path, name), shims.get(name, pathlib.Path))
        for name in ("iterdir", "is_dir", "is_file", "is_symlink", "exists")
    )


def _upath_local_shims(kind: type) -> tx.Dict[str, tx.Any]:
    """The members universal-pathlib's local flavor backports onto `kind`.

    Before 3.13 its local path redefines `is_dir`/`is_file` (and, before
    3.12, `exists`) only to take a newer signature, then calls pathlib's own:
    it still lists and classifies the stdlib way. Empty for any other class;
    universal-pathlib is never imported here.
    """
    local = getattr(
        sys.modules.get("upath.implementations.local"), "LocalPath", None
    )
    if not (isinstance(local, type) and issubclass(kind, local)):
        return {}
    return {
        name: getattr(local, name)
        for name in ("is_dir", "is_file", "exists")
        if name in vars(local)
    }


def _is_stock(wrapped: tx.Any, base: type, names: tx.Iterable[str]) -> bool:
    """Whether `wrapped`'s class keeps `base`'s own `names`."""
    kind = type(wrapped)
    return all(
        getattr(kind, name, None) is getattr(base, name, None)
        for name in names
    )


def _is_listed_dir(facts: tx.Optional[Facts]) -> bool:
    """Whether a listed child is (or may be) a directory."""
    return facts is None or facts.get(LSTAT, "dir") is not False


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

        A child carries the listing's own answers (its `pathinfo` record)
        where the driver's listing gives them (`os.scandir` for a stdlib
        local path); otherwise it is a plain `iterdir()` child, classified
        by asking it. A known directory (one a parent listing reported)
        skips any check that the path is a directory.
        """
        wrapped = wrapper._wrapped
        if _is_stock_local(wrapped):
            with os.scandir(wrapped) as scan:
                return [
                    wrapper._listed(
                        wrapped / entry.name, Facts(entry=entry, listed=True)
                    )
                    for entry in scan
                ]
        return list(wrapper.iterdir())

    # -- pathinfo: listings that keep their detail, and the store's answers
    def listed_children(
        self, wrapper: tx.Any
    ) -> tx.Optional[tx.Iterator[tx.Any]]:
        """`iterdir()` children with the listing's answers, or None.

        None leaves `iterdir` to the driver: its listing says nothing more
        than the names. A stdlib local path is listed with `os.scandir`
        (before 3.14, whose pathlib does so itself and keeps each entry as
        the child's `info`), at the moment and with the errors its own
        `iterdir` has.
        """
        wrapped = wrapper._wrapped
        if not _is_stock_local(wrapped):
            return None
        if sys.version_info >= (3, 14):
            if type(wrapped).info is not pathlib.Path.info:
                return None
            # pathlib's own children, untouched (their `info` included);
            # reading `info` on a listed child returns its directory entry.
            return (
                wrapper._listed(child, Facts(entry=child.info, listed=True))
                for child in wrapped.iterdir()
            )

        def child(entry: os.DirEntry) -> tx.Any:
            return wrapper._listed(
                wrapped / entry.name, Facts(entry=entry, listed=True)
            )

        if sys.version_info >= (3, 13):
            # pathlib 3.13 reads the directory when iterdir() is called.
            with os.scandir(str(wrapped)) as scan:
                entries = list(scan)
            return (child(entry) for entry in entries)

        def lazily() -> tx.Iterator[tx.Any]:
            # Earlier pathlib reads it when the iteration starts.
            with os.scandir(wrapped) as scan:
                entries = list(scan)
            for entry in entries:
                yield child(entry)

        return lazily()

    def glob_tree(self, wrapper: tx.Any) -> tx.Optional[_glob.Tree]:
        """The subtree below `wrapper` in one recursive listing, or None.

        Asked by the shared glob when a `**` is about to walk a directory.
        None (the default) walks it one listing per directory, as pathlib
        does: right for a store with symbolic links, which a recursive
        listing does not follow.
        """
        return None

    def query_facts(
        self, wrapper: tx.Any, view: str, key: str, facts: Facts
    ) -> tx.Dict[str, tx.Any]:
        """What the store says about `wrapper` (a synchronous `Path`).

        Returns at least the fact `key` of `view`, and every other one the
        same call gave. A stdlib local path answers a whole view from
        `os.lstat` or `os.stat`; any other driver is asked through the
        path's own status members.
        """
        if _is_stock_local(wrapper._wrapped):
            return stat_view(wrapper._wrapped, view)
        return ask(wrapper, view, key, facts)

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
        listed_dirs: tx.Dict[str, Facts] = {}
        for entry in entries:
            facts = entry._listing_facts()
            try:
                if facts is not None:
                    # The listing's answer (no query per child), but for
                    # where a followed link leads, which is asked once.
                    is_dir = entry.pathinfo.is_dir(
                        follow_symlinks=follow_symlinks
                    )
                else:
                    is_dir = entry.is_dir()
                    # Mirror native walk: with follow_symlinks=False a
                    # symlinked directory is classified as a file
                    # (is_dir(follow_symlinks=False) is what pathlib uses),
                    # which is also what stops symlink cycles.
                    if is_dir and not follow_symlinks and entry.is_symlink():
                        is_dir = False
            except OSError as error:
                if on_error is not None:
                    on_error(error)
                continue
            # The store's own names, as a native walk reports them, so that
            # entries whose names differ only after a "?" stay apart.
            name = entry._store_name()
            (dirnames if is_dir else filenames).append(name)
            if is_dir and facts is not None:
                listed_dirs[name] = facts

        if top_down:
            yield wrapper, dirnames, filenames
        # Re-reads dirnames after the yield, so caller pruning is honored.
        # Joined through the driver: a directory whose key holds a "?" is
        # entered at that key, not at the key without its query. A listed
        # directory carries what its listing said.
        for name in dirnames:
            facts = listed_dirs.get(name)
            child = wrapper._wrapped / name
            yield from self._walk_fallback(
                wrapper._listed(child, facts) if facts is not None
                else wrapper.with_wrapped(child),
                top_down=top_down,
                on_error=on_error,
                follow_symlinks=follow_symlinks,
                known_dir=facts is not None,
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
        # Derived from universal-pathlib's UPath.iterdir (MIT; NOTICE.md).
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
            facts = listed_facts(info)
            name = info.get("name", "") if isinstance(info, dict) else info
            name = _drop_suffix(name, sep)
            if name == base_name:
                continue
            name = name.rpartition(sep)[2]
            if name in (".", ".."):
                continue
            # A file and a directory can share a name on an object store;
            # one child, a directory if either is (as isdir() answers).
            if name in found and _is_listed_dir(found[name]):
                continue
            found[name] = facts
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
                facts,
            )
            for name, facts in found.items()
        ]

    def listed_children(
        self, wrapper: tx.Any
    ) -> tx.Optional[tx.Iterator[tx.Any]]:
        wrapped = wrapper._wrapped
        if not _lists_with_fsspec(wrapped):
            return super().listed_children(wrapper)
        if not _is_stock(wrapped, _upath_class(), ("iterdir",)):
            # A flavor with its own iterdir (http, a local file URL, ...)
            # lists its own way.
            return None

        def lazily() -> tx.Iterator[tx.Any]:
            # UPath's iterdir is a generator: it lists when iterated.
            yield from self._listing(wrapper, known_dir=False)

        return lazily()

    def glob_tree(self, wrapper: tx.Any) -> tx.Optional[_glob.Tree]:
        # One fs.find(): a single paginated listing on an object store
        # (s3fs, gcsfs, adlfs). A local or symbolic-link store is walked one
        # directory at a time, as pathlib walks it.
        wrapped = wrapper._wrapped
        if (
            not _lists_with_fsspec(wrapped) or wrapper.is_local()
            or not _is_stock(wrapped, _upath_class(), ("iterdir",))
            or getattr(wrapped, "_relative_base", None) is not None
        ):
            return None
        sep = getattr(getattr(wrapped, "parser", None), "sep", "/")
        base = wrapped.parent if wrapped.parts[-1:] == ("",) else wrapped
        base_path = _drop_suffix(base.path, sep)
        try:
            found = base.fs.find(base_path, withdirs=True, detail=True)
        except (OSError, ValueError):
            return None
        if not isinstance(found, dict):  # a backend that ignores detail
            return None
        rebuild = getattr(base, "with_segments", None)

        def entries() -> tx.Iterator[tx.Tuple[str, str, tx.Any]]:
            for name, info in found.items():
                name = _drop_suffix(name, sep)
                if not name.startswith(base_path + sep):
                    continue  # the directory itself
                parent, _, leaf = name.rpartition(sep)
                if leaf in ("", ".", ".."):
                    continue
                child = (
                    rebuild(parent, leaf) if callable(rebuild)
                    else base / name[len(base_path) + 1:]
                )
                yield parent, name, wrapper._listed(child, listed_facts(info))

        return _glob.tree_from(base_path, _fsspec_key, entries())

    def query_facts(
        self, wrapper: tx.Any, view: str, key: str, facts: Facts
    ) -> tx.Dict[str, tx.Any]:
        # One fs.info() answers a whole view (UPath's own is_dir, is_file
        # and exists each make that call).
        wrapped = wrapper._wrapped
        if not _lists_with_fsspec(wrapped):
            return super().query_facts(wrapper, view, key, facts)
        fs = wrapped.fs
        try:
            info = fs.info(wrapped.path)
        except (OSError, ValueError):
            # fsspec's local info() follows a link, so a dangling one is
            # missing there; the link itself exists.
            if view == LSTAT and _is_link(fs, wrapped.path):
                return {"exists": True, "symlink": True}
            return {"exists": False}
        if not isinstance(info, dict):
            return ask(wrapper, view, key, facts)
        return info_view(info, view)


def _fsspec_key(path: tx.Any) -> str:
    """A UPath's place in its store, as fsspec's listings name it."""
    wrapped = path._wrapped
    sep = getattr(getattr(wrapped, "parser", None), "sep", "/")
    if wrapped.parts[-1:] == ("",):
        wrapped = wrapped.parent
    return _drop_suffix(wrapped.path, sep)


def _is_link(fs: tx.Any, name: str) -> bool:
    try:
        return bool(fs.islink(name))
    except (AttributeError, OSError, ValueError):
        return False


def _upath_class() -> type:
    from upath import UPath

    return UPath


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

    def listed_children(
        self, wrapper: tx.Any
    ) -> tx.Optional[tx.Iterator[tx.Any]]:
        # cloudpathlib's iterdir drops what its listing says about each
        # child; its lazy walk keeps it, from the same single listing.
        walk = getattr(wrapper._wrapped, "walk", None)
        if not callable(walk) or not _accepts(walk, "lazy"):
            return None

        def lazily() -> tx.Iterator[tx.Any]:
            rows = walk(lazy=True)
            try:
                top, dirnames, filenames = next(rows)
            finally:
                rows.close()
            for names, is_dir in ((dirnames, True), (filenames, False)):
                for name in names:
                    yield wrapper._listed(top / name, typed_facts(is_dir))

        return lazily()

    def glob_tree(self, wrapper: tx.Any) -> tx.Optional[_glob.Tree]:
        # cloudpathlib's walk lists the whole subtree in one call.
        walk = getattr(wrapper._wrapped, "walk", None)
        if not callable(walk):
            return None

        def entries() -> tx.Iterator[tx.Tuple[str, str, tx.Any]]:
            for top, dirnames, filenames in walk():
                key = _cloud_key(top)
                for names, is_dir in ((dirnames, True), (filenames, False)):
                    for name in names:
                        child = top / name
                        yield key, _cloud_key(child), wrapper._listed(
                            child, typed_facts(is_dir)
                        )

        return _glob.tree_from(
            _cloud_key(wrapper._wrapped),
            lambda path: _cloud_key(path._wrapped),
            entries(),
        )


def _cloud_key(path: tx.Any) -> str:
    return str(path).rstrip("/")


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
