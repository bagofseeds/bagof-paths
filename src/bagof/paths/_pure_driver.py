"""A dependency-free lexical driver for a remote URL.

This is the driver behind a path that has no installed backend: it implements
only the lexical (pure) surface, entirely with the standard library, so a
remote URL can be parsed and manipulated with neither universal-pathlib nor
cloudpathlib present. ``AsyncFSPath`` is the shape precedent -- lexical members
over a :class:`~pathlib.PurePosixPath`.

The path is a **wrapped driver object**, not a wrapper itself. ``Path`` (or the
public ``PurePath``) holds one in ``_wrapped`` and delegates to it through the
engine, exactly as it does a ``UPath`` or a ``CloudPath``. So this class
presents the shape the engine expects: the location attributes
(``protocol``/``path``/``drive``/``root``), the lexical members
(``name``/``parent``/``joinpath``/``/`` ...), and a ``str()`` that reassembles
the URL.

The location split follows universal-pathlib exactly, so a path built here
compares and hashes equal to the same URL built on a real backend. For a
bucketed scheme the first component is the drive and the remainder is an
absolute key path, so a bucket root normalizes to a trailing slash
(``s3://bucket`` has path ``bucket/``) and the parent of a bucket root is the
bucket root again.

A lexical member is delegated to the wrapped ``PurePosixPath``, so its
normalization is the standard library's. A ``.`` segment and a doubled slash
are collapsed, which universal-pathlib preserves. Ordinary keys, which contain
neither, are unaffected.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import typing_extensions as tx

from ._constants import SCHEME_RE
from ._protocols import traits_for


def _split_url(scheme: str, url: str) -> tx.Tuple[str, PurePosixPath]:
    """The drive and the key path of a URL, by the scheme's traits.

    A bucketed scheme splits its first component off as the drive and keeps the
    remainder as an absolute key path. Another absolute scheme keeps the whole
    remainder as an absolute path with no drive. Anything else keeps the
    remainder verbatim.
    """
    rest = url.split("://", 1)[1] if "://" in url else url
    traits = traits_for(scheme)
    if traits.bucketed:
        bucket, _, key = rest.partition("/")
        return bucket, PurePosixPath("/" + key)
    if traits.absolute:
        return "", PurePosixPath("/" + rest.lstrip("/"))
    return "", PurePosixPath(rest)


class PureCloudPath:
    """A remote path that supports only lexical operations, using no backend.

    This class carries the URL scheme, the drive (the bucket, for a bucketed
    scheme), and the key path. It exposes the same location attributes and
    lexical members a real backend would, and it reassembles the URL in
    ``str()``. It implements no I/O; an I/O member raises through the engine
    with a hint to install a backend.
    """

    __slots__ = ("_scheme", "_drive", "_pure", "_options")

    def __init__(
        self,
        scheme: str,
        drive: str,
        pure: PurePosixPath,
        options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> None:
        self._scheme = scheme
        self._drive = drive
        self._pure = pure
        self._options = dict(options or {})

    @classmethod
    def from_url(
        cls,
        url: str,
        scheme: str,
        options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> PureCloudPath:
        """Build a path from a full URL, splitting off the drive and key."""
        drive, pure = _split_url(scheme, url)
        return cls(scheme, drive, pure, options)

    def _derive(self, pure: PurePosixPath) -> PureCloudPath:
        """A sibling path with the same scheme and drive, at ``pure``."""
        return PureCloudPath(self._scheme, self._drive, pure, self._options)

    # -- location ----------------------------------------------------------
    @property
    def protocol(self) -> str:
        return self._scheme

    @property
    def drive(self) -> str:
        return self._drive

    @property
    def root(self) -> str:
        return "/" if self._pure.is_absolute() else ""

    @property
    def path(self) -> str:
        # The fsspec-style path: the drive followed by the key. The key path's
        # own string already carries its leading slash, so a bucket root
        # (key "/") reads as "bucket/", matching universal-pathlib.
        return self._drive + str(self._pure)

    @property
    def storage_options(self) -> tx.Dict[str, tx.Any]:
        return self._options

    # -- lexical properties ------------------------------------------------
    @property
    def name(self) -> str:
        return self._pure.name

    @property
    def stem(self) -> str:
        return self._pure.stem

    @property
    def suffix(self) -> str:
        return self._pure.suffix

    @property
    def suffixes(self) -> tx.List[str]:
        return self._pure.suffixes

    @property
    def parts(self) -> tx.Tuple[str, ...]:
        parts = self._pure.parts
        if self._drive and parts:
            # The absolute key path starts with "/"; present the anchor as the
            # drive plus root, as a real backend does ("bucket/", ...).
            return (self._drive + self.root,) + parts[1:]
        return parts

    @property
    def parent(self) -> PureCloudPath:
        return self._derive(self._pure.parent)

    @property
    def parents(self) -> tx.Tuple[PureCloudPath, ...]:
        return tuple(self._derive(p) for p in self._pure.parents)

    # -- lexical methods ---------------------------------------------------
    def joinpath(self, *segments: tx.Any) -> PureCloudPath:
        return self._derive(self._pure.joinpath(*[str(s) for s in segments]))

    def __truediv__(self, other: tx.Any) -> PureCloudPath:
        return self.joinpath(other)

    def with_name(self, name: str) -> PureCloudPath:
        return self._derive(self._pure.with_name(name))

    def with_suffix(self, suffix: str) -> PureCloudPath:
        return self._derive(self._pure.with_suffix(suffix))

    def as_posix(self) -> str:
        # A real backend returns the whole URL here, not just the key.
        return str(self)

    def as_uri(self) -> str:
        return str(self)

    def is_absolute(self) -> bool:
        return self._pure.is_absolute()

    def is_reserved(self) -> bool:
        # Reserved names are a Windows-filesystem concept; a remote key never
        # is one.
        return False

    def _locus(self) -> PurePosixPath:
        """The whole location as one path, so the drive joins the comparison.

        A bucketed key alone would compare relative to a key in a different
        bucket, so ``relative_to`` folds the drive into an absolute path and
        compares those.
        """
        if self._pure.is_absolute():
            return PurePosixPath("/", self._drive, *self._pure.parts[1:])
        return self._pure

    def _other_locus(self, other: tx.Any) -> PurePosixPath:
        if isinstance(other, PureCloudPath):
            return other._locus()
        text = str(other)
        if "://" in text:
            match = SCHEME_RE.match(text)
            scheme = match.group(1).lower() if match else self._scheme
            drive, pure = _split_url(scheme, text)
            return PureCloudPath(scheme, drive, pure)._locus()
        return PurePosixPath(text)

    def relative_to(
        self, other: tx.Any, walk_up: bool = False
    ) -> PureCloudPath:
        base = self._other_locus(other)
        if walk_up:
            rel = self._locus().relative_to(base, walk_up=True)
        else:
            rel = self._locus().relative_to(base)
        # The result is a bare key relative to the base, carrying no drive.
        return PureCloudPath(self._scheme, "", rel, self._options)

    # -- display -----------------------------------------------------------
    def __str__(self) -> str:
        # Match universal-pathlib: scheme://<path without a leading slash>.
        return "{}://{}".format(self._scheme, self.path.lstrip("/"))

    def __repr__(self) -> str:
        return f"PureCloudPath({str(self)!r})"

    # -- graceful I/O degradation ------------------------------------------
    @property
    def _unsupported_hint(self) -> str:
        # The engine reads this when it cannot delegate or synthesize a member,
        # so an I/O call on a backendless path explains the remedy.
        prefix = f"{self._scheme}://"
        return (
            f"no backend is installed to perform I/O on {prefix!r}; install "
            "universal-pathlib or cloudpathlib, or pass driver= a path class"
        )
