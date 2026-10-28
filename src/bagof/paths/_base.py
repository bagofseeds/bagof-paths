"""The base wrapper: composition, identity, derivation, and the location
properties that must read the same across drivers.

`BaseWrapper` holds the wrapped path in `_wrapped` and delegates to it.
It is slotted, and the concrete wrappers (`Path`/`AsyncPath`) add
`__slots__ = ()` so a plain wrapper carries no `__dict__` and rejects
stray attributes; a subclass that adds state (a `read_only` flag, say)
simply does not declare `__slots__` and gets a `__dict__` that
`with_wrapped` carries onto every derived path.

`BaseWrapper` does not inherit [os.PathLike][]; because it defines
`__fspath__`, `isinstance(path, os.PathLike)` is still true through
[os.PathLike][]'s subclass hook, and not inheriting is what lets the slots
actually hold.

The `protocol`/`path`/`drive`/`root`/`anchor` properties use
generic duck-typing that works on any path-like object -- trust a matching
string attribute, else parse the URL scheme out of `str()`; the driver
adapter layer refines them for the known drivers that diverge.
"""

from __future__ import annotations

import copy
import os
import pathlib
from pathlib import Path as LocalPath

import typing_extensions as tx

from . import _select
from ._args import is_driver, is_url, path_arg
from ._constants import (
    ACCESSOR_MEMBERS,
    ADAPTER_MEMBERS,
    COMPUTED_MEMBERS,
    LOCAL_PROTOCOLS,
    SCHEME_RE,
    STAGED_MEMBERS,
)
from ._detect import is_async_driver
from ._errors import NoLocalPathError, UnsupportedPathOperation
from ._protocols import (
    canonical_scheme,
    merged_storage_options,
    path_part,
    strip_url_query,
    traits_for,
)
from ._spec import BY_NAME

_MISSING = object()



class BaseWrapper:
    """Wrap a path-like object and expose it through one uniform surface."""

    __slots__ = ("_wrapped",)

    # Which front-end family this wrapper belongs to: whether its I/O
    # methods are synchronous or coroutines. It does not enter identity --
    # a Path and an AsyncPath at the same location compare equal.
    _family: tx.ClassVar[str] = "sync"

    def __init__(
        self,
        path: tx.Any,
        *,
        driver: tx.Any = None,
        storage_options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
    ) -> None:
        """Wrap a path object, or build one from a path string or URL.

        Parameters
        ----------
        path:
            A path-like object to wrap as it is, or a string. A plain string
            is a local path; a `scheme://` URL selects a driver.
        driver:
            For a URL string, a path class or `str -> path` factory to use
            instead of the automatic selection. Not valid for a path object.
        storage_options:
            For a URL string, connection options (endpoint, credentials, ...)
            forwarded to the driver, over any per-scheme defaults. Not valid
            for a local path or a path object.
        """
        if isinstance(path, str):
            path = self._from_string(path, driver, storage_options)
        else:
            if driver is not None:
                raise TypeError(
                    "driver= applies only to a URL string; a path object is "
                    "wrapped as it is (its own type is already its driver)"
                )
            if storage_options:
                raise TypeError(
                    "storage_options= applies only to a URL string; a path "
                    "object already carries its own connection"
                )
            if isinstance(path, BaseWrapper):
                path = path._wrapped
            elif not _is_path_shaped(path):
                raise TypeError(
                    f"cannot wrap {type(path).__name__!r}; expected a path "
                    "string or a path-like object"
                )
        if self._family == "sync" and is_async_driver(path):
            # A synchronous method over an async driver returns un-awaited
            # coroutines -- a silent trap. Refuse it at construction, the same
            # way the other unusable-argument cases above are refused.
            name = getattr(path, "__name__", None) or type(path).__name__
            raise TypeError(
                f"{name} is an asynchronous driver; wrap it with AsyncPath, "
                "not Path"
            )
        self._wrapped = path

    def _from_string(
        self,
        text: str,
        driver: tx.Any,
        storage_options: tx.Optional[tx.Mapping[str, tx.Any]],
    ) -> tx.Any:
        """Build the wrapped driver for a URL/path string.

        The default is the shared selection. `AsyncPath` overrides this to
        prefer a natively-async driver for a remote URL before falling back
        to the shared selection (a synchronous driver run in a thread).
        """
        return _build_from_string(text, driver, storage_options)

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

        A `file:` URI becomes a local path, read as [pathlib.Path.from_uri][]
        reads it from Python 3.14 on, on every version: percent-escapes are
        decoded, and a query or fragment is not part of the path. A URI of
        another scheme is built through the ordinary constructor's driver
        selection, so `Path.from_uri("s3://...")` and `Path("s3://...")`
        agree.
        """
        match = SCHEME_RE.match(uri)
        scheme = match.group(1).lower() if match else ""
        if scheme and scheme != "file":
            return cls(uri)
        return cls(_local_from_file_uri(uri))

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
        """Whether `name` is wired for this path.

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
        if name in COMPUTED_MEMBERS or name in STAGED_MEMBERS:
            # Computed here, so available wherever the wrapper defines it
            # (local_copy and staged are on Path/AsyncPath, not PurePath).
            return hasattr(self, name)
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
            | ACCESSOR_MEMBERS | STAGED_MEMBERS
        )
        return frozenset(name for name in names if self.supports(name))

    # -- derivation ---------------------------------------------------------
    def with_wrapped(self, wrapped: tx.Any) -> tx.Self:
        """Return a path of this wrapper's own type around `wrapped`.

        Every internal operation that produces a new path (`parent`,
        `joinpath`, `/`, ...) goes through here, so a subclass's extra
        state is preserved on derived paths. State is carried by a shallow
        copy (like `dataclasses.replace`); a subclass holding *mutable*
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
        refused, the same way the constructor refuses one. A plain
        `os.PathLike` is read as its string; `bytes` is refused.
        """
        target = path_arg(target)
        if isinstance(target, str):
            if not self._same_scheme(target):
                raise ValueError(
                    f"a target like {target!r} names a different scheme; "
                    "pass a wrapped path instead of a string"
                )
            wrapped = self._wrapped
            if hasattr(wrapped, "with_segments"):
                return wrapped.with_segments(target)
            return type(wrapped)(target)
        return target

    def _same_scheme(self, text: str) -> bool:
        """Whether a string names this path's scheme, or no scheme at all."""
        match = SCHEME_RE.match(text)
        if match is None:
            return True
        return canonical_scheme(match.group(1)) == canonical_scheme(
            self.protocol
        )

    def _join_args(
        self, segments: tx.Tuple[tx.Any, ...]
    ) -> tx.Tuple[tx.Self, tx.Tuple[tx.Any, ...]]:
        """The base and the remaining segments of a join, as pathlib reads it.

        Each segment is coerced like any path argument. On a remote path a
        URL string names a location of its own, as an absolute path does in
        `pathlib`: the join restarts from it (built through this path's
        driver, so its configuration carries over), exactly as it does for
        the same URL given as a wrapped path. A URL of another scheme is
        refused. A local path keeps `pathlib`'s reading of such a string.
        """
        base = self
        rest: tx.List[tx.Any] = []
        remote = not self.is_local()
        for segment in segments:
            segment = path_arg(segment)
            if remote and isinstance(segment, str) and is_url(segment):
                if not self._same_scheme(segment):
                    raise ValueError(
                        f"cannot join {segment!r} onto a "
                        f"{self.protocol + '://'!r} path: it names a "
                        "different scheme"
                    )
                base = self.with_wrapped(self._coerce_target(segment))
                rest = []
            else:
                rest.append(segment)
        return base, tuple(rest)

    # -- the path without its URL query -----------------------------------
    def _query_free(self) -> tx.Self:
        """This path without its URL query (and an http fragment).

        The lexical members run on this view, so a query (a token, a version
        id) is not part of the name, the parts or the parent, and a name
        moves from one store to another without it. Only an http URL has a
        fragment; elsewhere a `#` is part of the name.
        The path itself keeps them, since I/O needs them to reach the
        object. A derived path does not carry them, as in URL resolution
        (RFC 3986): it names a different object.
        """
        text = self._query_free_text(str(self._wrapped))
        if text == str(self._wrapped):
            return self
        # A driver that cannot rebuild itself from a string keeps its own
        # answer rather than lose its configuration.
        rebuilt = self._rebuilt(text)
        return self if rebuilt is None else rebuilt

    @staticmethod
    def _query_free_text(text: str) -> str:
        """A URL string without its query (and an http fragment)."""
        return path_part(text)

    def _rebuilt(self, text: str) -> tx.Optional[tx.Self]:
        """This path's driver rebuilt at the URL `text`, or `None`.

        The driver's own `with_segments` carries its configuration (storage
        options, a client) onto the new path; a driver without one cannot
        be rebuilt safely.
        """
        rebuild = getattr(self._wrapped, "with_segments", None)
        if not callable(rebuild):
            return None
        return self.with_wrapped(rebuild(text))

    def _store_name(self) -> str:
        """The name the driver itself gives this object.

        In a store whose keys hold `?` (s3, gs, memory, ...), two objects can
        differ only after it, so an operation that stays in the store names
        a path this way to keep them apart. `name`, which leaves the query
        out, is the portable name for another store. A path with no query
        has one name, so `name` (and any override of it) answers.
        """
        if self._query_free() is self:
            return self.name
        value = getattr(self._wrapped, "name", None)
        return value if isinstance(value, str) else self.name

    def _same_store(self, other: tx.Any) -> bool:
        """Whether the driver path `other` is in this path's store."""
        def store(protocol: str) -> str:
            if protocol in LOCAL_PROTOCOLS:
                return ""
            return canonical_scheme(protocol)

        theirs = self.with_wrapped(other).protocol
        return store(self.protocol) == store(theirs)

    def _query_free_arg(self, other: tx.Any) -> tx.Any:
        """A path argument without its URL query, as `_query_free` does.

        A driver object is read as a path of this kind, so its query is left
        out as a string's or a wrapper's is.
        """
        other = path_arg(other)
        if isinstance(other, str):
            return self._query_free_text(other)
        return self.with_wrapped(other)._query_free()._wrapped

    # -- location (generic; refined per-driver by the adapter layer) --------
    @property
    def protocol(self) -> str:
        """The URL scheme, or `""` for a local path."""
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
        """The drive (or bucket), or `""` when there is none."""
        value = getattr(self._wrapped, "drive", None)
        return value if isinstance(value, str) else ""

    @property
    def root(self) -> str:
        """The root, or `""` when there is none."""
        value = getattr(self._wrapped, "root", None)
        return value if isinstance(value, str) else ""

    @property
    def anchor(self) -> str:
        """The concatenation of drive and root."""
        value = getattr(self._wrapped, "anchor", None)
        return value if isinstance(value, str) else self.drive + self.root

    @classmethod
    def local_stage(cls) -> tx.Any:
        """A local directory shared by the local copies made inside it.

        While the stage is active, every `local_copy()` in the same thread
        or task goes into it, laid out as in its store, so copies taken at
        the same time keep their relative positions; it is removed, with
        anything left in it, when the block ends. A local path is never
        copied:

        ```pycon
        >>> with Path.local_stage():
        ...     with Path("/data/scan.nii").local_copy() as local:
        ...         print(local)
        /data/scan.nii
        ```

        ```python
        with Path.local_stage():
            with hdr.local_copy() as h, img.local_copy() as i:   # one store
                image = nibabel.load(h)       # finds the .img beside it
        ```

        A stage can also be passed as `local_copy(stage=...)` and removed
        with its `cleanup()`. Use `async with` with an `AsyncPath`.
        """
        from ._local_copy import LocalStage

        return LocalStage()

    def is_local(self) -> bool:
        """Whether the path names a file on the local file system.

        True for a plain path and for a `file://` URL; false for a remote
        URL. It reads only the path, never storage. A local path is the one
        `os.fspath` accepts, so it can be handed to a library that opens
        files by name; on any other path `os.fspath` raises
        [`NoLocalPathError`][bagof.paths.NoLocalPathError], a `TypeError`.

        ```pycon
        >>> Path("/data/scan.nii").is_local()
        True
        >>> Path("file:///data/scan.nii").is_local()
        True
        ```

        ```python
        Path("s3://bucket/scan.nii").is_local()     # False
        ```
        """
        return self.protocol in LOCAL_PROTOCOLS

    def _names_same_file(self, other: tx.Any) -> bool:
        """`samefile(other, strict=False)`: blocking, for both front-ends.

        Two local names lead to the same file when they resolve to it, or,
        where it does not exist yet, to the same existing directory and the
        same names below it. Two remote paths name the same object when they
        are equal. A local path and a remote one never do.
        """
        from ._pure import PurePath

        if not isinstance(other, BaseWrapper):
            other = PurePath(path_arg(other))
        if self.is_local() and other.is_local():
            return _local_identity(os.fspath(self)) == _local_identity(
                os.fspath(other)
            )
        return self._key() == other._key()

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
        """The driver's storage options (universal-pathlib/fsspec).

        This is the live connection mapping, so it returns any credentials
        the path was built with. Treat it as sensitive: it is not redacted.
        """
        return self._delegate_attr("storage_options")

    @property
    def fs(self) -> tx.Any:
        """The underlying fsspec filesystem (universal-pathlib)."""
        return self._delegate_attr("fs")

    @property
    def bucket(self) -> tx.Any:
        """The bucket the path lives in.

        Delegated to the driver where it has one (cloudpathlib), else derived
        from the drive of a bucketed protocol (`s3`, `gs`, `az`, ...);
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
        """The scheme prefix of the cloud path, e.g. `"s3://"`."""
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
    def _key(self) -> tx.Tuple[tx.Any, ...]:
        # Driver-independent: two paths pointing at the same location are
        # equal regardless of which driver -- or which front-end -- backs
        # them. Built from the canonical properties, not raw str(wrapped), so
        # every adapter refinement to protocol/path repairs identity.
        protocol = self.protocol
        if protocol in LOCAL_PROTOCOLS:
            # A local path also carries its flavour, as pathlib's identity
            # does: a POSIX path never equals a Windows one, and the hash
            # below is the flavour's own.
            return ("", self.path, _local_flavour(self._wrapped))
        # Fold scheme aliases (s3/s3a, gs/gcs) to their canonical name, so
        # two spellings of the same store compare and hash equal.
        return (canonical_scheme(protocol), self.path)

    def _foreign_key(
        self, other: tx.Any
    ) -> tx.Optional[tx.Tuple[tx.Any, ...]]:
        """The identity of an unwrapped path `other`, or `None`.

        A driver object (a `pathlib` path, a `UPath`, a `CloudPath`, an
        `anyio.Path`, ...) is read through the same location properties as
        a wrapper; a plain `os.PathLike` is read as the local name
        `os.fspath` gives. A string is never a path, and anything else is
        not comparable.
        """
        if isinstance(other, (str, bytes, bytearray, memoryview)):
            return None
        if not is_driver(other):
            if not hasattr(type(other), "__fspath__"):
                return None
            try:
                text = os.fspath(other)
            except TypeError:
                return None
            if not isinstance(text, str):
                return None
            other = pathlib.PurePath(text)
        try:
            return self.with_wrapped(other)._key()
        except (TypeError, ValueError):
            return None

    def __eq__(self, other: tx.Any) -> bool:
        """Whether `other` names the same location.

        Any path compares by where it points, whatever its type: another
        wrapper (`Path`, `PurePath`, `AsyncPath`), a `pathlib` path, a
        `UPath`, a `CloudPath`, or any [os.PathLike][]. A string never
        equals a path.

        ```pycon
        >>> import pathlib
        >>> Path("/data/scan.nii") == pathlib.Path("/data/scan.nii")
        True
        >>> pathlib.Path("/data/scan.nii") == Path("/data/scan.nii")
        True
        >>> Path("/data/scan.nii") == "/data/scan.nii"
        False
        ```

        Only the location counts: two paths to the same place are equal
        even when they carry different connection options.
        """
        if isinstance(other, BaseWrapper):
            return self._key() == other._key()
        key = self._foreign_key(other)
        if key is None:
            return NotImplemented
        return self._key() == key

    def __hash__(self) -> int:
        """A hash that agrees with equality.

        A local path hashes as the `pathlib` path at the same location, so a
        wrapper finds the `pathlib` path in a set or a dictionary, and the
        other way round. A remote path hashes as a universal-pathlib path at
        the same URL (for its canonical scheme). A cloudpathlib path, or a
        universal-pathlib path spelled with a scheme alias (`s3a://`),
        compares equal but hashes differently, so it does not find its
        wrapper in a set.
        """
        key = self._key()
        if not key[0]:
            return hash(key[2](key[1]))
        return hash(key)

    # -- pathlib 3.14+ interoperability -------------------------------------
    # pathlib 3.14 takes any argument with `with_segments` for one of its own
    # paths and reads two private members from it: `relative_to` reads
    # `_tail`, and `copy`/`copy_into`/`move` call `target._copy_from(...)`.
    # Both are present, unchanged, from 3.14.0 to 3.15. pathlib 3.15 also
    # reads `__vfspath__` from the target's class: `copy` names both paths
    # in the error when they are the same, and requires the member to do
    # so. They are answered here from the public surface only, so that a
    # wrapper argument behaves as the os.PathLike it is. No earlier pathlib
    # reads any of these names.
    @property
    def _tail(self) -> tx.List[str]:
        # pathlib's `_tail`: the parts after the anchor.
        parts = list(self.parts)
        return parts[1:] if self.anchor else parts

    def __vfspath__(self) -> str:
        # pathlib's own paths answer with `str(self)`: the path as it is
        # written, the URL for a remote path. pathlib uses it only to name
        # the path, never to open it.
        return str(self)

    def _copy_from(self, source: tx.Any, **kwargs: tx.Any) -> None:
        # pathlib's `copy` has checked the paths are distinct and calls this
        # to copy `source` (one of its own paths) here. Copy as it copies to
        # an os.PathLike: to this path's local file name, by pathlib itself.
        if not self.is_local():
            raise NoLocalPathError(
                "copy",
                driver=self._wrapped,
                hint=(
                    "pathlib copies only to a local file name, and this path "
                    "is remote"
                ),
            )
        if not isinstance(source, pathlib.Path):
            raise UnsupportedPathOperation(
                "copy",
                driver=source,
                hint="only a local pathlib path can be copied to this path "
                "from its own copy() method",
            )
        source.copy(source.with_segments(os.fspath(self)), **kwargs)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self._wrapped)!r})"

    def __str__(self) -> str:
        return str(self._wrapped)

    def __fspath__(self) -> str:
        if self.is_local():
            return os.fspath(self._wrapped)
        raise NoLocalPathError(
            "__fspath__",
            driver=self._wrapped,
            hint=(
                "a non-local path has no local filesystem representation; "
                "use str(path) for the URI, or open()/read_bytes() for data"
            ),
        )

    def __bytes__(self) -> bytes:
        """The local file name as bytes, as `os.fsencode` gives it.

        Only a local path has one; a remote path raises.
        """
        if self.is_local():
            return os.fsencode(os.fspath(self._wrapped))
        raise UnsupportedPathOperation(
            "__bytes__",
            driver=self._wrapped,
            hint=(
                "a non-local path has no local file name to encode; use "
                "str(path).encode() for the URL"
            ),
        )

    def __truediv__(self, other: tx.Any) -> tx.Self:
        base, rest = self._join_args((other,))
        if not rest:
            return base
        return base.with_wrapped(base._query_free()._wrapped / rest[0])

    def __rtruediv__(self, other: tx.Any) -> tx.Self:
        other = path_arg(other)
        return self.with_wrapped(other / self._wrapped)


def _build_from_string(
    text: str,
    driver: tx.Any,
    storage_options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
) -> tx.Any:
    """Turn a string into a driver path: local, selected, or via `driver=`.

    An explicit `driver` (a path class or `str -> path` callable) wins. A
    plain path or a `file://`/`local://` URI becomes a stdlib
    [pathlib.Path][]. A remote scheme, or an fsspec chain like
    `simplecache::s3://...`, is handed to driver selection.

    `storage_options` (endpoint, credentials, ...) are forwarded to the
    driver. They apply only to a remote URL: a local path has nowhere to send
    them, and passing them for one is a `TypeError`.
    """
    match = SCHEME_RE.match(text)
    scheme = match.group(1).lower() if match is not None else ""
    if driver is not None:
        # An explicit driver still gets the scheme's default connection
        # options (with any per-call override on top): the credentials belong
        # to the store, not to whichever factory builds the path.
        options = merged_storage_options(scheme, storage_options)
        return driver(text, **options) if options else driver(text)
    if match is None:
        if "::" in text and "://" in text:
            # An fsspec chain (simplecache::s3://...) with no leading
            # scheme://. The inner "://" is what marks it a URL, so a plain
            # local filename that merely contains "::" stays a local path.
            return _select.build(text, "", storage_options)
        _reject_local_storage_options(storage_options, text)
        return LocalPath(text)
    if scheme in LOCAL_PROTOCOLS:
        _reject_local_storage_options(storage_options, text)
        return _local_from_url(text, scheme)
    # URL schemes are case-insensitive (RFC 3986) but a backend may reject a
    # mixed-case one; lower-case only the scheme, leaving the (possibly
    # case-sensitive) remainder of the URL untouched.
    text = scheme + text[match.end(1):]
    return _select.build(text, scheme, storage_options)


def _reject_local_storage_options(
    storage_options: tx.Optional[tx.Mapping[str, tx.Any]], text: str
) -> None:
    if storage_options:
        raise TypeError(
            f"storage_options= applies only to a remote URL; {text!r} is a "
            "local path with no connection to configure"
        )


def _local_from_url(text: str, scheme: str) -> LocalPath:
    """A local path from a `file://` / `local://` URL, on any version."""
    if scheme == "file":
        return _local_from_file_uri(text)
    # local:// is fsspec's name for the local filesystem. As in any non-http
    # URL its query is not part of the path and a "#" is; the rest is read
    # as it is, with no percent-decoding, as fsspec reads it.
    text = strip_url_query(text, fragment=False)
    return LocalPath(text.split("://", 1)[1] or ".")


def _local_from_file_uri(uri: str) -> LocalPath:
    """A local path from a `file:` URI, as pathlib reads it from 3.14 on.

    A file URI is a URL (RFC 8089): its query and fragment are not part of the
    path, and a `?` or `#` in a file name is written `%3F` / `%23`, as
    `as_uri()` writes it. Percent-escapes are decoded with the filesystem
    encoding. An authority other than this machine names a network share: a
    UNC path on Windows, an error elsewhere. Earlier Pythons disagree (3.13
    keeps a literal `?`), so the rules are written here once rather than
    taken from the running interpreter. Derived from CPython 3.14's
    `urllib.request.url2pathname` (PSF-licensed; see NOTICE.md).
    """
    import sys
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(uri)
    if parts.scheme != "file":
        raise ValueError(f"URI does not start with 'file:': {uri!r}")
    authority, path = parts.netloc, parts.path
    local = authority in ("", "localhost") or authority == _hostname()
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        if authority[1:2] == ":":
            path = authority + path  # file://c:/file.txt
        elif not local:
            path = "//" + authority + path  # file://server/share/file.txt
        elif path[:3] == "///":
            path = path[1:]  # file://///server/share/file.txt
        else:
            if path[:1] == "/" and path[2:3] in (":", "|"):
                path = path[1:]  # the slash before a DOS drive
            if path[1:2] == "|":
                path = path[:1] + ":" + path[2:]  # an old-style drive bar
        path = path.replace("/", "\\")
    elif not local:
        raise ValueError(
            f"cannot build a local path from {uri!r}: it names the host "
            f"{authority!r}, not this machine"
        )
    text = unquote(
        path,
        encoding=sys.getfilesystemencoding(),
        errors=sys.getfilesystemencodeerrors(),
    )
    result = LocalPath(text)
    if not result.is_absolute():
        raise ValueError(f"URI is not absolute: {uri!r}")
    return result


def _hostname() -> tx.Optional[str]:
    """This machine's hostname, or `None` when it has none."""
    import socket

    try:
        return socket.gethostname()
    except (OSError, AttributeError):  # pragma: no cover - no hostname
        return None


def _local_flavour(wrapped: tx.Any) -> tx.Type[pathlib.PurePath]:
    """The `pathlib` flavour of a local path: Windows or POSIX.

    A `pathlib` path has its own; any other local driver (`anyio.Path`, a
    local `UPath`) follows the running platform, as `pathlib.Path` does.
    """
    if isinstance(wrapped, pathlib.PureWindowsPath):
        return pathlib.PureWindowsPath
    if isinstance(wrapped, pathlib.PurePosixPath):
        return pathlib.PurePosixPath
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return pathlib.PureWindowsPath
    return pathlib.PurePosixPath


def _local_identity(name: str) -> tx.Tuple[tx.Any, ...]:
    """The file a local name leads to, whether or not it exists yet.

    Symbolic links and `.`/`..` are resolved; the result is the device and
    inode of the file, or of its nearest existing ancestor followed by the
    names below it, so a hard link or another spelling of a directory leads
    to the same identity.
    """
    head = os.path.realpath(name)
    rest: tx.List[str] = []
    while True:
        try:
            info = os.stat(head)
        except OSError:
            parent, tail = os.path.split(head)
            if parent == head:  # pragma: no cover - the root always exists
                break
            rest.append(os.path.normcase(tail))
            head = parent
            continue
        return (info.st_dev, info.st_ino, tuple(reversed(rest)))
    return (head, tuple(reversed(rest)))  # pragma: no cover


def _is_path_shaped(obj: tx.Any) -> bool:
    """Whether `obj` looks enough like a path to wrap.

    Deliberately lenient: the flagship non-local `UPath` refuses
    `__fspath__`, so gating on that would reject the very drivers this
    package exists for. Any object that walks like a path -- `__fspath__`,
    `parts`, or `path` -- is accepted, and unsupported members degrade
    one at a time rather than at construction.
    """
    return (
        hasattr(obj, "__fspath__")
        or hasattr(obj, "parts")
        or hasattr(obj, "path")
    )
