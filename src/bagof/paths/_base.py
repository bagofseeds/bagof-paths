"""The base wrapper: composition, identity, derivation, and the location
properties that must read the same across drivers.

``BaseWrapper`` holds the wrapped path in ``_wrapped`` and delegates to it.
It is slotted, and the concrete wrappers (``Path``/``AsyncPath``) add
``__slots__ = ()`` so a plain wrapper carries no ``__dict__`` and rejects
stray attributes; a subclass that adds state (a ``read_only`` flag, say)
simply does not declare ``__slots__`` and gets a ``__dict__`` that
:meth:`with_wrapped` carries onto every derived path.

``BaseWrapper`` does not inherit :class:`os.PathLike`; because it defines
``__fspath__``, ``isinstance(path, os.PathLike)`` is still true through
``os.PathLike``'s subclass hook, and not inheriting is what lets the slots
actually hold.

The ``protocol``/``path``/``drive``/``root``/``anchor`` properties use
generic duck-typing that works on any path-like object -- trust a matching
string attribute, else parse the URL scheme out of ``str()``; the driver
adapter layer refines them for the known drivers that diverge.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path as LocalPath

import typing_extensions as tx

from ._constants import LOCAL_PROTOCOLS, SCHEME_RE
from ._errors import UnsupportedPathOperation
from ._spec import BY_NAME


class BaseWrapper:
    """Wrap a path-like object and expose it through one uniform surface."""

    __slots__ = ("_wrapped",)

    # Which front-end family this wrapper belongs to. Only wrappers of the
    # same family compare equal (a sync Path never equals an async one).
    _family: tx.ClassVar[str] = "sync"

    def __init__(self, path: tx.Any, *, driver: tx.Any = None) -> None:
        if driver is not None:
            raise NotImplementedError(
                "driver selection lands with the adapter layer; for now wrap "
                "an already-constructed driver path object"
            )
        if isinstance(path, BaseWrapper):
            path = path._wrapped
        elif isinstance(path, str):
            if SCHEME_RE.match(path):
                raise ValueError(
                    f"a URL string like {path!r} needs a driver to "
                    "interpret its scheme; wrap a driver path object "
                    "(e.g. UPath/AnyPath), or wait for driver selection"
                )
            path = LocalPath(path)
        elif not _is_path_shaped(path):
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

    # -- capability introspection ------------------------------------------
    def supports(self, name: str) -> bool:
        """Whether ``name`` is wired for this path.

        Answers "is this operation available" -- by delegation or by a
        synthesized fallback -- not "will a call succeed on this particular
        path". It is a static property of the wrapped object, not a
        filesystem probe.
        """
        member = BY_NAME.get(name)
        if member is None:
            # The location properties (protocol, path, drive, ...) and other
            # non-underscore members resolve directly.
            return not name.startswith("_") and hasattr(self, name)
        if hasattr(self._wrapped, member.name):
            return True
        if member.fallback and all(self.supports(n) for n in member.needs):
            return True
        return False

    # -- derivation ---------------------------------------------------------
    def with_wrapped(self, wrapped: tx.Any) -> tx.Self:
        """Return a path of this wrapper's own type around ``wrapped``.

        Every internal operation that produces a new path (``parent``,
        ``joinpath``, ``/``, ...) goes through here, so a subclass's extra
        state is preserved on derived paths. State is carried by a shallow
        copy (like ``dataclasses.replace``); a subclass holding *mutable*
        state that derived paths must not share should override this.
        """
        new = copy.copy(self)
        new._wrapped = wrapped
        return new

    # -- location (generic; refined per-driver by the adapter layer) --------
    @property
    def protocol(self) -> str:
        """The URL scheme, or ``""`` for a local path."""
        value = getattr(self._wrapped, "protocol", None)
        if isinstance(value, str):
            return value
        match = SCHEME_RE.match(str(self._wrapped))
        return match.group(1) if match else ""

    @property
    def path(self) -> str:
        """The path within the protocol (fsspec convention, scheme-less)."""
        value = getattr(self._wrapped, "path", None)
        if isinstance(value, str):
            return value
        text = str(self._wrapped)
        match = SCHEME_RE.match(text)
        return text[match.end():] if match else text

    @property
    def drive(self) -> str:
        """The drive (or bucket), or ``""`` when there is none."""
        value = getattr(self._wrapped, "drive", None)
        return value if isinstance(value, str) else ""

    @property
    def root(self) -> str:
        """The root, or ``""`` when there is none."""
        value = getattr(self._wrapped, "root", None)
        return value if isinstance(value, str) else ""

    @property
    def anchor(self) -> str:
        """The concatenation of drive and root."""
        value = getattr(self._wrapped, "anchor", None)
        return value if isinstance(value, str) else self.drive + self.root

    # -- identity -----------------------------------------------------------
    def _key(self) -> tx.Tuple[str, str]:
        # Driver-independent: two wrappers of the same family pointing at the
        # same location are equal regardless of which driver backs them.
        # Built from the canonical properties, not raw str(wrapped), so every
        # adapter refinement to protocol/path repairs identity automatically.
        protocol = self.protocol
        if protocol in LOCAL_PROTOCOLS:
            protocol = ""
        return (protocol, self.path)

    def __eq__(self, other: tx.Any) -> bool:
        if isinstance(other, BaseWrapper) and self._family == other._family:
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


def _is_path_shaped(obj: tx.Any) -> bool:
    """Whether ``obj`` looks enough like a path to wrap.

    Deliberately lenient: the flagship non-local ``UPath`` refuses
    ``__fspath__``, so gating on that would reject the very drivers this
    package exists for. Any object that walks like a path -- ``__fspath__``,
    ``parts``, or ``path`` -- is accepted, and unsupported members degrade
    one at a time rather than at construction.
    """
    return (
        hasattr(obj, "__fspath__")
        or hasattr(obj, "parts")
        or hasattr(obj, "path")
    )
