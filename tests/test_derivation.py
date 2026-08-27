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


class SlottedPath(Path):
    """A subclass that keeps its state in slots, not __dict__."""

    __slots__ = ("flag",)

    def __init__(self, path: object, *, flag: object = None, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self.flag = flag


def test_slotted_subclass_state_carried() -> None:
    p = SlottedPath("/store/a/b", flag="x")
    assert p.parent.flag == "x"
    assert (p / "c").flag == "x"
    assert p.parents[0].flag == "x"


def test_reassignment_on_derived_path_is_independent() -> None:
    p = ReadOnlyPath("/store/a/b", read_only=True)
    child = p.parent
    child.read_only = False
    assert p.read_only is True  # copy is independent for reassignment


class TaggedPath(Path):
    def __init__(self, path: object, *, tags: object = None, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self.tags = [] if tags is None else tags


def test_mutable_state_is_shared_shallow() -> None:
    # Documented behavior: with_wrapped is a shallow copy, so a mutable
    # attribute is shared across derived paths (like dataclasses.replace).
    p = TaggedPath("/store/a/b", tags=["t"])
    p.parent.tags.append("u")
    assert p.tags == ["t", "u"]


def test_plain_path_rejects_stray_attributes() -> None:
    import pytest

    with pytest.raises(AttributeError):
        Path("/a").sneaky = 1  # slots hold: no __dict__ on a plain Path
