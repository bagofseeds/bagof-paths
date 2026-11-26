"""`pathinfo`: what a path is, from its listing or asked once (#64).

A listed child (iterdir, glob/rglob, walk) answers from what the listing
said, with no further call to the store; any other path asks the store the
first time and keeps the answer. The answers are point-in-time, never ride
onto a derived path, and are the same object on every driver. The stdlib
tests run on every interpreter, the 3.8 core-only leg included; the backend
tests need universal-pathlib or cloudpathlib.
"""

import asyncio
import os
import pathlib
import pickle
import sys

import pytest

from bagof.paths import (
    AsyncPath,
    AsyncPathInfo,
    Path,
    PathInfo,
    PurePath,
)

_ANSWERS = ("is_dir", "is_file", "is_symlink", "exists")


def _answers(info: PathInfo) -> dict:
    return {
        "dir": info.is_dir(), "file": info.is_file(),
        "link": info.is_symlink(), "exists": info.exists(),
        "size": info.size(),
    }


def _tree(root: pathlib.Path) -> None:
    (root / "sub").mkdir()
    (root / "sub" / "inner.txt").write_bytes(b"in")
    (root / "scan.nii").write_bytes(b"0123")


def _count_os_stats(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every os.stat / os.lstat made from Python code."""
    calls: list = []
    for name in ("stat", "lstat"):
        original = getattr(os, name)

        def counted(
            *args: object, _name: str = name, _original: object = original,
            **kwargs: object,
        ) -> object:
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(os, name, counted)
    return calls


# -- the public surface -------------------------------------------------------
def test_pathinfo_is_kept_on_the_path(tmp_path: pathlib.Path) -> None:
    p = Path(tmp_path)
    assert isinstance(p.pathinfo, PathInfo)
    assert p.pathinfo is p.pathinfo
    assert isinstance(AsyncPath(tmp_path).pathinfo, AsyncPathInfo)


def test_supports_pathinfo_without_reading_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bagof.paths._base import BaseWrapper

    def boom(self: object, kind: object) -> object:
        raise AssertionError("supports() read the property")

    monkeypatch.setattr(BaseWrapper, "_info_view", boom)
    assert Path(tmp_path).supports("pathinfo")
    assert AsyncPath(tmp_path).supports("pathinfo")
    assert "pathinfo" in Path(tmp_path).capabilities()
    assert not PurePath(str(tmp_path)).supports("pathinfo")
    assert not hasattr(PurePath(str(tmp_path)), "pathinfo")


# -- stdlib: from the listing -------------------------------------------------
def test_listed_children_answer_from_the_listing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(tmp_path)
    children = {c.name: c for c in Path(tmp_path).iterdir()}
    calls = _count_os_stats(monkeypatch)
    for child in children.values():
        for name in _ANSWERS:
            getattr(child.pathinfo, name)()
        child.pathinfo.exists(follow_symlinks=False)
        child.pathinfo.is_dir(follow_symlinks=False)
    assert calls == []  # the types came from os.scandir's entries
    assert children["sub"].pathinfo.is_dir()
    assert not children["sub"].pathinfo.is_file()
    assert children["scan.nii"].pathinfo.is_file()
    assert children["scan.nii"].pathinfo.size() == 4
    assert children["sub"].pathinfo.size() is None
    mtime = children["scan.nii"].pathinfo.mtime()
    assert mtime == pytest.approx((tmp_path / "scan.nii").stat().st_mtime)


def test_iterdir_children_are_the_same_paths(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(tmp_path)
    ours = sorted(str(c) for c in Path(tmp_path).iterdir())
    assert ours == sorted(str(c) for c in tmp_path.iterdir())
    monkeypatch.chdir(tmp_path)
    assert sorted(str(c) for c in Path(".").iterdir()) == sorted(
        str(c) for c in pathlib.Path(".").iterdir()
    )


def test_iterdir_errors_as_pathlib_does(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "nope"
    try:
        expected = list(missing.iterdir())
    except OSError as error:
        expected = type(error)
    with pytest.raises(expected):
        list(Path(missing).iterdir())
    if sys.version_info >= (3, 13):
        # pathlib 3.13 reads the directory when iterdir() is called.
        with pytest.raises(FileNotFoundError):
            Path(missing).iterdir()
    else:
        Path(missing).iterdir()  # earlier pathlib, when iterated


def test_info_is_left_as_the_drivers_own(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path)
    (child, *_rest) = Path(tmp_path).iterdir()
    if sys.version_info >= (3, 14):
        # pathlib's own listing entry, as pathlib.Path.iterdir() gives it.
        assert type(child.info) is type(next(tmp_path.iterdir()).info)
    else:
        with pytest.raises(NotImplementedError):
            child.info  # noqa: B018 - no info before 3.14, as before


def test_listed_snapshot_survives_a_delete(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path)
    children = {c.name: c for c in Path(tmp_path).iterdir()}
    scan, sub = children["scan.nii"], children["sub"]
    (tmp_path / "scan.nii").unlink()
    (tmp_path / "sub" / "inner.txt").unlink()
    (tmp_path / "sub").rmdir()
    # What the listing said ...
    assert scan.pathinfo.is_file() and scan.pathinfo.exists()
    assert sub.pathinfo.is_dir() and sub.pathinfo.exists()
    # ... while the path itself asks the file system.
    assert not scan.exists() and not scan.is_file()
    assert not sub.exists() and not sub.is_dir()


def test_a_later_query_never_overrides_the_listing(
    tmp_path: pathlib.Path,
) -> None:
    _tree(tmp_path)
    scan = next(c for c in Path(tmp_path).iterdir() if c.name == "scan.nii")
    (tmp_path / "scan.nii").unlink()
    # The size was not asked before the delete: it is read now, and the file
    # is gone. What the listing said about its type still stands.
    assert scan.pathinfo.size() is None
    assert scan.pathinfo.is_file() and scan.pathinfo.exists()


def test_unlisted_path_asks_once_and_keeps_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(tmp_path)
    scan = Path(tmp_path) / "scan.nii"
    calls = _count_os_stats(monkeypatch)
    assert _answers(scan.pathinfo) == {
        "dir": False, "file": True, "link": False, "exists": True, "size": 4,
    }
    scan.pathinfo.mtime()
    assert calls == ["lstat"]  # one lstat answers everything
    (tmp_path / "scan.nii").unlink()
    # As pathlib 3.14's info: the answer is the one obtained.
    assert scan.pathinfo.is_file() and scan.pathinfo.exists()
    assert not scan.exists()


def test_missing_path(tmp_path: pathlib.Path) -> None:
    info = (Path(tmp_path) / "nope").pathinfo
    assert _answers(info) == {
        "dir": False, "file": False, "link": False, "exists": False,
        "size": None,
    }
    assert info.mtime() is None
    # A name too long is not there either: the info never raises.
    assert Path(tmp_path / ("a" * 300)).pathinfo.exists() is False


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
@pytest.mark.parametrize("listed", [True, False])
def test_symlinks_as_pathlib_reads_them(
    tmp_path: pathlib.Path, listed: bool,
) -> None:
    _tree(tmp_path)
    (tmp_path / "to-sub").symlink_to(tmp_path / "sub", True)
    (tmp_path / "to-scan").symlink_to(tmp_path / "scan.nii")
    (tmp_path / "dangling").symlink_to(tmp_path / "gone")
    if listed:
        paths = {c.name: c for c in Path(tmp_path).iterdir()}
    else:
        paths = {c.name: Path(c) for c in tmp_path.iterdir()}
    for name in ("to-sub", "to-scan", "dangling"):
        info, real = paths[name].pathinfo, tmp_path / name
        for follow in (True, False):
            assert info.exists(follow_symlinks=follow) == (
                real.exists() if follow else True
            ), name
            assert info.is_dir(follow_symlinks=follow) == (
                real.is_dir() if follow else False
            ), name
            assert info.is_file(follow_symlinks=follow) == (
                real.is_file() if follow else False
            ), name
        assert info.is_symlink()
    assert paths["to-scan"].pathinfo.size() == 4
    assert paths["dangling"].pathinfo.size() is None
    assert paths["dangling"].pathinfo.mtime() is None


# -- never on a derived path --------------------------------------------------
def test_no_leakage_to_derived_paths(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path)
    sub = next(c for c in Path(tmp_path).iterdir() if c.name == "sub")
    assert sub.pathinfo.is_dir()
    derived = [
        sub / "inner.txt", sub.joinpath("inner.txt"), sub.parent,
        sub.with_name("scan.nii"), sub.with_suffix(".x"),
        sub.with_wrapped(sub.wrapped), sub.absolute(), Path(sub),
        pickle.loads(pickle.dumps(sub)),
    ]
    for path in derived:
        assert path._facts() is None, path
        assert path.pathinfo is not sub.pathinfo
    assert (sub / "inner.txt").pathinfo.is_file()
    assert not sub.with_name("scan.nii").pathinfo.is_dir()


# -- walk and glob ------------------------------------------------------------
def test_walk_directories_carry_their_listing(tmp_path: pathlib.Path) -> None:
    from bagof.paths._drivers import GenericAdapter

    _tree(tmp_path)
    # The synthesized walk (pathlib < 3.12), driven directly.
    rows = list(GenericAdapter()._walk_fallback(
        Path(tmp_path), top_down=True, on_error=None, follow_symlinks=False,
    ))
    (top, _, _), (sub, _, files) = rows
    assert top._facts() is None  # the start was not listed
    assert files == ["inner.txt"]
    assert sub._listing_facts() is not None and sub.pathinfo.is_dir()


# -- an unknown driver --------------------------------------------------------
class _Plain(os.PathLike):
    """A driver with no detailed listing: names, and a status per call."""

    asked: list = []

    def __init__(self, real: pathlib.Path) -> None:
        self._real = pathlib.Path(real)

    def __fspath__(self) -> str:
        return os.fspath(self._real)

    def __str__(self) -> str:
        return str(self._real)

    def __truediv__(self, other: object) -> "_Plain":
        return _Plain(self._real / other)

    @property
    def name(self) -> str:
        return self._real.name

    def iterdir(self) -> object:
        return (_Plain(p) for p in self._real.iterdir())

    def exists(self) -> bool:
        self.asked.append("exists")
        return self._real.exists()

    def is_dir(self) -> bool:
        self.asked.append("is_dir")
        return self._real.is_dir()

    def is_file(self) -> bool:
        self.asked.append("is_file")
        return self._real.is_file()

    def stat(self) -> os.stat_result:
        self.asked.append("stat")
        return self._real.stat()


def test_driver_without_a_detailed_listing(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path)
    _Plain.asked = []
    children = {c.name: c for c in Path(_Plain(tmp_path)).iterdir()}
    assert _Plain.asked == []
    scan = children["scan.nii"]
    assert scan.pathinfo.is_file() and scan.pathinfo.exists()
    assert scan.pathinfo.size() == 4
    assert not scan.pathinfo.is_symlink()  # no symlinks in this store
    asked = list(_Plain.asked)
    # Each question asked once, then kept.
    assert scan.pathinfo.is_file() and scan.pathinfo.size() == 4
    assert _Plain.asked == asked
    assert asked.count("stat") == 1 and asked.count("is_file") == 1
    assert children["sub"].pathinfo.is_dir()
    assert children["sub"].pathinfo.size() is None


# -- AsyncPath over the stdlib ------------------------------------------------
def test_async_listed_children_answer_without_a_thread(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bagof.paths import _bridge

    _tree(tmp_path)

    async def go() -> object:
        children = {c.name: c async for c in AsyncPath(tmp_path).iterdir()}
        hops = []
        original = _bridge.run

        async def counted(func: object, *args: object, **kw: object) -> object:
            hops.append(func)
            return await original(func, *args, **kw)

        monkeypatch.setattr(_bridge, "run", counted)
        types = {
            name: (
                await c.pathinfo.is_dir(), await c.pathinfo.is_file(),
                await c.pathinfo.exists(), await c.pathinfo.is_symlink(),
            )
            for name, c in children.items()
        }
        listed_hops = len(hops)
        size = await children["scan.nii"].pathinfo.size()
        missing = await (AsyncPath(tmp_path) / "nope").pathinfo.exists()
        return types, listed_hops, size, missing, len(hops)

    types, listed_hops, size, missing, hops = asyncio.run(go())
    assert types == {
        "sub": (True, False, True, False),
        "scan.nii": (False, True, True, False),
    }
    assert listed_hops == 0  # the listing's answers: no store call at all
    # Anything else is asked in a worker thread, never on the loop.
    assert (size, missing, hops) == (4, False, 2)


def test_async_snapshot_survives_a_delete(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path)

    async def go() -> object:
        scan = AsyncPath(tmp_path) / "scan.nii"
        before = await scan.pathinfo.is_file()
        await scan.unlink()
        return before, await scan.pathinfo.is_file(), await scan.exists()

    assert asyncio.run(go()) == (True, True, False)


# -- universal-pathlib over memory:// -----------------------------------------
_COUNTED = ("ls", "isdir", "isfile", "info", "exists", "glob", "find")


def _count_memory_calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    memory = pytest.importorskip("fsspec.implementations.memory")
    counts = dict.fromkeys(_COUNTED, 0)
    for name in _COUNTED:
        original = getattr(memory.MemoryFileSystem, name)

        def counted(
            self: object, *args: object, _name: str = name,
            _original: object = original, **kwargs: object,
        ) -> object:
            counts[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(memory.MemoryFileSystem, name, counted)
    return counts


def _memory_tree(root: str) -> object:
    UPath = pytest.importorskip("upath").UPath
    base = UPath(root)
    (base / "sub" / "inner.txt").write_bytes(b"in")
    (base / "sub" / "deeper" / "x.nii").write_bytes(b"xx")
    (base / "scan.nii").write_bytes(b"0123")
    return base


def _ask_everything(paths: object) -> dict:
    return {
        p.name: (
            p.pathinfo.is_dir(), p.pathinfo.is_file(), p.pathinfo.exists(),
            p.pathinfo.is_symlink(), p.pathinfo.size(),
            p.pathinfo.mtime() is not None,
        )
        for p in paths
    }


def test_upath_iterdir_children_cost_no_further_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _memory_tree("memory://bagofpaths-info-iterdir/root")
    counts = _count_memory_calls(monkeypatch)
    answers = _ask_everything(Path(base).iterdir())
    assert answers == {
        "sub": (True, False, True, False, None, False),
        "scan.nii": (False, True, True, False, 4, True),
    }
    # The one listing; no child is asked anything.
    assert {k: v for k, v in counts.items() if v} == {"ls": 1}


def test_upath_iterdir_children_are_upaths_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _memory_tree("memory://bagofpaths-info-same/root")
    ours = [c.wrapped for c in Path(base).iterdir()]
    assert ours == list(base.iterdir())
    assert [type(c) for c in ours] == [type(c) for c in base.iterdir()]
    with pytest.raises(NotADirectoryError):
        list(Path(base / "nope").iterdir())
    with pytest.raises(NotADirectoryError):
        list(Path(base / "scan.nii").iterdir())


@pytest.mark.parametrize(
    "method, pattern",
    [
        ("glob", "*"), ("glob", "*.nii"), ("glob", "*/"), ("glob", "**"),
        ("glob", "sub/*"), ("glob", "**/*.nii"), ("rglob", "*.nii"),
        ("rglob", "*"), ("rglob", "*/"),
    ],
)
def test_upath_glob_matches_upaths_own(
    monkeypatch: pytest.MonkeyPatch, method: str, pattern: str,
) -> None:
    base = _memory_tree("memory://bagofpaths-info-glob/root")
    counts = _count_memory_calls(monkeypatch)
    native = [str(p) for p in getattr(base, method)(pattern)]
    native_calls = dict(counts)
    counts.update(dict.fromkeys(counts, 0))
    matches = list(getattr(Path(base), method)(pattern))
    assert [str(p) for p in matches] == native
    # The same fs.glob as UPath's own: the detail costs nothing more ...
    assert counts == native_calls
    counts.update(dict.fromkeys(counts, 0))
    asked = _ask_everything(matches)
    # ... and the answers cost no further call.
    assert counts == dict.fromkeys(_COUNTED, 0)
    for name, (is_dir, is_file, exists, *_rest) in asked.items():
        assert exists and is_dir != is_file, name


def test_upath_walk_directories_carry_their_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _memory_tree("memory://bagofpaths-info-walk/root")
    rows = list(Path(base).walk())
    counts = _count_memory_calls(monkeypatch)
    assert {dp.name: dp.pathinfo.is_dir() for dp, _, _ in rows[1:]} == {
        "sub": True, "deeper": True,
    }
    assert counts == dict.fromkeys(_COUNTED, 0)


def test_upath_unlisted_path_asks_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _memory_tree("memory://bagofpaths-info-once/root")
    scan = Path(base) / "scan.nii"
    counts = _count_memory_calls(monkeypatch)
    assert _ask_everything([scan]) == {
        "scan.nii": (False, True, True, False, 4, True),
    }
    assert {k: v for k, v in counts.items() if v} == {"info": 1}
    (base / "scan.nii").unlink()
    assert scan.pathinfo.is_file()  # kept
    assert not scan.exists()  # the live answer
    missing = Path(base) / "nope"
    assert not missing.pathinfo.exists() and missing.pathinfo.size() is None


def test_upath_listing_without_detail_asks_each_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = pytest.importorskip("fsspec.implementations.memory")
    base = _memory_tree("memory://bagofpaths-info-nodetail/root")
    original = memory.MemoryFileSystem.ls

    def names_only(
        self: object, path: str, detail: bool = False, **kwargs: object
    ) -> object:
        return original(self, path, detail=False, **kwargs)

    monkeypatch.setattr(memory.MemoryFileSystem, "ls", names_only)
    children = list(Path(base).iterdir())
    assert all(c._facts() is None for c in children)
    assert _ask_everything(children) == {
        "sub": (True, False, True, False, None, False),
        "scan.nii": (False, True, True, False, 4, True),
    }


def test_upath_no_leakage_and_info_untouched() -> None:
    base = _memory_tree("memory://bagofpaths-info-leak/root")
    sub = next(c for c in Path(base).iterdir() if c.name == "sub")
    assert sub.pathinfo.is_dir()
    for derived in (sub / "inner.txt", sub.parent, sub.with_name("x")):
        assert derived._facts() is None
    assert type(sub.info) is type(base.info)  # the driver's own object


def test_async_upath_children_cost_no_further_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A synchronous driver run in a thread: its listing's answers travel.
    base = _memory_tree("memory://bagofpaths-info-async/root")

    async def go() -> object:
        found = {}
        children = [c async for c in AsyncPath(base).iterdir()]
        matches = [m async for m in AsyncPath(base).rglob("*.nii")]
        counts = _count_memory_calls(monkeypatch)
        for path in children + matches:
            found[path.name] = (
                await path.pathinfo.is_dir(), await path.pathinfo.size()
            )
        return found, {k: v for k, v in counts.items() if v}

    found, calls = asyncio.run(go())
    assert found == {
        "sub": (True, None), "scan.nii": (False, 4), "x.nii": (False, 2),
    }
    assert calls == {}


# -- cloudpathlib -------------------------------------------------------------
def test_cloudpathlib_asks_once_and_keeps_it() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    root = client.CloudPath("s3://bagofpaths-info/root")
    (root / "sub" / "inner.txt").write_bytes(b"in")
    (root / "scan.nii").write_bytes(b"0123")
    children = {c.name: c for c in Path(root).iterdir()}
    scan, sub = children["scan.nii"], children["sub"]
    assert (scan.pathinfo.is_file(), scan.pathinfo.size()) == (True, 4)
    assert scan.pathinfo.mtime() is not None
    assert (sub.pathinfo.is_dir(), sub.pathinfo.size()) == (True, None)
    assert not scan.pathinfo.is_symlink()  # no symlinks in a bucket
    scan.unlink()
    assert scan.pathinfo.is_file() and scan.pathinfo.exists()  # kept
    assert not scan.exists()
    assert not (Path(root) / "nope").pathinfo.exists()
