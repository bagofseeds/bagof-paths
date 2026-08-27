"""Synthesized fallbacks.

When the wrapped object lacks a member but does provide more primitive ones,
the member is synthesized here rather than raising. Each function takes the
wrapper as its first argument and uses the wrapper's own (delegated) members.

The engine looks a fallback up by name (the ``fallback`` field of a spec
:class:`~bagof.paths._spec.Member`) and only calls it once the member's
``needs`` are satisfied, so a fallback may assume its primitives exist.
"""

from __future__ import annotations

import typing_extensions as tx


def with_stem(wrapper: tx.Any, stem: str) -> tx.Any:
    """``with_stem`` from ``with_name`` (pathlib gained it in 3.9)."""
    wrapped = wrapper._wrapped
    return wrapped.with_name(stem + wrapped.suffix)


def is_relative_to(wrapper: tx.Any, other: tx.Any) -> bool:
    """``is_relative_to`` from ``relative_to`` (pathlib gained it in 3.9)."""
    try:
        wrapper._wrapped.relative_to(other)
    except ValueError:
        return False
    return True


# name -> synthesis function, resolved by the engine from a Member.fallback.
FALLBACKS: tx.Dict[str, tx.Callable[..., tx.Any]] = {
    "with_stem": with_stem,
    "is_relative_to": is_relative_to,
}
