"""Driver adapters: rmdir safety, copy/move, and walk.

The generic path is tested on stdlib pathlib; the divergent, data-loss-prone
behaviors are tested against the real universal-pathlib and cloudpathlib
backends where the interpreter has them.
"""

import os
import pathlib
import shutil

import pytest

from bagof.paths import Path, UnsupportedPathOperation, register_driver


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


# -- the generic rmdir guard for unregistered recursive-by-default drivers --
class _RecursiveRmdir(os.PathLike):
    """An unregistered driver whose rmdir recurses by default (UPath-like)."""

    def __init__(self, real: pathlib.Path) -> None:
        self._real = pathlib.Path(real)

    def __fspath__(self) -> str:
        return os.fspath(self._real)

    def __str__(self) -> str:
        return str(self._real)

    def rmdir(self, recursive: bool = True) -> None:
        if recursive:
            shutil.rmtree(self._real)
        else:
            self._real.rmdir()  # raises on a non-empty dir


def test_generic_rmdir_guards_a_recursive_by_default_driver(
    tmp_path: pathlib.Path,
) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "f").write_text("keep")
    p = Path(_RecursiveRmdir(d))
    with pytest.raises(OSError):
        p.rmdir()  # generic adapter must pass recursive=False, not delete
    assert (d / "f").read_text() == "keep"


def test_move_file_onto_existing_directory_raises(
    tmp_path: pathlib.Path,
) -> None:
    f = Path(tmp_path) / "a.txt"
    f.write_text("x")
    (tmp_path / "d").mkdir()
    with pytest.raises(OSError):
        f.move(tmp_path / "d")
    assert f.exists()  # source untouched, not silently moved into the dir


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_walk_does_not_follow_symlink_cycles(
    tmp_path: pathlib.Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.txt").touch()
    (real / "loop").symlink_to(real, target_is_directory=True)
    entries = list(Path(real).walk(follow_symlinks=False))
    assert len(entries) == 1  # terminates; the cycle is not descended
    _, dirnames, filenames = entries[0]
    assert dirnames == []
    assert sorted(filenames) == ["f.txt", "loop"]  # symlinked dir as a file


def test_walk_pruning_via_dirnames(tmp_path: pathlib.Path) -> None:
    root = Path(tmp_path)
    (root / "keep").mkdir()
    (root / "keep" / "a.txt").touch()
    (root / "skip").mkdir()
    (root / "skip" / "b.txt").touch()
    seen = []
    for _dirpath, dirnames, filenames in root.walk():
        seen.extend(filenames)
        if "skip" in dirnames:
            dirnames.remove("skip")  # prune before descent
    assert "a.txt" in seen
    assert "b.txt" not in seen  # pruned branch was never entered


# -- the public extension point --------------------------------------------
def test_late_register_driver_wins(tmp_path: pathlib.Path) -> None:
    from bagof.paths import _drivers

    class MarkerAdapter(_drivers.GenericAdapter):
        pass

    marker = MarkerAdapter()
    register_driver(pathlib.PurePath, marker)
    try:
        # A later registration for a class the built-ins also match must win.
        assert _drivers.adapter_for(pathlib.Path(tmp_path)) is marker
    finally:
        _drivers._REGISTRY[:] = [
            e for e in _drivers._REGISTRY if e[1] is not marker
        ]
        _drivers._CACHE.clear()


# -- capability introspection ----------------------------------------------
def test_supports_and_capabilities(tmp_path: pathlib.Path) -> None:
    p = Path(tmp_path)
    assert p.supports("rmdir") is True
    assert p.supports("copy") is True  # local
    assert p.supports("walk") is True
    caps = p.capabilities()
    assert "rmdir" in caps and "copy" in caps and "match" in caps
    assert "exists" in caps


def test_coerce_target_rejects_foreign_scheme(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a.txt"
    f.write_text("x")
    with pytest.raises(ValueError):
        f.copy("s3://bucket/up.txt")  # a local path can't target a URL string


# -- real backends ----------------------------------------------------------
def _safe_rmdir_invariant(base: object, expected: object) -> None:
    """A bare rmdir() must never delete a non-empty tree; recursive removes."""
    p = Path(base)
    try:
        p.rmdir()  # non-recursive; on UPath a bare rmdir() would recurse
    except expected:
        pass  # refusing a non-empty dir is the safe outcome
    assert (base / "f.txt").read_text() == "keep"  # data intact
    p.rmdir(recursive=True)
    assert not base.exists()


def test_upath_rmdir_does_not_destroy_a_nonempty_tree() -> None:
    UPath = pytest.importorskip("upath").UPath
    base = UPath("memory://bagofpaths-test/dir")
    base.mkdir(parents=True, exist_ok=True)
    (base / "f.txt").write_text("keep")
    _safe_rmdir_invariant(base, OSError)


def test_cloudpathlib_rmdir_does_not_destroy_a_nonempty_tree() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    from cloudpathlib.exceptions import DirectoryNotEmptyError

    client = local.LocalS3Client()
    base = local.LocalS3Path("s3://bagofpaths-test/dir", client=client)
    (base / "f.txt").write_text("keep")
    _safe_rmdir_invariant(base, (OSError, DirectoryNotEmptyError))


def test_cloudpathlib_unlink_honors_missing_ok_false() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    raw = local.LocalS3Path("s3://bagofpaths-test/none.txt", client=client)
    missing = Path(raw)
    assert missing.exists() is False
    with pytest.raises(FileNotFoundError):
        missing.unlink()  # cloudpathlib default is missing_ok=True; ours False
    missing.unlink(missing_ok=True)  # no raise


def test_upath_coerce_target_preserves_storage_options() -> None:
    UPath = pytest.importorskip("upath").UPath
    src = UPath("memory://bagofpaths-test/opts/a.txt")
    p = Path(src)
    target = p._coerce_target("memory://bagofpaths-test/opts/b.txt")
    assert getattr(target, "storage_options", None) == src.storage_options


def test_local_to_cloud_copy_does_not_silently_cache(
    tmp_path: pathlib.Path,
) -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    src = Path(tmp_path) / "a.txt"
    src.write_text("x")
    target = local.LocalS3Path("s3://bagofpaths-test/dst.txt", client=client)
    with pytest.raises(UnsupportedPathOperation):
        src.copy(target)  # must not os.fspath a cloud target and "succeed"
