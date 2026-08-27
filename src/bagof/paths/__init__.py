"""A uniform pathlib-style API over pathlib, UPath, and cloud paths.

The public surface is re-exported here; every implementation module is
private. See ``docs/design/path-wrapper.md`` for the design.
"""

from ._errors import UnsupportedPathOperation
from ._path import Path

__all__ = [
    "Path",
    "UnsupportedPathOperation",
]
