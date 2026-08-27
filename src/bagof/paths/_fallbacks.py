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

import io

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

    Decoding goes through the same text layer ``open()`` uses, so newline
    translation and the encoding defaults match a native ``read_text``.
    """
    stream = io.TextIOWrapper(
        io.BytesIO(wrapper.read_bytes()),
        encoding=encoding, errors=errors, newline=newline,
    )
    try:
        return stream.read()
    finally:
        stream.detach()


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

    Encoding goes through the same text layer ``open()`` uses, so newline
    translation matches a native ``write_text``. Returns the number of
    characters written.
    """
    if not isinstance(data, str):
        raise TypeError(
            f"write_text() argument must be str, not {type(data).__name__}"
        )
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(
        buffer, encoding=encoding, errors=errors, newline=newline
    )
    count = stream.write(data)
    stream.flush()
    wrapper.write_bytes(buffer.getvalue())
    stream.detach()
    return count


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
    "read_bytes": read_bytes,
    "read_text": read_text,
    "write_bytes": write_bytes,
    "write_text": write_text,
    "with_stem": with_stem,
    "is_relative_to": is_relative_to,
}
