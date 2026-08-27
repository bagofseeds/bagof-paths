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


def build(
    text: str,
    scheme: str,
    storage_options: tx.Optional[tx.Mapping[str, tx.Any]] = None,
) -> tx.Any:
    """Construct a driver path for a remote ``scheme`` (or fsspec chain).

    ``storage_options`` (endpoint, credentials, ...) are forwarded to the
    chosen driver. A per-scheme default registered with the protocol is the
    base; the per-call mapping overrides it key by key.
    """
    from ._protocols import merged_storage_options, traits_for

    options = merged_storage_options(scheme, storage_options)
    preferred = traits_for(scheme).driver if scheme else None
    if preferred is not None:
        # An explicit registration: its own errors are meaningful, not masked.
        return _call_factory(preferred, text, options)
    upath = _upath_class()
    if upath is not None:
        try:
            return _call_factory(upath, text, options)
        except ValueError as error:
            if _is_unknown_scheme(error):
                raise NoDriverError(scheme, hint=_HINT) from error
            raise  # a recognised scheme rejected for a real reason
    if _cloudpathlib_impl(scheme) is not None:
        if options:
            # cloudpathlib takes a Client object, not an fsspec options dict;
            # there is no safe automatic translation between the two. Refuse
            # before building, so the message names the real limitation
            # rather than a missing cloud SDK.
            raise TypeError(
                "storage_options= is not supported for the cloudpathlib "
                "backend; pass a pre-built Client via driver=, or install "
                "universal-pathlib"
            )
        return _cloudpathlib_class(scheme)(text)  # its own errors are kept
    raise NoDriverError(scheme, hint=_HINT)


def _call_factory(
    factory: tx.Callable[..., tx.Any],
    text: str,
    options: tx.Mapping[str, tx.Any],
) -> tx.Any:
    """Build a path, forwarding storage options only when there are any.

    A driver whose signature is the historical ``str -> path`` still works:
    with no options it is called with the URL alone.
    """
    return factory(text, **options) if options else factory(text)


def _is_unknown_scheme(error: ValueError) -> bool:
    # universal-pathlib rejects an unknown scheme with this exact phrasing.
    return "Unsupported filesystem" in str(error)


def _upath_class() -> tx.Optional[tx.Callable[..., tx.Any]]:
    try:
        from upath import UPath
    except ImportError:  # pragma: no cover
        return None
    return UPath


def _cloud_key(scheme: str) -> str:
    """cloudpathlib's registry key for a scheme (following our aliases)."""
    canonical = canonical_scheme(scheme)
    return _CLOUD_KEY.get(canonical, canonical)


def _cloudpathlib_impl(scheme: str) -> tx.Any:
    """cloudpathlib's implementation entry for a scheme, or ``None``.

    Reads the registry only -- it does not touch ``path_class``, so it does
    not trigger the missing-SDK fail-fast. That lets a caller decide whether
    the scheme is cloudpathlib's to handle before forcing the SDK to load.
    """
    if not scheme:
        return None
    try:
        from cloudpathlib.cloudpath import implementation_registry
    except ImportError:  # pragma: no cover
        return None
    return implementation_registry.get(_cloud_key(scheme))


def _cloudpathlib_class(
    scheme: str,
) -> tx.Optional[tx.Callable[[str], tx.Any]]:
    impl = _cloudpathlib_impl(scheme)
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
