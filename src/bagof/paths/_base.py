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

from ._constants import (
    ACCESSOR_MEMBERS,
    ADAPTER_MEMBERS,
    COMPUTED_MEMBERS,
    LOCAL_PROTOCOLS,
    SCHEME_RE,
)
from ._errors import UnsupportedPathOperation
from ._spec import BY_NAME

_MISSING = object()


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

    # -- local-filesystem constructors -------------------------------------
    # home/cwd/from_uri build a fresh path rather than wrap an existing one.
    # They are inherently local, so they return a wrapper of this class over a
    # stdlib path; on the async wrapper they stay synchronous, like the
    # constructor and the lexical members.
    @classmethod
    def home(cls) -> tx.Self:
        """A path for the user's home directory (local filesystem)."""
        return cls(LocalPath.home())

    @classmethod
    def cwd(cls) -> tx.Self:
        """A path for the current working directory (local filesystem)."""
        return cls(LocalPath.cwd())

    @classmethod
    def from_uri(cls, uri: str) -> tx.Self:
        """A path from a ``file://`` URI (local filesystem).

        A URI naming another scheme (``s3://``, ``memory://``, ...) needs a
        driver to interpret it; wrap a driver path built from the URI instead.
        """
        native = getattr(LocalPath, "from_uri", None)
        if native is None:  # pathlib gained from_uri in 3.13
            return cls(_local_from_file_uri(uri))  # pragma: no cover
        return cls(native(uri))

    # -- the wrapped object -------------------------------------------------
    @property
    def wrapped(self) -> tx.Any:
        """The underlying path object this wrapper delegates to."""
        return self._wrapped

    def _delegate_attr(self, name: str) -> tx.Any:
        """Return a driver attribute, or raise if the driver lacks it."""
        value = getattr(self._wrapped, name, _MISSING)
        if value is _MISSING:
            raise UnsupportedPathOperation(name, driver=self._wrapped)
        return value

    # -- capability introspection ------------------------------------------
    def supports(self, name: str) -> bool:
        """Whether ``name`` is wired for this path.

        Answers "is this operation available" -- by delegation or by a
        synthesized fallback -- not "will a call succeed on this particular
        path". It is a static property of the wrapped object, not a
        filesystem probe.
        """
        member = BY_NAME.get(name)
        if member is not None:
            if hasattr(self._wrapped, member.name):
                return True
            if member.fallback and all(
                self.supports(n) for n in member.needs
            ):
                return True
            return False
        if name in ADAPTER_MEMBERS:
            return self._supports_adapter_member(name)
        if name in COMPUTED_MEMBERS:
            return True
        if name in ACCESSOR_MEMBERS:
            # Answered on the wrapped object: reading the property itself
            # would raise UnsupportedPathOperation for a driver that lacks it.
            return hasattr(self._wrapped, name)
        # Location properties (protocol, path, drive, ...) resolve directly.
        return not name.startswith("_") and hasattr(self, name)

    def _supports_adapter_member(self, name: str) -> bool:
        wrapped = self._wrapped
        if name == "rmdir":
            return hasattr(wrapped, "rmdir")
        if name == "walk":
            return hasattr(wrapped, "walk") or self.supports("iterdir")
        # copy / copy_into / move / move_into
        return self.protocol in LOCAL_PROTOCOLS or hasattr(wrapped, "copy")

    def capabilities(self) -> tx.FrozenSet[str]:
        """The set of members that are wired for this path (see supports)."""
        names = (
            set(BY_NAME) | ADAPTER_MEMBERS | COMPUTED_MEMBERS
            | ACCESSOR_MEMBERS
        )
        return frozenset(name for name in names if self.supports(name))

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

    def _coerce_target(self, target: tx.Any) -> tx.Any:
        """A copy/move/rename target as a bare driver path of this kind.

        A string is turned into a path *through the wrapped object* so its
        driver configuration (UPath storage options, a cloudpathlib client)
        carries onto the target; a string naming a different scheme is
        refused, the same way the constructor refuses one.
        """
        if isinstance(target, BaseWrapper):
            return target._wrapped
        if isinstance(target, str):
            match = SCHEME_RE.match(target)
            if match and match.group(1) != self.protocol:
                raise ValueError(
                    f"a target like {target!r} names a different scheme; "
                    "pass a wrapped path instead of a string"
                )
            wrapped = self._wrapped
            if hasattr(wrapped, "with_segments"):
                return wrapped.with_segments(target)
            return type(wrapped)(target)
        return target

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

    # -- driver-specific accessors -----------------------------------------
    # Metadata and handles a particular driver exposes. Each delegates to the
    # wrapped object and raises UnsupportedPathOperation when it is absent, so
    # driver-native state is reachable without reaching for `.wrapped`. They
    # return driver-native values unchanged and stay synchronous on both
    # wrappers, matching how the underlying drivers expose them.
    @property
    def info(self) -> tx.Any:
        """A metadata accessor for the path (universal-pathlib/cloud)."""
        return self._delegate_attr("info")

    @property
    def storage_options(self) -> tx.Any:
        """The driver's storage options (universal-pathlib/fsspec)."""
        return self._delegate_attr("storage_options")

    @property
    def fs(self) -> tx.Any:
        """The underlying fsspec filesystem (universal-pathlib)."""
        return self._delegate_attr("fs")

    @property
    def bucket(self) -> tx.Any:
        """The bucket the path lives in (cloudpathlib)."""
        return self._delegate_attr("bucket")

    @property
    def key(self) -> tx.Any:
        """The object key within the bucket (cloudpathlib)."""
        return self._delegate_attr("key")

    @property
    def client(self) -> tx.Any:
        """The cloud client backing the path (cloudpathlib)."""
        return self._delegate_attr("client")

    @property
    def cloud_prefix(self) -> tx.Any:
        """The scheme prefix of the cloud path, e.g. ``"s3://"``."""
        return self._delegate_attr("cloud_prefix")

    @property
    def fspath(self) -> tx.Any:
        """The local cache path for the object (cloudpathlib)."""
        return self._delegate_attr("fspath")

    @property
    def etag(self) -> tx.Any:
        """The stored entity tag of the object (cloudpathlib)."""
        return self._delegate_attr("etag")

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


def _local_from_file_uri(uri: str) -> LocalPath:  # pragma: no cover
    """Build a local path from a ``file://`` URI on pathlib < 3.13.

    Only the ``file`` scheme maps to a local path; anything else needs a
    driver to interpret it and is refused, the way the constructor refuses a
    scheme-ful string.
    """
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    match = SCHEME_RE.match(uri)
    scheme = match.group(1) if match else ""
    if scheme != "file":
        raise ValueError(
            f"cannot build a local path from {uri!r}; a URL scheme like "
            f"{scheme!r} needs a driver to interpret it"
        )
    return LocalPath(url2pathname(urlparse(uri).path))


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
