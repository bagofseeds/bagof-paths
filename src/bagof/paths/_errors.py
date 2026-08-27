"""The exception raised when an operation can be neither delegated nor
synthesized."""

from __future__ import annotations

import pathlib

import typing_extensions as tx

# pathlib.UnsupportedOperation exists from Python 3.13 and is itself a
# NotImplementedError subclass -- it is the stdlib's own answer for exactly
# this situation, so inherit it where available and fall back to
# NotImplementedError otherwise. Either way, ``except NotImplementedError``
# catches ours.
_Base = getattr(pathlib, "UnsupportedOperation", NotImplementedError)


class UnsupportedPathOperation(_Base):
    """A path operation is not supported by the wrapped object.

    Raised when the wrapped object does not implement an operation and it
    cannot be synthesized from what the object does provide. The message
    names the operation and, where known, the wrapped driver.
    """

    def __init__(
        self,
        operation: str,
        *,
        driver: tx.Any = None,
        hint: tx.Optional[str] = None,
    ) -> None:
        self.operation = operation
        self.driver = driver
        message = f"{operation!r} is not supported"
        if driver is not None:
            name = getattr(driver, "__name__", None) or type(driver).__name__
            message += f" for {name}"
        if hint:
            message += f": {hint}"
        super().__init__(message)


class NoDriverError(ValueError):
    """No installed driver can interpret a URL's scheme.

    Raised at construction, when ``Path("scheme://...")`` names a scheme that
    neither an explicit ``driver=`` nor any installed backend can build. It is
    a ``ValueError`` -- the constructor's historical error type for a string it
    cannot interpret -- so existing ``except ValueError`` handlers still catch
    it, and it says nothing about the scheme being unsupportable in principle:
    installing a backend (or registering one) can make the same string work.

    The scheme is available on the ``scheme`` attribute.
    """

    def __init__(self, scheme: str, *, hint: tx.Optional[str] = None) -> None:
        self.scheme = scheme
        if scheme:
            message = (
                f"no installed driver can interpret the scheme {scheme!r}"
            )
        else:
            # A chained URL (simplecache::s3://...) has no single scheme.
            message = "no installed driver can interpret this URL"
        if hint:
            message += f": {hint}"
        super().__init__(message)
