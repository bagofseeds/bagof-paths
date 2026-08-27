"""Driver selection: build a backend path object from a URL string.

Given a scheme that is not the local filesystem (or an fsspec chain like
``simplecache::s3://...``), pick a backend and construct a path. The order is:
a protocol's preferred driver, then the availability order universal-pathlib
then cloudpathlib. A scheme no installed backend can build raises
:class:`~bagof.paths.NoDriverError` -- never a silent local path.

universal-pathlib is the default: it builds any fsspec URL lazily (no cloud
SDK is needed to *construct* a path, only to do I/O) and covers the widest set
of schemes. cloudpathlib is the fallback for when universal-pathlib is absent;
its concrete implementation class is selected from its own registry -- never
``AnyPath``, whose answer for an unrecognised scheme is a silent local path.

Only universal-pathlib's *unknown-scheme* rejection becomes ``NoDriverError``.
A driver that recognises the scheme but rejects the URL for another reason (a
missing storage option, a bad prefix), and any error from an explicitly
preferred driver, is a real error and propagates unchanged.
"""

from __future__ import annotations

import typing_extensions as tx

from ._errors import NoDriverError
from ._protocols import canonical_scheme

_HINT = (
    "install universal-pathlib (or cloudpathlib) to build this scheme, pass "
    "driver= a path class, or register_protocol(scheme, driver=...)"
)

# our canonical scheme -> cloudpathlib's registry key, where they differ.
_CLOUD_KEY = {"az": "azure"}


def build(text: str, scheme: str) -> tx.Any:
    """Construct a driver path for a remote ``scheme`` (or fsspec chain)."""
    from ._protocols import traits_for

    preferred = traits_for(scheme).driver if scheme else None
    if preferred is not None:
        # An explicit registration: its own errors are meaningful, not masked.
        return preferred(text)
    upath = _upath_class()
    if upath is not None:
        try:
            return upath(text)
        except ValueError as error:
            if _is_unknown_scheme(error):
                raise NoDriverError(scheme, hint=_HINT) from error
            raise  # a recognised scheme rejected for a real reason
    cloud = _cloudpathlib_class(scheme)
    if cloud is not None:
        return cloud(text)  # cloudpathlib's own errors are meaningful
    raise NoDriverError(scheme, hint=_HINT)


def _is_unknown_scheme(error: ValueError) -> bool:
    # universal-pathlib rejects an unknown scheme with this exact phrasing.
    return "Unsupported filesystem" in str(error)


def _upath_class() -> tx.Optional[tx.Callable[[str], tx.Any]]:
    try:
        from upath import UPath
    except ImportError:  # pragma: no cover
        return None
    return UPath


def _cloud_key(scheme: str) -> str:
    """cloudpathlib's registry key for a scheme (following our aliases)."""
    canonical = canonical_scheme(scheme)
    return _CLOUD_KEY.get(canonical, canonical)


def _cloudpathlib_class(
    scheme: str,
) -> tx.Optional[tx.Callable[[str], tx.Any]]:
    if not scheme:
        return None
    try:
        from cloudpathlib.cloudpath import implementation_registry
    except ImportError:  # pragma: no cover
        return None
    impl = implementation_registry.get(_cloud_key(scheme))
    if impl is None:
        return None
    # Accessing path_class raises MissingDependenciesError when the cloud SDK
    # is absent; that is the intended fail-fast (the message names the pip
    # extra to install), so it is not caught here.
    path_class = impl.path_class
    prefix = path_class.cloud_prefix

    def factory(text: str) -> tx.Any:
        # cloudpathlib validates the literal prefix, so rewrite an aliased or
        # mixed-case scheme (s3a://, ABFS://) to the implementation's own.
        rest = text.split("://", 1)[1] if "://" in text else text
        return path_class(prefix + rest)

    return factory
