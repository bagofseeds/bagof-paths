"""Path arguments: the one place a path given to a method is coerced.

A method that takes a path accepts what `pathlib` accepts: a string, any
[os.PathLike][], a `pathlib` path, a driver object, or one of the wrappers.
Before the argument reaches a driver it is brought to one of two shapes:

- `path_arg` -- for the wrapper and the engine: a wrapper becomes its driver,
  a plain `os.PathLike` (one that is not itself a driver) becomes its
  `os.fspath` string, and a string or a driver object is kept;
- `path_text` -- for a driver that works on text (the dependency-free
  lexical driver, the native async driver): the same, then a driver object
  becomes its string, which is its URL.

Both refuse `bytes`, as `pathlib` does: decoding is safe for a local name but
not for a URL or a cloud key, which are UTF-8.
"""

from __future__ import annotations

import os

import typing_extensions as tx

from ._constants import SCHEME_RE


def _type_error(value: tx.Any) -> TypeError:
    # pathlib's own wording, so the message reads the same on every driver.
    return TypeError(
        "argument should be a str or an os.PathLike object where "
        f"__fspath__ returns a str, not {type(value).__name__!r}"
    )


def is_driver(value: tx.Any) -> bool:
    """Whether `value` is a path object in its own right, not a bare name.

    A driver (a `pathlib` path, a `UPath`, a `CloudPath`, ...) carries its
    own scheme and configuration, so it is passed on as it is. An object
    that only has `__fspath__` (or an `os.DirEntry`) is a name, read with
    `os.fspath`. A cloud driver must never be read that way: cloudpathlib's
    `__fspath__` downloads the object.
    """
    return hasattr(value, "parts") or hasattr(value, "protocol")


def path_arg(value: tx.Any) -> tx.Any:
    """A path argument as a driver object or a string.

    A wrapper becomes its driver, a plain `os.PathLike` its `os.fspath`
    string; `bytes` (and a `__fspath__` that returns bytes) is refused with
    `TypeError`. Anything else is passed on for the driver to judge.
    """
    from ._base import BaseWrapper

    if isinstance(value, str):
        return value
    if isinstance(value, BaseWrapper):
        return value._wrapped
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _type_error(value)
    if not is_driver(value) and hasattr(type(value), "__fspath__"):
        text = os.fspath(value)
        if not isinstance(text, str):
            raise _type_error(text)
        return text
    return value


def path_text(value: tx.Any) -> str:
    """A path argument as text, for a driver that joins and parses strings.

    As `path_arg`, then a driver object becomes its string (a URL for a
    remote driver). Anything that is neither a string, an `os.PathLike` nor
    a path object is refused with `TypeError`, as `pathlib` refuses it.
    """
    value = path_arg(value)
    if isinstance(value, str):
        return value
    if is_driver(value):
        return str(value)
    raise _type_error(value)


def is_url(text: str) -> bool:
    """Whether `text` starts with a `scheme://`, and so names a location."""
    return SCHEME_RE.match(text) is not None
