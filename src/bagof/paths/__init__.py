"""A uniform pathlib-style API over pathlib, UPath, and cloud paths.

The public surface is re-exported here; every implementation module is
private. See ``docs/design/path-wrapper.md`` for the design.
"""

from ._async_path import AsyncPath
from ._drivers import register_driver
from ._errors import NoDriverError, UnsupportedPathOperation
from ._path import Path
from ._protocols import ProtocolTraits, register_protocol

__all__ = [
    "AsyncPath",
    "NoDriverError",
    "Path",
    "ProtocolTraits",
    "UnsupportedPathOperation",
    "register_driver",
    "register_protocol",
]
