"""Whether a wrapped driver's members are coroutines.

A driver is *async* when its I/O members are coroutines (``anyio.Path``,
``trio.Path``, ...). This is a property of the driver class, so it is worked
out once and cached. ``AsyncPath`` awaits such a driver directly; ``Path``
refuses it (a synchronous method returning an un-awaited coroutine is a trap).
"""

from __future__ import annotations

import inspect

import typing_extensions as tx

_CACHE: tx.Dict[type, bool] = {}


def _probe(wrapped: tx.Any) -> bool:
    for name in ("exists", "open", "iterdir", "stat"):
        member = getattr(wrapped, name, None)
        if member is not None and (
            inspect.iscoroutinefunction(member)
            or inspect.isasyncgenfunction(member)
        ):
            return True
    return False


def is_async_driver(wrapped: tx.Any) -> bool:
    """Whether ``wrapped``'s members are coroutines (cached by type)."""
    kind = type(wrapped)
    cached = _CACHE.get(kind)
    if cached is None:
        cached = _probe(wrapped)
        _CACHE[kind] = cached
    return cached
