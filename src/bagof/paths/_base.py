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

from . import _select
from ._constants import (
    ACCESSOR_MEMBERS,
    ADAPTER_MEMBERS,
    COMPUTED_MEMBERS,
    LOCAL_PROTOCOLS,
    SCHEME_RE,
)
from ._errors import UnsupportedPathOperation
from ._protocols import canonical_scheme, traits_for
from ._spec import BY_NAME

_MISSING = object()


class BaseWrapper:
    """Wrap a path-like object and expose it through one uniform surface."""

    __slots__ = ("_wrapped",)

    # Which front-end family this wrapper belongs to. Only wrappers of the
    # same family compare equal (a sync Path never equals an async one).
    _family: tx.ClassVar[str] = "sync"

    def __init__(self, path: tx.Any, *, driver: tx.Any = None) -> None:
        if isinstance(path, str):
            path = _build_from_string(path, driver)
        else:
            if driver is not None:
                raise TypeError(
                    "driver= applies only to a URL string; a path object is "
                    "wrapped as it is (its own type is already its driver)"
                )
            if isinstance(path, BaseWrapper):
                path = path._wrapped
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
        """A path from a URI.

        A ``file://`` URI becomes a local path; a URI of another scheme is
        built through the ordinary constructor's driver selection, so
        ``Path.from_uri("s3://...")`` and ``Path("s3://...")`` agree.
        """
        match = SCHEME_RE.match(uri)
        scheme = match.group(1).lower() if match else ""
        if scheme and scheme not in LOCAL_PROTOCOLS:
            return cls(uri)
        # The stdlib is case-sensitive about "file:"; normalise the scheme
        # (the constructor does the same for a remote URL).
        if match is not None:
            uri = scheme + uri[match.end(1):]
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
            # Check the driver *class*, not the instance: these accessors are
            # properties, and some (universal-pathlib's `fs`/`info`) build the
            # filesystem when read -- which can raise ImportError for a
            # missing backend SDK. The class carries the descriptor unread.
            has = hasattr(type(self._wrapped), name)
            if name == "bucket":
                # bucket is also derivable from the drive of a bucketed
                # protocol, even when the driver has no bucket attribute.
                return has or (
                    traits_for(self.protocol).bucketed and bool(self.drive)
                )
            return has
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
            if match and canonical_scheme(match.group(1)) != canonical_scheme(
                self.protocol
            ):
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
        """The bucket the path lives in.

        Delegated to the driver where it has one (cloudpathlib), else derived
        from the drive of a bucketed protocol (``s3``, ``gs``, ``az``, ...);
        a non-bucketed protocol (local, memory) has no bucket and raises.
        """
        value = getattr(self._wrapped, "bucket", _MISSING)
        if value is not _MISSING:
            return value
        if traits_for(self.protocol).bucketed:
            drive = self.drive
            if drive:
                return drive
            hint = "this path names no bucket (it is the bucketed root)"
        else:
            hint = "only a bucketed protocol (s3, gs, az, ...) has a bucket"
        raise UnsupportedPathOperation(
            "bucket", driver=self._wrapped, hint=hint
        )

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
        else:
            # Fold scheme aliases (s3/s3a, gs/gcs) to their canonical name, so
            # two spellings of the same store compare and hash equal.
            protocol = canonical_scheme(protocol)
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


def _build_from_string(text: str, driver: tx.Any) -> tx.Any:
    """Turn a string into a driver path: local, selected, or via ``driver=``.

    An explicit ``driver`` (a path class or ``str -> path`` callable) wins. A
    plain path or a ``file://``/``local://`` URI becomes a stdlib
    ``pathlib.Path``. A remote scheme, or an fsspec chain like
    ``simplecache::s3://...``, is handed to driver selection.
    """
    if driver is not None:
        return driver(text)
    match = SCHEME_RE.match(text)
    if match is None:
        if "::" in text and "://" in text:
            # An fsspec chain (simplecache::s3://...) with no leading
            # scheme://. The inner "://" is what marks it a URL, so a plain
            # local filename that merely contains "::" stays a local path.
            return _select.build(text, "")
        return LocalPath(text)
    scheme = match.group(1).lower()
    if scheme in LOCAL_PROTOCOLS:
        return _local_from_url(text, scheme)
    # URL schemes are case-insensitive (RFC 3986) but a backend may reject a
    # mixed-case one; lower-case only the scheme, leaving the (possibly
    # case-sensitive) remainder of the URL untouched.
    text = scheme + text[match.end(1):]
    return _select.build(text, scheme)


def _local_from_url(text: str, scheme: str) -> LocalPath:
    """A local path from a ``file://`` / ``local://`` URL, on any version."""
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    parsed = urlparse(text)
    if scheme == "file":
        return LocalPath(url2pathname(parsed.path))
    # local://<netloc><path> -- keep whatever follows the scheme as the path.
    return LocalPath(url2pathname(parsed.netloc + parsed.path) or ".")


def _local_from_file_uri(uri: str) -> LocalPath:  # pragma: no cover
    """Build a local path from a ``file://`` URI on pathlib < 3.13.

    Only the ``file`` scheme maps to a local path; anything else needs a
    driver to interpret it and is refused, the way the constructor refuses a
    scheme-ful string.
    """
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    match = SCHEME_RE.match(uri)
    scheme = match.group(1).lower() if match else ""
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
