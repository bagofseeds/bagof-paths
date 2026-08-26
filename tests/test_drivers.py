"""Driver adapters: rmdir safety, copy/move, and walk.

The generic path is tested on stdlib pathlib; the divergent, data-loss-prone
behaviors are tested against the real universal-pathlib and cloudpathlib
backends where the interpreter has them.
"""

import pathlib

import pytest

from bagof.paths import Path


# -- generic (stdlib pathlib) ----------------------------------------------
def test_rmdir_removes_empty(tmp_path: pathlib.Path) -> None:
    d = Path(tmp_path) / "e"
    d.mkdir()
    d.rmdir()
    assert not (tmp_path / "e").exists()


def test_rmdir_nonrecursive_keeps_a_nonempty_tree(
    tmp_path: pathlib.Path,
) -> None:
    d = Path(tmp_path) / "d"
    d.mkdir()
    (d / "f").write_text("keep")
    with pytest.raises(OSError):
        d.rmdir()
    assert (tmp_path / "d" / "f").read_text() == "keep"  # data intact


def test_rmdir_recursive_removes_tree(tmp_path: pathlib.Path) -> None:
    d = Path(tmp_path) / "d"
    d.mkdir()
    (d / "f").touch()
    (d / "sub").mkdir()
    d.rmdir(recursive=True)
    assert not (tmp_path / "d").exists()


def test_copy_file_and_directory(tmp_path: pathlib.Path) -> None:
    src = Path(tmp_path) / "a.txt"
    src.write_text("x")
    dst = src.copy(tmp_path / "b.txt")
    assert isinstance(dst, Path)
    assert dst.read_text() == "x"

    srcd = Path(tmp_path) / "d"
    srcd.mkdir()
    (srcd / "f").write_text("y")
    srcd.copy(tmp_path / "d2")
    assert (tmp_path / "d2" / "f").read_text() == "y"


def test_copy_into(tmp_path: pathlib.Path) -> None:
    src = Path(tmp_path) / "a.txt"
    src.write_text("x")
    (Path(tmp_path) / "dest").mkdir()
    src.copy_into(tmp_path / "dest")
    assert (tmp_path / "dest" / "a.txt").read_text() == "x"


def test_move_and_move_into(tmp_path: pathlib.Path) -> None:
    src = Path(tmp_path) / "a.txt"
    src.write_text("x")
    dst = src.move(tmp_path / "b.txt")
    assert dst.read_text() == "x"
    assert not (tmp_path / "a.txt").exists()

    src2 = Path(tmp_path) / "c.txt"
    src2.write_text("z")
    (Path(tmp_path) / "dest").mkdir()
    src2.move_into(tmp_path / "dest")
    assert (tmp_path / "dest" / "c.txt").read_text() == "z"
    assert not (tmp_path / "c.txt").exists()


def test_walk_yields_wrapped_tuples(tmp_path: pathlib.Path) -> None:
    root = Path(tmp_path)
    (root / "a.txt").touch()
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").touch()

    seen = {}
    for dirpath, dirnames, filenames in root.walk():
        assert isinstance(dirpath, Path)
        seen[str(dirpath)] = (sorted(dirnames), sorted(filenames))
    assert seen[str(root)] == (["sub"], ["a.txt"])
    assert seen[str(sub)] == ([], ["b.txt"])


def test_copy_preserves_subclass_type(tmp_path: pathlib.Path) -> None:
    class Sub(Path):
        pass

    src = Sub(tmp_path) / "a.txt"
    src.write_text("x")
    assert isinstance(src.copy(tmp_path / "b.txt"), Sub)


# -- real backends: the data-loss guard ------------------------------------
def _safe_rmdir_invariant(base: object) -> None:
    """A bare rmdir() must never delete a non-empty tree; recursive removes."""
    p = Path(base)
    try:
        p.rmdir()  # non-recursive; on UPath a bare rmdir() would recurse
    except Exception:  # noqa: BLE001 - refusing a non-empty dir is the point
        pass
    assert (base / "f.txt").read_text() == "keep"  # data intact either way
    p.rmdir(recursive=True)
    assert not base.exists()


def test_upath_rmdir_does_not_destroy_a_nonempty_tree() -> None:
    UPath = pytest.importorskip("upath").UPath
    base = UPath("memory://bagofpaths-test/dir")
    base.mkdir(parents=True, exist_ok=True)
    (base / "f.txt").write_text("keep")
    _safe_rmdir_invariant(base)


def test_cloudpathlib_rmdir_does_not_destroy_a_nonempty_tree() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    base = local.LocalS3Path("s3://bagofpaths-test/dir", client=client)
    (base / "f.txt").write_text("keep")
    _safe_rmdir_invariant(base)
