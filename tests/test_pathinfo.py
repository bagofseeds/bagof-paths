"""`pathinfo`: what a path is, answered as pathlib 3.14's `info` (#64).

The answers come in two views, as 3.14 keeps them: the path itself (its
type, as a listing saw it, or one `lstat`) and what it leads to (one
`stat`, read the first time `exists()`, `size()` or `mtime()` -- or the
type of anything not listed -- is asked). Each is kept once obtained. The
3.14 table below is measured, and every driver is held to it. The stdlib
tests run on every interpreter, the 3.8 core-only leg included; the backend
tests need universal-pathlib or cloudpathlib.
"""

import asyncio
import os
import pathlib
import pickle
import shutil
import sys

import pytest

from bagof.paths import (
    AsyncPath,
    AsyncPathInfo,
    Path,
    PathInfo,
    PurePath,
)

# What pathlib 3.14's `Path.info` answers (measured, CPython 3.14.6 on
# Linux) for a path `t` that is listed (`iterdir`) or not (`root / "t"`),
# after it is changed. Each string answers, in order: exists(),
# is_dir(), is_file(), exists(follow_symlinks=False),
# is_dir(follow_symlinks=False), is_file(follow_symlinks=False),
# is_symlink(); the three strings are for nothing asked before the change,
# is_file() asked before it, and is_symlink() asked before it.
_ORDER = (
    ("exists", True), ("is_dir", True), ("is_file", True),
    ("exists", False), ("is_dir", False), ("is_file", False),
    ("is_symlink", None),
)
_PRE = ("", "is_file", "is_symlink")
_PYTHON_314 = {
    ("listed", "file", "none"): ("1011010", "1011010", "1011010"),
    ("listed", "file", "delete"): ("0011010", "0011010", "0011010"),
    ("listed", "file", "replace"): ("1011010", "1011010", "1011010"),
    ("listed", "file", "type-change"): ("1011010", "1011010", "1011010"),
    ("listed", "dir", "none"): ("1101100", "1101100", "1101100"),
    ("listed", "dir", "delete"): ("0101100", "0101100", "0101100"),
    ("listed", "dir", "replace"): ("1101100", "1101100", "1101100"),
    ("listed", "dir", "type-change"): ("1101100", "1101100", "1101100"),
    ("listed", "link-file", "none"): ("1011001", "1011001", "1011001"),
    ("listed", "link-file", "delete"): ("0001001", "1011001", "0001001"),
    ("listed", "link-file", "replace"): ("1011001", "1011001", "1011001"),
    ("listed", "link-file", "type-change"): (
        "1101001", "1011001", "1101001"
    ),
    ("listed", "link-file", "delete-target"): (
        "0001001", "1011001", "0001001"
    ),
    ("listed", "link-dir", "none"): ("1101001", "1101001", "1101001"),
    ("listed", "link-dir", "delete"): ("0001001", "1101001", "0001001"),
    ("listed", "link-dir", "replace"): ("1011001", "1101001", "1011001"),
    ("listed", "link-dir", "type-change"): ("1101001", "1101001", "1101001"),
    ("listed", "link-dir", "delete-target"): (
        "0001001", "1101001", "0001001"
    ),
    ("listed", "dangling", "none"): ("0001001", "0001001", "0001001"),
    ("listed", "dangling", "delete"): ("0001001", "0001001", "0001001"),
    ("listed", "dangling", "replace"): ("1011001", "1011001", "1011001"),
    ("listed", "dangling", "type-change"): ("1101001", "1101001", "1101001"),
    ("unlisted", "file", "none"): ("1011010", "1011010", "1011010"),
    ("unlisted", "file", "delete"): ("0000000", "1010000", "0001010"),
    ("unlisted", "file", "replace"): ("1011010", "1011010", "1011010"),
    ("unlisted", "file", "type-change"): ("1101100", "1011100", "1101010"),
    ("unlisted", "dir", "none"): ("1101100", "1101100", "1101100"),
    ("unlisted", "dir", "delete"): ("0000000", "1100000", "0001100"),
    ("unlisted", "dir", "replace"): ("1101100", "1101100", "1101100"),
    ("unlisted", "dir", "type-change"): ("1011010", "1101010", "1011100"),
    ("unlisted", "link-file", "none"): ("1011001", "1011001", "1011001"),
    ("unlisted", "link-file", "delete"): ("0000000", "1010000", "0001001"),
    ("unlisted", "link-file", "replace"): ("1011001", "1011001", "1011001"),
    ("unlisted", "link-file", "type-change"): (
        "1101100", "1011100", "1101001"
    ),
    ("unlisted", "link-file", "delete-target"): (
        "0001001", "1011001", "0001001"
    ),
    ("unlisted", "link-dir", "none"): ("1101001", "1101001", "1101001"),
    ("unlisted", "link-dir", "delete"): ("0000000", "1100000", "0001001"),
    ("unlisted", "link-dir", "replace"): ("1011001", "1101001", "1011001"),
    ("unlisted", "link-dir", "type-change"): (
        "1101100", "1101100", "1101001"
    ),
    ("unlisted", "link-dir", "delete-target"): (
        "0001001", "1101001", "0001001"
    ),
    ("unlisted", "dangling", "none"): ("0001001", "0001001", "0001001"),
    ("unlisted", "dangling", "delete"): ("0000000", "0000000", "0001001"),
    ("unlisted", "dangling", "replace"): ("1011001", "0001001", "1011001"),
    ("unlisted", "dangling", "type-change"): (
        "1101100", "0001100", "1101001"
    ),
}
_SCENARIOS = [
    (listed, kind, how, pre)
    for (listed, kind, how) in _PYTHON_314
    for pre in _PRE
]
_LINK_FREE = [s for s in _SCENARIOS if s[1] in ("file", "dir")]
# A key cannot become a prefix in place on cloudpathlib's local test store
# (the emptied directory stays behind), so a type change is not tried there.
_CLOUD = [s for s in _LINK_FREE if s[2] != "type-change"]
_HAS_LINKS = hasattr(os, "symlink") and os.name != "nt"


def _expected(listed: str, kind: str, how: str, pre: str) -> str:
    return _PYTHON_314[listed, kind, how][_PRE.index(pre)]


def _bits(info: object) -> str:
    out = ""
    for name, follow in _ORDER:
        method = getattr(info, name)
        out += str(int(method() if follow is None else method(
            follow_symlinks=follow
        )))
    return out


def _pre(info: object, pre: str) -> None:
    if pre:
        getattr(info, pre)()


# -- a local tree, changed as the table says ----------------------------------
def _build(root: pathlib.Path, kind: str) -> None:
    target = root / "t"
    if kind == "file":
        target.write_bytes(b"1234")
    elif kind == "dir":
        target.mkdir()
    elif kind == "link-file":
        (root / "target").write_bytes(b"1234")
        target.symlink_to(root / "target")
    elif kind == "link-dir":
        (root / "target").mkdir()
        target.symlink_to(root / "target", True)
    else:
        target.symlink_to(root / "gone")


def _change(root: pathlib.Path, how: str) -> None:
    target = root / "t"
    if how == "delete":
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)
    elif how == "delete-target":
        other = root / "target"
        if other.is_dir():
            shutil.rmtree(other)
        else:
            other.unlink()
    elif how == "replace":
        if target.is_symlink():
            target.unlink()
            (root / "other").write_bytes(b"xx")
            target.symlink_to(root / "other")
        elif target.is_file():
            target.unlink()
            target.write_bytes(b"xx")
        else:
            shutil.rmtree(target)
            target.mkdir()
    elif how == "type-change":
        if target.is_symlink() or target.is_file():
            target.unlink()
            target.mkdir()
        else:
            shutil.rmtree(target)
            target.write_bytes(b"xx")


def _skip_links(kind: str) -> None:
    if kind not in ("file", "dir") and not _HAS_LINKS:
        pytest.skip("no symbolic links here")


@pytest.mark.parametrize("listed, kind, how, pre", _SCENARIOS)
def test_answers_as_pathlib_314_does(
    tmp_path: pathlib.Path, listed: str, kind: str, how: str, pre: str,
) -> None:
    _skip_links(kind)
    _build(tmp_path, kind)
    if listed == "listed":
        (path,) = [c for c in Path(tmp_path).iterdir() if c.name == "t"]
    else:
        path = Path(tmp_path) / "t"
    _pre(path.pathinfo, pre)
    _change(tmp_path, how)
    assert _bits(path.pathinfo) == _expected(listed, kind, how, pre)


@pytest.mark.skipif(sys.version_info < (3, 14), reason="pathlib 3.14 info")
@pytest.mark.parametrize("listed, kind, how, pre", _SCENARIOS)
def test_the_table_is_pathlib_314s_own(
    tmp_path: pathlib.Path, listed: str, kind: str, how: str, pre: str,
) -> None:
    _skip_links(kind)
    _build(tmp_path, kind)
    if listed == "listed":
        (path,) = [c for c in tmp_path.iterdir() if c.name == "t"]
    else:
        path = tmp_path / "t"
    _pre(path.info, pre)
    _change(tmp_path, how)
    assert _bits(path.info) == _expected(listed, kind, how, pre)


class _Awaited:
    """An AsyncPathInfo read as a PathInfo, one event loop per call."""

    def __init__(self, info: AsyncPathInfo) -> None:
        self._info = info

    def __getattr__(self, name: str) -> object:
        method = getattr(self._info, name)
        return lambda **kw: asyncio.run(method(**kw))


@pytest.mark.parametrize("listed, kind, how, pre", _SCENARIOS)
def test_async_answers_as_pathlib_314_does(
    tmp_path: pathlib.Path, listed: str, kind: str, how: str, pre: str,
) -> None:
    _skip_links(kind)
    _build(tmp_path, kind)

    async def child() -> AsyncPath:
        return [c async for c in AsyncPath(tmp_path).iterdir()
                if c.name == "t"][0]

    if listed == "listed":
        path = asyncio.run(child())
    else:
        path = AsyncPath(tmp_path) / "t"
    info = _Awaited(path.pathinfo)
    _pre(info, pre)
    _change(tmp_path, how)
    assert _bits(info) == _expected(listed, kind, how, pre)


# -- the public surface -------------------------------------------------------
def _tree(root: pathlib.Path) -> None:
    (root / "sub").mkdir()
    (root / "sub" / "inner.txt").write_bytes(b"in")
    (root / "scan.nii").write_bytes(b"0123")


def _count_os_stats(
    monkeypatch: pytest.MonkeyPatch, root: pathlib.Path,
) -> list:
    """Record every os.stat / os.lstat made on a path under `root`.

    Calls on other paths are not counted: a tracer such as coverage stats
    its own files while the test runs.
    """
    calls: list = []
    prefix = os.fspath(root)
    for name in ("stat", "lstat"):
        original = getattr(os, name)

        def counted(
            *args: object, _name: str = name, _original: object = original,
            **kwargs: object,
        ) -> object:
            target = args[0] if args else kwargs.get("path")
            if isinstance(target, (str, bytes, os.PathLike)):
                if os.fsdecode(os.fspath(target)).startswith(prefix):
                    calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(os, name, counted)
    return calls


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


# -- stdlib: what costs what --------------------------------------------------
def test_listed_types_cost_nothing_and_status_one_stat(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(tmp_path)
    mtime = (tmp_path / "scan.nii").stat().st_mtime
    children = {c.name: c for c in Path(tmp_path).iterdir()}
    calls = _count_os_stats(monkeypatch, tmp_path)
    for child in children.values():
        info = child.pathinfo
        info.is_dir(), info.is_file(), info.is_symlink()
        info.exists(follow_symlinks=False)
        info.is_dir(follow_symlinks=False)
    assert calls == []  # the types came from the listing
    scan = children["scan.nii"].pathinfo
    assert (scan.is_file(), scan.size()) == (True, 4)
    assert scan.exists() and scan.mtime() == pytest.approx(mtime)
    assert children["sub"].pathinfo.size() is None
    # exists(), size() and mtime(): one os.stat per path, when first asked.
    assert calls == ["stat", "stat"]


def test_unlisted_path_reads_each_view_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(tmp_path)
    scan = Path(tmp_path) / "scan.nii"
    calls = _count_os_stats(monkeypatch, tmp_path)
    info = scan.pathinfo
    assert (info.is_file(), info.exists(), info.size()) == (True, True, 4)
    assert info.mtime() is not None and not info.is_dir()
    assert calls == ["stat"]  # what the path leads to
    assert not info.is_symlink() and info.exists(follow_symlinks=False)
    assert calls == ["stat", "lstat"]  # the path itself
    (tmp_path / "scan.nii").unlink()
    assert info.is_file() and info.exists() and info.size() == 4  # kept
    assert not scan.exists()  # the live answer


def test_listed_status_is_read_when_first_asked(
    tmp_path: pathlib.Path,
) -> None:
    # 3.14's DirEntryInfo: the type is the listing's, exists() is not.
    _tree(tmp_path)
    scan = next(c for c in Path(tmp_path).iterdir() if c.name == "scan.nii")
    (tmp_path / "scan.nii").unlink()
    assert scan.pathinfo.is_file()
    assert not scan.pathinfo.exists()
    assert scan.pathinfo.size() is None and scan.pathinfo.mtime() is None


def test_missing_path(tmp_path: pathlib.Path) -> None:
    info = (Path(tmp_path) / "nope").pathinfo
    assert _bits(info) == "0000000"
    assert info.size() is None and info.mtime() is None
    # A name too long is not there either: the info never raises.
    assert Path(tmp_path / ("a" * 300)).pathinfo.exists() is False


@pytest.mark.skipif(not _HAS_LINKS, reason="no symlinks")
def test_link_size_is_what_it_leads_to(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path)
    (tmp_path / "to-scan").symlink_to(tmp_path / "scan.nii")
    (tmp_path / "dangling").symlink_to(tmp_path / "gone")
    paths = {c.name: c for c in Path(tmp_path).iterdir()}
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
    assert _Plain.asked == ["is_file"]  # a file: the rest follows
    assert scan.pathinfo.size() == 4
    assert _Plain.asked == ["is_file", "stat"]
    # No symlinks in this store; the path itself is asked apart, as
    # pathlib 3.14 asks its lstat apart from its stat.
    assert not scan.pathinfo.is_symlink()
    assert _Plain.asked == ["is_file", "stat", "exists", "is_file"]
    assert scan.pathinfo.is_file() and scan.pathinfo.size() == 4
    assert scan.pathinfo.exists(follow_symlinks=False)
    assert len(_Plain.asked) == 4  # kept
    assert children["sub"].pathinfo.is_dir()
    assert children["sub"].pathinfo.size() is None


@pytest.mark.parametrize(
    "listed, kind, how, pre", [s for s in _LINK_FREE if s[0] == "unlisted"]
)
def test_unknown_driver_answers_as_pathlib_314_does(
    tmp_path: pathlib.Path, listed: str, kind: str, how: str, pre: str,
) -> None:
    # Its listing says nothing, so its children are unlisted. Its answers
    # come one call at a time, but a view's type answers are asked
    # together, so they describe one moment, as one stat does.
    _build(tmp_path, kind)
    path = Path(_Plain(tmp_path)) / "t"
    _pre(path.pathinfo, pre)
    _change(tmp_path, how)
    assert _bits(path.pathinfo) == _expected(listed, kind, how, pre)


# -- AsyncPath over the stdlib ------------------------------------------------
def test_async_listed_types_answer_without_a_thread(
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
                await c.pathinfo.is_symlink(),
            )
            for name, c in children.items()
        }
        listed_hops = len(hops)
        size = await children["scan.nii"].pathinfo.size()
        exists = await children["scan.nii"].pathinfo.exists()
        missing = await (AsyncPath(tmp_path) / "nope").pathinfo.exists()
        return types, listed_hops, size, exists, missing, len(hops)

    types, listed_hops, size, exists, missing, hops = asyncio.run(go())
    assert types == {
        "sub": (True, False, False), "scan.nii": (False, True, False),
    }
    assert listed_hops == 0  # the listing's answers: no store call at all
    # Anything else is asked in a worker thread, never on the loop.
    assert (size, exists, missing, hops) == (4, True, False, 2)


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


def test_upath_iterdir_types_cost_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _memory_tree("memory://bagofpaths-info-iterdir/root")
    counts = _count_memory_calls(monkeypatch)
    children = list(Path(base).iterdir())
    assert {k: v for k, v in counts.items() if v} == {"ls": 1}
    types = {
        c.name: (c.pathinfo.is_dir(), c.pathinfo.is_file(),
                 c.pathinfo.is_symlink())
        for c in children
    }
    assert types == {
        "sub": (True, False, False), "scan.nii": (False, True, False),
    }
    assert {k: v for k, v in counts.items() if v} == {"ls": 1}
    assert _ask_everything(children) == {
        "sub": (True, False, True, False, None, False),
        "scan.nii": (False, True, True, False, 4, True),
    }
    # exists(), size(), mtime(): one fs.info per child, when first asked.
    assert {k: v for k, v in counts.items() if v} == {"ls": 1, "info": 2}


def test_upath_iterdir_children_are_upaths_own() -> None:
    base = _memory_tree("memory://bagofpaths-info-same/root")
    ours = [c.wrapped for c in Path(base).iterdir()]
    assert ours == list(base.iterdir())
    assert [type(c) for c in ours] == [type(c) for c in base.iterdir()]
    with pytest.raises(NotADirectoryError):
        list(Path(base / "nope").iterdir())
    with pytest.raises(NotADirectoryError):
        list(Path(base / "scan.nii").iterdir())


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


def test_upath_unlisted_path_reads_each_view_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _memory_tree("memory://bagofpaths-info-once/root")
    scan = Path(base) / "scan.nii"
    counts = _count_memory_calls(monkeypatch)
    assert _ask_everything([scan]) == {
        "scan.nii": (False, True, True, False, 4, True),
    }
    _ask_everything([scan])
    # One fs.info for what it leads to, one for the path itself.
    assert {k: v for k, v in counts.items() if v} == {"info": 2}
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


def test_async_upath_children_carry_their_types(
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
                await path.pathinfo.is_dir(), await path.pathinfo.is_file()
            )
        return found, {k: v for k, v in counts.items() if v}

    found, calls = asyncio.run(go())
    assert found == {
        "sub": (True, False), "scan.nii": (False, True),
        "x.nii": (False, True),
    }
    assert calls == {}


# -- every link-free scenario, on the stores ----------------------------------
def _store_change(
    target: object, kind: str, how: str, make_dir: object, remove_dir: object,
) -> None:
    if how == "none":
        return
    if kind == "file":
        target.unlink()
    else:
        remove_dir(target)
    if how == "delete":
        return
    if (how == "replace") == (kind == "file"):
        target.write_bytes(b"xx")
    else:
        make_dir(target)


def _store_scenario(
    root: object, listed: str, kind: str, how: str, pre: str,
    make_dir: object, remove_dir: object,
) -> str:
    target = root / "t"
    if kind == "file":
        target.write_bytes(b"1234")
    else:
        make_dir(target)
    path = (
        next(c for c in Path(root).iterdir() if c.name == "t")
        if listed == "listed" else Path(root) / "t"
    )
    _pre(path.pathinfo, pre)
    _store_change(target, kind, how, make_dir, remove_dir)
    return _bits(path.pathinfo)


@pytest.mark.parametrize("listed, kind, how, pre", _LINK_FREE)
def test_upath_answers_as_pathlib_314_does(
    listed: str, kind: str, how: str, pre: str,
) -> None:
    UPath = pytest.importorskip("upath").UPath
    root = UPath(f"memory://bagofpaths-info-table/{listed}-{kind}-{how}-{pre}")
    got = _store_scenario(
        root, listed, kind, how, pre,
        lambda p: p.mkdir(parents=True), lambda p: p.rmdir(recursive=True),
    )
    assert got == _expected(listed, kind, how, pre)


@pytest.mark.parametrize("listed, kind, how, pre", _CLOUD)
def test_cloudpathlib_answers_as_pathlib_314_does(
    listed: str, kind: str, how: str, pre: str,
) -> None:
    local = pytest.importorskip("cloudpathlib.local")
    root = local.LocalS3Client().CloudPath(
        f"s3://bagofpaths-info-table/{listed}-{kind}-{how}-{pre}"
    )
    # An object store has no empty directory: one holds a file.
    got = _store_scenario(
        root, listed, kind, how, pre,
        lambda p: (p / "x").write_bytes(b"x"), lambda p: p.rmtree(),
    )
    assert got == _expected(listed, kind, how, pre)


def test_cloudpathlib_iterdir_types_cost_no_call() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    root = client.CloudPath("s3://bagofpaths-info/root")
    (root / "sub" / "inner.txt").write_bytes(b"in")
    (root / "scan.nii").write_bytes(b"0123")
    children = {c.name: c for c in Path(root).iterdir()}
    assert sorted(children) == sorted(c.name for c in root.iterdir())
    assert all(c._listing_facts() is not None for c in children.values())
    scan, sub = children["scan.nii"], children["sub"]
    assert (scan.pathinfo.is_file(), scan.pathinfo.size()) == (True, 4)
    assert scan.pathinfo.mtime() is not None
    assert (sub.pathinfo.is_dir(), sub.pathinfo.size()) == (True, None)
    assert not scan.pathinfo.is_symlink()  # no symlinks in a bucket
    scan.unlink()
    assert scan.pathinfo.is_file() and scan.pathinfo.exists()  # kept
    assert not scan.exists()
    assert not (Path(root) / "nope").pathinfo.exists()
