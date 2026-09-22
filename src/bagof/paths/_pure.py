"""The public pure-path type.

`PurePath` is the lexical counterpart of `Path`. It shares the whole lexical
surface and the location and identity machinery, but adds none of the I/O
methods, so it is the pure level of the same path model. A local string is
wrapped as a stdlib pure path, a remote URL as the dependency-free lexical
cloud driver, and an existing driver object as its own lexical view.
"""

from __future__ import annotations

import pathlib

import typing_extensions as tx

from ._base import (
    BaseWrapper,
    _build_from_string,
    _local_from_url,
    _reject_local_storage_options,
)
from ._constants import LOCAL_PROTOCOLS, SCHEME_RE
from ._protocols import merged_storage_options
from ._pure_driver import PureCloudPath
from ._purepath import PurePathMixin


class PurePath(PurePathMixin, BaseWrapper):
    """A path for lexical operations only, local or in the cloud.

    A `PurePath` parses and manipulates a path without touching any filesystem.
    It provides the lexical surface -- `name`, `parent`, `suffix`, `joinpath`,
    `with_name`, and the rest -- together with the location properties
    `protocol`, `path`, `drive`, and `root`. It has no reading, writing,
    listing, or other I/O methods.

    A remote URL is understood with no backend installed, so a cloud location
    can be taken apart and rebuilt without `universal-pathlib` or
    `cloudpathlib`.

    ```pycon
    >>> from bagof.paths import PurePath
    >>> p = PurePath("/data/sets/train.zarr")
    >>> p.name
    'train.zarr'
    >>> p.parent
    PurePath('/data/sets')
    >>> p.suffix
    '.zarr'
    ```

    A `PurePath` and a [`Path`][bagof.paths.Path] for the same location compare
    equal and hash alike, so one serves to look the other up in a set or a
    dictionary. This mirrors `pathlib`, where a pure path equals the full path
    beside it.
    """

    __slots__ = ()

    def _from_string(
        self,
        text: str,
        driver: tx.Any,
        storage_options: tx.Optional[tx.Mapping[str, tx.Any]],
    ) -> tx.Any:
        """Build the wrapped object for a URL or path string.

        A local path becomes a stdlib pure path. A remote URL becomes the
        lexical cloud driver, which needs no backend. An explicit `driver`
        still builds a real driver object, whose own lexical view is wrapped.
        """
        if driver is not None:
            return _build_from_string(text, driver, storage_options)
        match = SCHEME_RE.match(text)
        scheme = match.group(1).lower() if match is not None else ""
        if match is None:
            if "::" in text and "://" in text:
                # An fsspec chain (simplecache::s3://...) with no leading
                # scheme: its identity scheme is empty, and it is manipulated
                # lexically with no backend.
                return PureCloudPath.from_url(text, "", storage_options)
            _reject_local_storage_options(storage_options, text)
            return pathlib.PurePath(text)
        if scheme in LOCAL_PROTOCOLS:
            _reject_local_storage_options(storage_options, text)
            return pathlib.PurePath(str(_local_from_url(text, scheme)))
        text = scheme + text[match.end(1):]
        options = merged_storage_options(scheme, storage_options)
        return PureCloudPath.from_url(text, scheme, options)
