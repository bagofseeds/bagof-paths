"""Derivation carries subclass state onto every derived path.

This is the extensibility guarantee that the reference implementation broke:
a subclass adds a flag, and every path derived from it (parent, joinpath, /,
...) must keep both the subclass type and the flag.
"""

from bagof.paths import Path


class ReadOnlyPath(Path):
    """A downstream-style subclass: one class, one added attribute."""

    def __init__(
        self, path: object, *, read_only: bool = False, **kwargs
    ) -> None:
        super().__init__(path, **kwargs)
        self.read_only = read_only


def test_derived_paths_keep_subclass_type() -> None:
    p = ReadOnlyPath("/store/a/b")
    assert type(p.parent) is ReadOnlyPath
    assert type(p / "c") is ReadOnlyPath
    assert type(p.joinpath("c")) is ReadOnlyPath
    assert type(p.with_suffix(".zarr")) is ReadOnlyPath
    assert all(type(q) is ReadOnlyPath for q in p.parents)


def test_derived_paths_keep_subclass_state() -> None:
    p = ReadOnlyPath("/store/a/b", read_only=True)
    assert p.read_only is True
    assert p.parent.read_only is True
    assert (p / "c").read_only is True
    assert p.joinpath("c").read_only is True
    assert p.parents[0].read_only is True


def test_with_wrapped_directly() -> None:
    from pathlib import Path as LocalPath

    p = ReadOnlyPath("/store/a", read_only=True)
    q = p.with_wrapped(LocalPath("/store/x"))
    assert type(q) is ReadOnlyPath
    assert q.read_only is True
    assert str(q) == "/store/x"
