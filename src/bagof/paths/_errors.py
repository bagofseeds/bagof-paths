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
        # A driver that carries its own hint is the internal backendless
        # driver; naming it would leak a private class, so name only the
        # operation and let the hint carry the meaning.
        internal = getattr(driver, "_unsupported_hint", None) is not None
        if driver is not None and not internal:
            name = getattr(driver, "__name__", None) or type(driver).__name__
            message += f" for {name}"
        if hint:
            message += f": {hint}"
        super().__init__(message)


class NoLocalPathError(UnsupportedPathOperation, TypeError):
    """A path has no local file name.

    Raised by `os.fspath` (and anything that calls it, such as `open`) on a
    remote path: an `s3://` key is not a file on this machine. It is both an
    `UnsupportedPathOperation` and a `TypeError`, so the usual probe for "is
    this a local path?" still works:

    ```python
    try:
        name = os.fspath(path)
    except TypeError:
        ...  # not a local file; use str(path), path.open(), or local_copy()
    ```

    `path.is_local()` answers the same question without raising.
    """


class NoDriverError(ValueError):
    """No installed driver can interpret a URL's scheme.

    Raised at construction, when `Path("scheme://...")` names a scheme that
    neither an explicit `driver=` nor any installed backend can build. It is
    a `ValueError` -- the constructor's historical error type for a string it
    cannot interpret -- so existing `except ValueError` handlers still catch
    it, and it says nothing about the scheme being unsupportable in principle:
    installing a backend (or registering one) can make the same string work.

    The scheme is available on the `scheme` attribute.
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


class CommitError(OSError):
    """A staged version could not be moved onto its target.

    Raised by `commit` on the staging area that
    [`Path.staged`][bagof.paths.Path.staged] returns. The target is left as
    it was (or restored), and the staged data is kept, so the work is not
    lost: the `staged` attribute is its location, `target` the path it was
    meant for, and the original error is chained as `__cause__`. An
    `errno` from that error is carried over. It is an `OSError`, so an
    `except OSError` around a write still catches it.
    """

    def __init__(
        self, target: tx.Any, staged: tx.Any, error: BaseException
    ) -> None:
        self.target = target
        self.staged = staged
        message = (
            f"could not commit the staged version of {str(target)!r} "
            f"({error}); the staged data is kept at {str(staged)!r}"
        )
        code = getattr(error, "errno", None)
        if isinstance(code, int):
            super().__init__(code, message)
        else:
            super().__init__(message)
        self._error = error

    def __reduce__(self) -> tx.Any:
        # OSError pickles its args, which do not match this constructor; a
        # worker process must still be able to send the error back.
        return (type(self), (self.target, self.staged, self._error))
