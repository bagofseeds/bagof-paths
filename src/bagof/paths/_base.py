"""The base wrapper: composition, identity, derivation, and the location
properties that must read the same across drivers.

``BaseWrapper`` holds the wrapped path in ``_wrapped`` and delegates to it.
It is deliberately slotted, so a plain wrapper is cheap; a subclass that adds
state (a ``read_only`` flag, say) simply does not declare ``__slots__`` and
gets a ``__dict__``, which :meth:`with_wrapped` carries onto every derived
path for free.

The ``protocol``/``path``/``drive``/``root``/``anchor`` properties here use
generic duck-typing that works on any path-like object; the driver adapter
layer refines them for the handful of known drivers that diverge.
"""

from __future__ import annotations

import os
from pathlib import Path as LocalPath

import typing_extensions as tx

from ._constants import LOCAL_PROTOCOLS
from ._errors import UnsupportedPathOperation


class BaseWrapper(os.PathLike):
    """Wrap a path-like object and expose it through one uniform surface."""

    __slots__ = ("_wrapped",)

    def __init__(self, path: tx.Any, *, driver: tx.Any = None) -> None:
        # Driver selection arrives with the adapter layer; for now a string
        # becomes a stdlib pathlib path and an already-built path-like object
        # is wrapped as-is.
        if isinstance(path, BaseWrapper):
            path = path._wrapped
        elif isinstance(path, str):
            path = LocalPath(path)
        elif not isinstance(path, os.PathLike):
            raise TypeError(
                f"cannot wrap {type(path).__name__!r}; expected a path "
                "string or a path-like object"
            )
        self._wrapped = path

    # -- the wrapped object -------------------------------------------------
    @property
    def wrapped(self) -> tx.Any:
        """The underlying path object this wrapper delegates to."""
        return self._wrapped

    @property
    def wrapped_type(self) -> type:
        """The type of the underlying path object."""
        return type(self._wrapped)

    # -- derivation ---------------------------------------------------------
    def with_wrapped(self, wrapped: tx.Any) -> tx.Self:
        """Return a path of this wrapper's own type around ``wrapped``.

        Every internal operation that produces a new path (``parent``,
        ``joinpath``, ``/``, ...) goes through here, so a subclass's extra
        state is preserved on derived paths. Subclasses that keep state in
        slots, or want different derivation, override this.
        """
        new = object.__new__(type(self))
        new._wrapped = wrapped
        state = getattr(self, "__dict__", None)
        if state:
            new.__dict__.update(state)
        return new

    # -- location (generic; refined per-driver by the adapter layer) --------
    @property
    def protocol(self) -> str:
        """The URL scheme, or ``""`` for a local path."""
        return getattr(self._wrapped, "protocol", "") or ""

    @property
    def path(self) -> str:
        """The path within the protocol (fsspec convention)."""
        value = getattr(self._wrapped, "path", None)
        return value if isinstance(value, str) else str(self._wrapped)

    @property
    def drive(self) -> str:
        """The drive (or bucket), or ``""`` when there is none."""
        return getattr(self._wrapped, "drive", "") or ""

    @property
    def root(self) -> str:
        """The root, or ``""`` when there is none."""
        return getattr(self._wrapped, "root", "") or ""

    @property
    def anchor(self) -> str:
        """The concatenation of drive and root."""
        value = getattr(self._wrapped, "anchor", None)
        return value if value is not None else self.drive + self.root

    # -- identity -----------------------------------------------------------
    def _key(self) -> tx.Tuple[str, str]:
        # Driver-independent: two wrappers pointing at the same location are
        # equal regardless of which driver backs them.
        return (self.protocol, str(self._wrapped))

    def __eq__(self, other: tx.Any) -> bool:
        if isinstance(other, BaseWrapper):
            return self._key() == other._key()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self._wrapped)!r})"

    def __str__(self) -> str:
        return str(self._wrapped)

    def __fspath__(self) -> str:
        if self.protocol in LOCAL_PROTOCOLS:
            return os.fspath(self._wrapped)
        raise UnsupportedPathOperation(
            "__fspath__",
            driver=self._wrapped,
            hint=(
                "a non-local path has no local filesystem representation; "
                "use str(path) for the URI, or open()/read_bytes() for data"
            ),
        )

    def __truediv__(self, other: tx.Any) -> tx.Self:
        if isinstance(other, BaseWrapper):
            other = other._wrapped
        return self.with_wrapped(self._wrapped / other)

    def __rtruediv__(self, other: tx.Any) -> tx.Self:
        if isinstance(other, BaseWrapper):
            other = other._wrapped
        return self.with_wrapped(other / self._wrapped)
