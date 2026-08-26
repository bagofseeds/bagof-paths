"""The synchronous path wrapper."""

from __future__ import annotations

import typing_extensions as tx

from . import _engine as engine
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

    # -- capability introspection ------------------------------------------
    def supports(self, name: str) -> bool:
        """Whether ``name`` is wired for this path.

        Answers "is this operation available" -- by delegation or by a
        synthesized fallback -- not "will a call succeed on this particular
        path". It is a static property of the wrapped object, not a
        filesystem probe.
        """
        member = BY_NAME.get(name)
        if member is None:
            return hasattr(self, name)
        if hasattr(self._wrapped, member.name):
            return True
        if member.fallback and all(self.supports(p) for p in member.needs):
            return True
        return False

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
        """Whether the path matches the given glob-style ``pattern``."""
        return engine.invoke(
            self, BY_NAME["match"], (pattern,),
            {"case_sensitive": case_sensitive},
        )

    def full_match(
        self, pattern: str, *, case_sensitive: tx.Optional[bool] = None
    ) -> bool:
        """Whether the whole path matches the given ``pattern``."""
        return engine.invoke(
            self, BY_NAME["full_match"], (pattern,),
            {"case_sensitive": case_sensitive},
        )
