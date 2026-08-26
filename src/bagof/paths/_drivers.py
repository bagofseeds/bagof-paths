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
the generic one with no registration.
"""

from __future__ import annotations

import os
import shutil

import typing_extensions as tx

from ._constants import LOCAL_PROTOCOLS
from ._errors import UnsupportedPathOperation


class GenericAdapter:
    """Divergent operations for any path-like object, by duck-typing.

    The behavior here is the safe default; a named adapter overrides only
    where its driver needs different handling.
    """

    def rmdir(self, wrapper: tx.Any, *, recursive: bool) -> None:
        wrapped = wrapper._wrapped
        if not recursive:
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
    ) -> tx.Any:
        if wrapper.protocol not in LOCAL_PROTOCOLS:
            raise UnsupportedPathOperation(
                "copy",
                driver=wrapper._wrapped,
                hint="this driver has no native copy",
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
        return target

    def move(self, wrapper: tx.Any, target: tx.Any) -> tx.Any:
        try:
            wrapper._wrapped.replace(target)
            return target
        except (OSError, AttributeError):
            self.copy(
                wrapper, target,
                follow_symlinks=True, preserve_metadata=True,
            )
            wrapper._wrapped.unlink()
            return target

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
            walk = native(
                top_down=top_down,
                on_error=on_error,
                follow_symlinks=follow_symlinks,
            )
            for dirpath, dirnames, filenames in walk:
                yield wrapper.with_wrapped(dirpath), dirnames, filenames
            return
        yield from self._walk_fallback(
            wrapper, top_down=top_down, on_error=on_error,
        )

    def _walk_fallback(
        self, wrapper: tx.Any, *, top_down: bool, on_error: tx.Any,
    ) -> tx.Iterator[tx.Tuple[tx.Any, tx.List[str], tx.List[str]]]:
        # Synthesized for pathlib < 3.12; follow_symlinks is not honored
        # because the underlying is_dir predates that keyword there.
        try:
            entries = list(wrapper.iterdir())
        except OSError as error:
            if on_error is not None:
                on_error(error)
            return
        dirnames, filenames = [], []
        for entry in entries:
            (dirnames if entry.is_dir() else filenames).append(entry.name)
        if top_down:
            yield wrapper, dirnames, filenames
        for name in dirnames:
            yield from self._walk_fallback(
                wrapper / name, top_down=top_down, on_error=on_error,
            )
        if not top_down:
            yield wrapper, dirnames, filenames


class UPathAdapter(GenericAdapter):
    """universal-pathlib: rmdir recurses by default, copy/move are native."""

    def rmdir(self, wrapper: tx.Any, *, recursive: bool) -> None:
        # Bare rmdir() would delete a non-empty tree; always be explicit.
        wrapper._wrapped.rmdir(recursive=recursive)

    def copy(
        self, wrapper: tx.Any, target: tx.Any, **_: tx.Any
    ) -> tx.Any:
        return wrapper._wrapped.copy(target)

    def move(self, wrapper: tx.Any, target: tx.Any) -> tx.Any:
        return wrapper._wrapped.move(target)


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
    ) -> tx.Any:
        return wrapper._wrapped.copy(
            target,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
        )

    def move(self, wrapper: tx.Any, target: tx.Any) -> tx.Any:
        return wrapper._wrapped.move(target)


_GENERIC = GenericAdapter()
_REGISTRY: tx.List[tx.Tuple[type, GenericAdapter]] = []
_CACHE: tx.Dict[type, GenericAdapter] = {}


def register_driver(base: type, adapter: GenericAdapter) -> None:
    """Register ``adapter`` for a driver base class and its subclasses."""
    _REGISTRY.append((base, adapter))
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
    except ImportError:
        pass
    try:
        from cloudpathlib import CloudPath

        register_driver(CloudPath, CloudPathAdapter())
    except ImportError:
        pass
    try:
        # The local test doubles do not subclass CloudPath.
        from cloudpathlib.local import LocalPath

        register_driver(LocalPath, CloudPathAdapter())
    except ImportError:
        pass


_register_known_drivers()
