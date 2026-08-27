"""The synchronous path wrapper."""

from __future__ import annotations

import typing_extensions as tx

from . import _engine as engine
from . import _match
from ._base import BaseWrapper
from ._spec import BY_NAME


class Path(BaseWrapper):
    """A path-like object exposed through one uniform, pathlib-style surface.

    Construct one around a path string or any path-like object::

        >>> from bagof.paths import Path
        >>> p = Path("/data/sets/train.zarr")
        >>> p.name
        'train.zarr'
        >>> p.parent
        Path('/data/sets')
        >>> (p / "chunks").suffix
        ''

    Each member either delegates to the wrapped object, falls back to a
    synthesized implementation, or raises
    :class:`~bagof.paths.UnsupportedPathOperation`.
    """

    __slots__ = ()

    # -- pure-path properties ----------------------------------------------
    @property
    def name(self) -> str:
        """The final path component."""
        return engine.get(self, BY_NAME["name"])

    @property
    def stem(self) -> str:
        """The final component without its suffix."""
        return engine.get(self, BY_NAME["stem"])

    @property
    def suffix(self) -> str:
        """The final component's last suffix, including the leading dot."""
        return engine.get(self, BY_NAME["suffix"])

    @property
    def suffixes(self) -> tx.List[str]:
        """The final component's suffixes, each including its dot."""
        return engine.get(self, BY_NAME["suffixes"])

    @property
    def parts(self) -> tx.Tuple[str, ...]:
        """The path's components."""
        return engine.get(self, BY_NAME["parts"])

    @property
    def parent(self) -> tx.Self:
        """The logical parent of the path."""
        return engine.get(self, BY_NAME["parent"])

    @property
    def parents(self) -> tx.Tuple[tx.Self, ...]:
        """The logical ancestors of the path, closest first."""
        return engine.get(self, BY_NAME["parents"])

    # -- pure-path methods -------------------------------------------------
    def joinpath(self, *segments: tx.Any) -> tx.Self:
        """Combine the path with each of the given segments."""
        return engine.invoke(self, BY_NAME["joinpath"], segments)

    def with_name(self, name: str) -> tx.Self:
        """A new path with the final component changed to ``name``."""
        return engine.invoke(self, BY_NAME["with_name"], (name,))

    def with_stem(self, stem: str) -> tx.Self:
        """A new path with the stem changed to ``stem``."""
        return engine.invoke(self, BY_NAME["with_stem"], (stem,))

    def with_suffix(self, suffix: str) -> tx.Self:
        """A new path with the suffix changed to ``suffix``."""
        return engine.invoke(self, BY_NAME["with_suffix"], (suffix,))

    def as_posix(self) -> str:
        """The path as a string with forward slashes."""
        return engine.invoke(self, BY_NAME["as_posix"])

    def as_uri(self) -> str:
        """The path as a URI. Requires an absolute path."""
        return engine.invoke(self, BY_NAME["as_uri"])

    def is_absolute(self) -> bool:
        """Whether the path is absolute."""
        return engine.invoke(self, BY_NAME["is_absolute"])

    def is_relative_to(self, other: tx.Any) -> bool:
        """Whether the path is relative to ``other``."""
        return engine.invoke(self, BY_NAME["is_relative_to"], (other,))

    def relative_to(self, other: tx.Any, *, walk_up: bool = False) -> tx.Self:
        """The path made relative to ``other``."""
        return engine.invoke(
            self, BY_NAME["relative_to"], (other,), {"walk_up": walk_up}
        )

    def match(
        self, pattern: str, *, case_sensitive: tx.Optional[bool] = None
    ) -> bool:
        """Whether the path matches ``pattern``, anchored from the right.

        Matching is lexical and consistent across drivers: it runs on the
        canonical path, not on the wrapped object.
        """
        return _match.match(self.path, pattern, case_sensitive=case_sensitive)

    def full_match(
        self, pattern: str, *, case_sensitive: tx.Optional[bool] = None
    ) -> bool:
        """Whether the whole path matches ``pattern`` (``**`` spans segments).

        Uses CPython 3.13's glob semantics on every interpreter.
        """
        return _match.full_match(
            self.path, pattern, case_sensitive=case_sensitive
        )
