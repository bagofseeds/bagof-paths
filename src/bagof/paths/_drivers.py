"""Driver adapters: where operations that diverge across backends are
reconciled in one place.

Most of the surface is uniform and goes through the engine. A few operations
are not -- ``rmdir``'s recursion, native ``copy``/``move``, ``walk`` -- because
the backends genuinely disagree:

- ``UPath``'s bare ``rmdir()`` deletes a *non-empty* tree (its ``recursive``
  defaults to ``True``), so the wrapper must always pass ``recursive=False``;
- ``cloudpathlib`` has no recursive ``rmdir`` at all, but a separate
  ``rmtree()``;
- ``pathlib`` gained ``copy`` in 3.14 and ``walk`` in 3.12.

Each such operation lives on an adapter: a :class:`GenericAdapter` that works
on any (including unknown) driver by duck-typing, and named subclasses that
override only the specific points a known driver diverges on. Adapters are
selected by driver type through :func:`adapter_for`; an unknown driver gets
the generic one with no registration, and a later :func:`register_driver`
wins over an earlier one for the same object.
"""

from __future__ import annotations

import errno
import inspect
import os
import shutil

import typing_extensions as tx

from ._constants import LOCAL_PROTOCOLS
from ._errors import UnsupportedPathOperation


def _accepts(func: tx.Any, name: str) -> bool:
    """Whether ``func`` accepts a keyword argument called ``name``."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


class GenericAdapter:
    """Divergent operations for any path-like object, by duck-typing.

    The behavior here is the safe default; a named adapter overrides only
    where its driver needs different handling. Copy/move perform the
    operation and return nothing -- the wrapper builds the returned path.
    """

    def _target_is_local(self, wrapper: tx.Any, target: tx.Any) -> bool:
        return wrapper.with_wrapped(target).protocol in LOCAL_PROTOCOLS

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
        if wrapper.protocol in LOCAL_PROTOCOLS:
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
        source_local = wrapper.protocol in LOCAL_PROTOCOLS
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

    def _walk_fallback(
        self,
        wrapper: tx.Any,
        *,
        top_down: bool,
        on_error: tx.Any,
        follow_symlinks: bool,
    ) -> tx.Iterator[tx.Tuple[tx.Any, tx.List[str], tx.List[str]]]:
        # Synthesized for pathlib < 3.12.
        try:
            entries = list(wrapper.iterdir())
        except OSError as error:
            if on_error is not None:
                on_error(error)
            return
        dirnames: tx.List[str] = []
        filenames: tx.List[str] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError as error:
                if on_error is not None:
                    on_error(error)
                continue
            # Mirror native walk: with follow_symlinks=False a symlinked
            # directory is classified as a file (is_dir(follow_symlinks=False)
            # is what pathlib uses), which is also what stops symlink cycles.
            if is_dir and not follow_symlinks and entry.is_symlink():
                is_dir = False
            (dirnames if is_dir else filenames).append(entry.name)

        if top_down:
            yield wrapper, dirnames, filenames
        # Re-reads dirnames after the yield, so caller pruning is honored.
        for name in dirnames:
            yield from self._walk_fallback(
                wrapper / name,
                top_down=top_down,
                on_error=on_error,
                follow_symlinks=follow_symlinks,
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
    """Register ``adapter`` for a driver base class and its subclasses.

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
