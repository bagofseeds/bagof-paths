"""One path API for local files and the cloud.

Works like [pathlib.Path][] over a local path, a `UPath`, a cloud path, or
any path object. The public names are re-exported here; every other module is
private.
"""

from ._async_path import AsyncPath
from ._drivers import register_driver
from ._errors import (
    CommitError,
    NoDriverError,
    NoLocalPathError,
    UnsupportedPathOperation,
)
from ._path import Path
from ._protocols import (
    ProtocolTraits,
    register_protocol,
    set_storage_options,
)
from ._pure import PurePath
from ._staged import AsyncStage, Stage

__all__ = [
    "AsyncPath",
    "AsyncStage",
    "CommitError",
    "NoDriverError",
    "NoLocalPathError",
    "Path",
    "ProtocolTraits",
    "PurePath",
    "Stage",
    "UnsupportedPathOperation",
    "register_driver",
    "register_protocol",
    "set_storage_options",
]
