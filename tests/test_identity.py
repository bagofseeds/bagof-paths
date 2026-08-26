"""Identity: equality, hashing, and driver-independence."""

from bagof.paths import Path


def test_equality_and_hash() -> None:
    assert Path("/a/b") == Path("/a/b")
    assert Path("/a/b") != Path("/a/c")
    assert hash(Path("/a/b")) == hash(Path("/a/b"))


def test_usable_as_dict_key_and_set_member() -> None:
    table = {Path("/a/b"): 1}
    assert table[Path("/a/b")] == 1
    assert Path("/a/b") in {Path("/a/b"), Path("/a/c")}


def test_equality_is_driver_independent_across_subclasses() -> None:
    # Two wrappers pointing at the same location are equal regardless of the
    # wrapper subclass -- mirroring pathlib, where PurePosixPath == PosixPath.
    class Other(Path):
        pass

    assert Path("/a/b") == Other("/a/b")
    assert hash(Path("/a/b")) == hash(Other("/a/b"))


def test_not_equal_to_foreign_types() -> None:
    assert (Path("/a/b") == "/a/b") is False
    assert Path("/a/b") != object()
