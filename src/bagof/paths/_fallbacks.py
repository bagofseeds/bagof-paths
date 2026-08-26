"""Synthesized fallbacks.

When the wrapped object lacks a member but does provide more primitive ones,
the member is synthesized here rather than raising. Each function takes the
wrapper as its first argument and uses the wrapper's own (delegated) members,
so a fallback composes on top of whatever the driver does implement -- text
from bytes, bytes from ``open``.

The engine looks a fallback up by name (the ``fallback`` field of a spec
:class:`~bagof.paths._spec.Member`) and only calls it once the member's
``needs`` are satisfied, so a fallback may assume its primitives exist.
"""

from __future__ import annotations

import typing_extensions as tx


def read_bytes(wrapper: tx.Any) -> bytes:
    """``read_bytes`` from ``open``."""
    with wrapper.open("rb") as handle:
        return handle.read()


def read_text(
    wrapper: tx.Any,
    encoding: tx.Optional[str] = None,
    errors: tx.Optional[str] = None,
    newline: tx.Optional[str] = None,
) -> str:
    """``read_text`` from ``read_bytes``.

    Newline translation is not applied; a driver that needs it should
    implement ``read_text`` itself.
    """
    data = wrapper.read_bytes()
    return data.decode(encoding or "utf-8", errors or "strict")


def write_bytes(wrapper: tx.Any, data: tx.Any) -> int:
    """``write_bytes`` from ``open``; returns the number of bytes written."""
    view = memoryview(data).cast("B")
    with wrapper.open("wb") as handle:
        return handle.write(view)


def write_text(
    wrapper: tx.Any,
    data: str,
    encoding: tx.Optional[str] = None,
    errors: tx.Optional[str] = None,
    newline: tx.Optional[str] = None,
) -> int:
    """``write_text`` from ``write_bytes``.

    Newline translation is not applied; a driver that needs it should
    implement ``write_text`` itself.
    """
    if not isinstance(data, str):
        raise TypeError(
            f"write_text() argument must be str, not {type(data).__name__}"
        )
    encoded = data.encode(encoding or "utf-8", errors or "strict")
    return wrapper.write_bytes(encoded)


# name -> synthesis function, resolved by the engine from a Member.fallback.
FALLBACKS: tx.Dict[str, tx.Callable[..., tx.Any]] = {
    "read_bytes": read_bytes,
    "read_text": read_text,
    "write_bytes": write_bytes,
    "write_text": write_text,
}
