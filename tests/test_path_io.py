"""The concrete I/O surface over stdlib pathlib, on a real temp tree."""

import pathlib

import pytest

from bagof.paths import Path


def test_mkdir_touch_and_status(tmp_path: pathlib.Path) -> None:
    d = Path(tmp_path) / "sub"
    assert d.exists() is False
    assert d.mkdir() is None  # returns None, not self
    assert d.exists() is True
    assert d.is_dir() is True
    f = d / "f.txt"
    f.touch()
    assert f.is_file() is True
    assert f.is_dir() is False
    assert f.is_symlink() is False


def test_read_write_bytes_and_text(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a.bin"
    assert f.write_bytes(b"hello") == 5
    assert f.read_bytes() == b"hello"
    g = Path(tmp_path) / "a.txt"
    g.write_text("héllo")
    assert g.read_text() == "héllo"


def test_open(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a.txt"
    with f.open("w") as handle:
        handle.write("x")
    with f.open() as handle:
        assert handle.read() == "x"


def test_iterdir_glob_rglob_return_wrapped(tmp_path: pathlib.Path) -> None:
    root = Path(tmp_path)
    (root / "a.txt").touch()
    (root / "b.log").touch()
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.txt").touch()

    entries = list(root.iterdir())
    assert all(isinstance(e, Path) for e in entries)
    assert (root / "a.txt") in entries

    assert sorted(str(p) for p in root.glob("*.txt")) == [str(root / "a.txt")]
    assert str(sub / "c.txt") in [str(p) for p in root.rglob("*.txt")]
    assert all(isinstance(p, Path) for p in root.rglob("*"))


def test_unlink(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a"
    f.touch()
    f.unlink()
    assert f.exists() is False
    with pytest.raises(FileNotFoundError):
        f.unlink()
    f.unlink(missing_ok=True)  # no raise


def test_resolve_and_absolute(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a"
    assert isinstance(f.resolve(), Path)
    assert f.absolute().is_absolute() is True


def test_rename_and_replace(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a"
    f.write_text("x")
    moved = f.rename(tmp_path / "b")
    assert isinstance(moved, Path)
    assert moved.read_text() == "x"
    assert f.exists() is False

    other = Path(tmp_path) / "c"
    other.write_text("y")
    other.replace(tmp_path / "b")
    assert (Path(tmp_path) / "b").read_text() == "y"


def test_stat_and_samefile(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a"
    f.touch()
    assert f.stat().st_size == 0
    assert f.samefile(tmp_path / "a") is True
    assert f.samefile(Path(tmp_path) / "a") is True


def test_io_preserves_subclass_type(tmp_path: pathlib.Path) -> None:
    class Sub(Path):
        pass

    root = Sub(tmp_path)
    (root / "f.txt").touch()
    assert all(isinstance(e, Sub) for e in root.iterdir())
    assert isinstance(root.resolve(), Sub)
