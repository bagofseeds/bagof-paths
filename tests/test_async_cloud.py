"""The native async cloud driver over an fsspec ``AsyncFileSystem``.

These tests do not need cloud credentials or a network: they register an
in-process, natively-async fsspec backend (``memasync``) backed by fsspec's
own in-memory filesystem, and drive ``AsyncPath`` against it end to end.
"""

import asyncio

import pytest

fsspec = pytest.importorskip("fsspec")
# AsyncFileSystemWrapper (a sync filesystem exposed as async) is our
# dependency-free async backend; it landed in a recent fsspec, so skip the
# whole module on an older one rather than fail to collect.
_asyn_wrapper = pytest.importorskip("fsspec.implementations.asyn_wrapper")
AsyncFileSystemWrapper = _asyn_wrapper.AsyncFileSystemWrapper

from fsspec.implementations.memory import MemoryFileSystem  # noqa: E402

from bagof.paths import AsyncPath, Path, UnsupportedPathOperation  # noqa: E402
from bagof.paths._async_fsspec import (  # noqa: E402
    _FS_BY_LOOP,
    AsyncFSPath,
    _as_timestamp,
    _freeze,
    _stat_from_info,
    _sweep_closed_loops,
    is_async_filesystem,
)


class _MemAsyncFS(AsyncFileSystemWrapper):
    """A natively-async in-memory filesystem for the ``memasync`` scheme."""

    protocol = "memasync"
    async_impl = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        # fsspec passes asynchronous=/skip_instance_cache=; the wrapper builds
        # its own async view over a fresh MemoryFileSystem (whose store is
        # class-level, so writes are visible across instances).
        for drop in ("asynchronous", "skip_instance_cache"):
            kwargs.pop(drop, None)
        super().__init__(MemoryFileSystem(), asynchronous=True)

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        text = path.replace("memasync://", "memory://")
        return MemoryFileSystem._strip_protocol(text)


fsspec.register_implementation("memasync", _MemAsyncFS, clobber=True)


@pytest.fixture(autouse=True)
def _clean_store() -> object:
    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]
    yield
    MemoryFileSystem.store.clear()
    MemoryFileSystem.pseudo_dirs[:] = [""]


def _run(coro: object) -> object:
    return asyncio.run(coro)


# -- selection --------------------------------------------------------------
def test_async_url_selects_the_native_driver() -> None:
    p = AsyncPath("memasync://bucket/a.txt")
    assert isinstance(p.wrapped, AsyncFSPath)
    assert p.protocol == "memasync"
    assert p.path == "/bucket/a.txt"


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_sync_path_does_not_get_the_async_driver() -> None:
    # A synchronous Path never wraps the async driver; it uses the ordinary
    # selection (universal-pathlib), which a sync method can drive.
    pytest.importorskip("upath")
    p = Path("memasync://bucket/a.txt")
    assert not isinstance(p.wrapped, AsyncFSPath)


def test_explicit_driver_beats_async_selection() -> None:
    class _Fake:
        def __init__(self, text: str) -> None:
            self.text = text

        def __fspath__(self) -> str:
            return self.text

    p = AsyncPath("memasync://bucket/a.txt", driver=_Fake)
    assert isinstance(p.wrapped, _Fake)


# -- reading and writing ----------------------------------------------------
def test_write_and_read_bytes() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/data.bin")
        n = await p.write_bytes(b"payload")
        return n, await p.read_bytes()

    assert _run(go()) == (7, b"payload")


def test_write_and_read_text() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/note.txt")
        await p.write_text("héllo")
        return await p.read_text()

    assert _run(go()) == "héllo"


def test_open_read_and_write() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/f.txt")
        async with await p.open("w") as fh:
            await fh.write("line one\n")
        collected = []
        async with await p.open("r") as fh:
            async for line in fh:
                collected.append(line)
        return collected

    assert _run(go()) == ["line one\n"]


def test_open_binary() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/raw")
        async with await p.open("wb") as fh:
            await fh.write(b"\x00\x01\x02")
        async with await p.open("rb") as fh:
            return await fh.read()

    assert _run(go()) == b"\x00\x01\x02"


# -- status -----------------------------------------------------------------
def test_status_queries() -> None:
    async def go() -> object:
        f = AsyncPath("memasync://b/dir/file.txt")
        await f.write_bytes(b"x")
        d = AsyncPath("memasync://b/dir")
        return (
            await f.exists(),
            await f.is_file(),
            await f.is_dir(),
            await d.is_dir(),
            await AsyncPath("memasync://b/missing").exists(),
        )

    assert _run(go()) == (True, True, False, True, False)


# -- directory iteration ----------------------------------------------------
def test_iterdir_yields_async_paths() -> None:
    async def go() -> object:
        for name in ("a.txt", "b.txt", "c.txt"):
            await AsyncPath("memasync://b/d/" + name).write_bytes(b"z")
        entries = []
        async for child in AsyncPath("memasync://b/d").iterdir():
            assert isinstance(child, AsyncPath)
            entries.append(child.name)
        return sorted(entries)

    assert _run(go()) == ["a.txt", "b.txt", "c.txt"]


def test_glob_and_rglob() -> None:
    async def go() -> object:
        await AsyncPath("memasync://b/g/a.txt").write_bytes(b"z")
        await AsyncPath("memasync://b/g/sub/b.txt").write_bytes(b"z")
        root = AsyncPath("memasync://b/g")
        top = sorted([c.name async for c in root.glob("*.txt")])
        deep = sorted([c.name async for c in root.rglob("*.txt")])
        return top, deep

    top, deep = _run(go())
    assert top == ["a.txt"]
    assert deep == ["a.txt", "b.txt"]


# -- creation / removal -----------------------------------------------------
def test_mkdir_unlink_rmdir() -> None:
    async def go() -> object:
        f = AsyncPath("memasync://b/rm/x.txt")
        await f.write_bytes(b"z")
        before = await f.exists()
        await f.unlink()
        after = await f.exists()
        # unlink of a missing file: raises, unless missing_ok
        raised = False
        try:
            await f.unlink()
        except FileNotFoundError:
            raised = True
        await f.unlink(missing_ok=True)  # no raise
        return before, after, raised

    assert _run(go()) == (True, False, True)


def test_rmdir_is_not_recursive_by_default() -> None:
    async def go() -> object:
        await AsyncPath("memasync://b/tree/leaf.txt").write_bytes(b"z")
        d = AsyncPath("memasync://b/tree")
        # A non-recursive rmdir of a non-empty directory must not wipe it out.
        failed = False
        try:
            await d.rmdir()
        except Exception:
            failed = True
        still_there = await AsyncPath(
            "memasync://b/tree/leaf.txt"
        ).exists()
        # rmtree (recursive) does remove it.
        await d.rmdir(recursive=True)
        gone = not await AsyncPath("memasync://b/tree/leaf.txt").exists()
        return failed or still_there, gone

    guarded, gone = _run(go())
    assert guarded  # the tree survived a bare rmdir
    assert gone  # recursive removal worked


# -- moving and copying -----------------------------------------------------
def test_copy_move_rename() -> None:
    async def go() -> object:
        src = AsyncPath("memasync://b/m/src.txt")
        await src.write_bytes(b"data")
        # copy to a wrapped target
        dst = AsyncPath("memasync://b/m/copy.txt")
        copied = await src.copy(dst)
        # rename via a string URL target
        renamed = await src.rename("memasync://b/m/moved.txt")
        return (
            isinstance(copied, AsyncPath),
            await AsyncPath("memasync://b/m/copy.txt").read_bytes(),
            renamed.name,
            await AsyncPath("memasync://b/m/moved.txt").read_bytes(),
            await src.exists(),
        )

    is_async, copy_data, moved_name, moved_data, src_exists = _run(go())
    assert is_async
    assert copy_data == b"data"
    assert moved_name == "moved.txt"
    assert moved_data == b"data"
    assert src_exists is False


def test_copy_into_and_move_into() -> None:
    async def go() -> object:
        await AsyncPath("memasync://b/c/one.txt").write_bytes(b"1")
        await AsyncPath("memasync://b/c/two.txt").write_bytes(b"2")
        dest_dir = AsyncPath("memasync://b/dest")
        into = await AsyncPath("memasync://b/c/one.txt").copy_into(dest_dir)
        moved = await AsyncPath("memasync://b/c/two.txt").move_into(dest_dir)
        return (
            into.name,
            await AsyncPath("memasync://b/dest/one.txt").read_bytes(),
            moved.name,
            await AsyncPath("memasync://b/dest/two.txt").read_bytes(),
        )

    assert _run(go()) == ("one.txt", b"1", "two.txt", b"2")


# -- traversal --------------------------------------------------------------
def test_walk() -> None:
    async def go() -> object:
        await AsyncPath("memasync://b/w/a.txt").write_bytes(b"z")
        await AsyncPath("memasync://b/w/sub/b.txt").write_bytes(b"z")
        rows = []
        async for path, dirs, files in AsyncPath("memasync://b/w").walk():
            assert isinstance(path, AsyncPath)
            rows.append((path.name, sorted(dirs), sorted(files)))
        return sorted(rows)

    rows = _run(go())
    assert ("w", ["sub"], ["a.txt"]) in rows
    assert ("sub", [], ["b.txt"]) in rows


# -- lexical members --------------------------------------------------------
def test_lexical_members_are_synchronous() -> None:
    p = AsyncPath("memasync://bucket/dir/file.tar.gz")
    assert p.name == "file.tar.gz"
    assert p.stem == "file.tar"
    assert p.suffix == ".gz"
    assert p.suffixes == [".tar", ".gz"]
    assert "bucket" in p.parts
    assert str(p.parent) == "memasync://bucket/dir"
    assert [pp.name for pp in p.parents][0] == "dir"
    assert (p.parent / "other.txt").name == "other.txt"
    assert p.with_name("z.bin").name == "z.bin"
    assert p.with_suffix(".zip").name == "file.tar.zip"
    assert p.is_absolute() is True
    assert p.as_posix().endswith("file.tar.gz")
    assert p.joinpath("x", "y").name == "y"
    # with_segments with several relative parts joins them lexically.
    assert p.with_segments("a", "b", "c.txt").name == "c.txt"


def test_driver_repr() -> None:
    p = AsyncFSPath.from_url("memasync://bucket/k", "memasync", {})
    assert repr(p) == "AsyncFSPath('memasync://bucket/k')"


# -- more I/O coverage ------------------------------------------------------
def test_is_file_on_missing_is_false() -> None:
    async def go() -> object:
        return await AsyncPath("memasync://b/none.txt").is_file()

    assert _run(go()) is False


def test_mkdir() -> None:
    async def go() -> object:
        d = AsyncPath("memasync://b/newdir")
        await d.mkdir(parents=True, exist_ok=True)
        # A file placed inside proves the directory is usable.
        await (d / "inside.txt").write_bytes(b"z")
        return await (d / "inside.txt").exists()

    assert _run(go()) is True


def test_read_write_text_with_encoding() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/enc.txt")
        await p.write_text("café", encoding="latin-1")
        return await p.read_text(encoding="latin-1")

    assert _run(go()) == "café"


def test_open_append() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/log.txt")
        await p.write_text("one\n")
        async with await p.open("a") as fh:
            await fh.write("two\n")
        return await p.read_text()

    assert _run(go()) == "one\ntwo\n"


def test_move_and_bare_name_and_string_dir_targets() -> None:
    async def go() -> object:
        src = AsyncPath("memasync://b/mv/a.txt")
        await src.write_bytes(b"1")
        # move() (rename alias) to a wrapped target
        moved = await src.move(AsyncPath("memasync://b/mv/b.txt"))
        # copy to a bare-name target (resolved against the parent)
        copied = await moved.copy("c.txt")
        # copy_into a string directory
        into = await moved.copy_into("/b/mv/sub")
        return (
            moved.name,
            copied.name,
            await AsyncPath("memasync://b/mv/c.txt").read_bytes(),
            into.name,
            await AsyncPath("memasync://b/mv/sub/b.txt").read_bytes(),
        )

    assert _run(go()) == ("b.txt", "c.txt", b"1", "b.txt", b"1")


def test_resolve_and_absolute() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/r/x.txt")
        r = await p.resolve()
        a = await p.absolute()
        return r == p, a == p

    assert _run(go()) == (True, True)


# -- restored surface (Fable B2) --------------------------------------------
def test_stat_touch_and_storage_options() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/s/f.txt")
        await p.touch()  # creates empty
        existed = await p.exists()
        await p.write_bytes(b"12345")
        st = await p.stat()
        return existed, st.st_size

    existed, size = _run(go())
    assert existed is True
    assert size == 5


def test_storage_options_accessor() -> None:
    p = AsyncPath("memasync://b/k", storage_options={"marker": "v"})
    assert p.storage_options == {"marker": "v"}


def test_lexical_relative_to_does_not_raise() -> None:
    # relative_to / is_relative_to are pure-path: they must never reach for a
    # filesystem, and must not raise UnsupportedPathOperation.
    p = AsyncPath("memasync://bucket/a/b/c.txt")
    base = AsyncPath("memasync://bucket/a")
    assert p.is_relative_to(base) is True
    assert str(p.relative_to(base)) in ("b/c.txt", "memasync://b/c.txt")
    assert p.as_uri() == "memasync://bucket/a/b/c.txt"


# -- open() correctness (Fable M1) ------------------------------------------
def test_open_plus_mode_is_refused() -> None:
    async def go() -> object:
        with pytest.raises(UnsupportedPathOperation):
            await AsyncPath("memasync://b/rw.txt").open("r+")
        return True

    assert _run(go()) is True


def test_open_exclusive_refuses_existing() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/excl.txt")
        await p.write_bytes(b"z")
        with pytest.raises(FileExistsError):
            await p.open("x")
        return True

    assert _run(go()) is True


def test_open_forwards_encoding() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/enc2.txt")
        async with await p.open("w", encoding="latin-1") as fh:
            await fh.write("café")
        async with await p.open("r", encoding="latin-1") as fh:
            return await fh.read()

    assert _run(go()) == "café"


# -- target handling (Fable M2/M3/M4) ---------------------------------------
def test_bare_name_rename_stays_in_same_directory() -> None:
    async def go() -> object:
        src = AsyncPath("memasync://b/dir/src.txt")
        await src.write_bytes(b"x")
        moved = await src.rename("dest.txt")  # bare name = same directory
        return str(moved), await AsyncPath("memasync://b/dir/dest.txt").exists()

    moved_str, exists = _run(go())
    assert moved_str == "memasync://b/dir/dest.txt"
    assert exists is True


def test_cross_scheme_target_is_refused() -> None:
    async def go() -> object:
        src = AsyncPath("memasync://b/x.txt")
        await src.write_bytes(b"x")
        for call in (
            src.copy("s3://other/y.txt"),
            src.rename("s3://other/y.txt"),
            src.copy_into("gs://other/dir"),
        ):
            with pytest.raises(ValueError):
                await call
        return True

    assert _run(go()) is True


# -- resolve / walk order ---------------------------------------------------
def test_resolve_collapses_dotdot() -> None:
    async def go() -> object:
        p = AsyncPath("memasync://b/w/sub/../other.txt")
        r = await p.resolve()
        return str(r)

    assert _run(go()) == "memasync://b/w/other.txt"


def test_resolve_strict_raises_on_missing() -> None:
    async def go() -> object:
        with pytest.raises(FileNotFoundError):
            await AsyncPath("memasync://b/nope.txt").resolve(strict=True)
        return True

    assert _run(go()) is True


def test_walk_bottom_up() -> None:
    async def go() -> object:
        await AsyncPath("memasync://b/wb/a.txt").write_bytes(b"z")
        await AsyncPath("memasync://b/wb/sub/b.txt").write_bytes(b"z")
        names = []
        async for path, _dirs, _files in AsyncPath(
            "memasync://b/wb"
        ).walk(top_down=False):
            names.append(path.name)
        # bottom-up: the child directory is visited before its parent
        return names.index("sub") < names.index("wb")

    assert _run(go()) is True


# -- misc guards ------------------------------------------------------------
def test_write_bytes_rejects_int() -> None:
    async def go() -> object:
        with pytest.raises(TypeError):
            await AsyncPath("memasync://b/i.txt").write_bytes(5)
        return True

    assert _run(go()) is True


def test_sweep_closed_loops_drops_dead_entries() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    _FS_BY_LOOP[loop] = {"marker": object()}
    _sweep_closed_loops()
    assert loop not in _FS_BY_LOOP


def test_stat_helpers() -> None:
    import datetime

    assert _as_timestamp(1234.5) == 1234.5
    assert _as_timestamp(None) == 0.0
    when = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    assert _as_timestamp(when) == when.timestamp()
    st = _stat_from_info({"size": 9, "type": "directory", "mtime": 100})
    assert st.st_size == 9
    assert st.st_mtime == 100


def test_with_segments_url_rebuilds_absolute() -> None:
    p = AsyncFSPath.from_url("memasync://b/x.txt", "memasync", {})
    rebuilt = p.with_segments("memasync://b/deep/y.txt")
    assert rebuilt.path == "/b/deep/y.txt"


def test_relative_to_string_and_negative() -> None:
    p = AsyncPath("memasync://bucket/a/b/c.txt")
    assert p.is_relative_to("memasync://bucket/a") is True
    assert p.is_relative_to("memasync://other/a") is False
    # a bare (scheme-less) string is taken as a relative path
    assert p.is_relative_to("not-a-prefix") is False


def test_mkdir_variants() -> None:
    async def go() -> object:
        d = AsyncPath("memasync://b/mk")
        await d.mkdir(parents=True)
        # exist_ok=True on an existing dir is a no-op
        await d.mkdir(exist_ok=True)
        # exist_ok=False on an existing dir raises
        raised = False
        try:
            await d.mkdir()
        except FileExistsError:
            raised = True
        # a non-recursive mkdir of a fresh child works
        await (d / "child").mkdir()
        return raised, await (d / "child").exists()

    assert _run(go()) == (True, True)


def test_copy_into_url_and_relative_dir() -> None:
    async def go() -> object:
        src = AsyncPath("memasync://b/ci/a.txt")
        await src.write_bytes(b"1")
        # a URL-string directory
        u = await src.copy_into("memasync://b/ci/urldst")
        # a relative directory (resolved against the parent)
        r = await src.copy_into("reldst")
        return (
            await AsyncPath("memasync://b/ci/urldst/a.txt").read_bytes(),
            u.name,
            await AsyncPath("memasync://b/ci/reldst/a.txt").read_bytes(),
            r.name,
        )

    assert _run(go()) == (b"1", "a.txt", b"1", "a.txt")


def test_freeze_distinguishes_equal_repr_objects() -> None:
    class _Creds:
        def __repr__(self) -> str:
            return "<Creds>"

    a, b = _Creds(), _Creds()
    # Equal reprs must NOT collide, or one caller gets another's filesystem.
    assert _freeze({"c": a}) != _freeze({"c": b})
    # A list and a tuple with equal contents are kept distinct.
    assert _freeze([1, 2]) != _freeze((1, 2))


def test_preferred_driver_beats_async_selection() -> None:
    from bagof.paths import register_protocol
    from bagof.paths._protocols import _PROTOCOLS

    class _Pref:
        def __init__(self, text: str) -> None:
            self.text = text

        def __fspath__(self) -> str:
            return self.text

    try:
        register_protocol("memasync", driver=_Pref)
        p = AsyncPath("memasync://b/k")
        assert isinstance(p.wrapped, _Pref)
    finally:
        _PROTOCOLS.pop("memasync", None)


# -- identity ---------------------------------------------------------------
def test_identity() -> None:
    a = AsyncPath("memasync://bucket/key")
    b = AsyncPath("memasync://bucket/key")
    assert a == b
    assert hash(a) == hash(b)
    assert a != AsyncPath("memasync://bucket/other")
    # A sync and async path never compare equal, even at the same URL.
    pytest.importorskip("upath")
    assert a != Path("memasync://bucket/key")


# -- members the store lacks ------------------------------------------------
def test_unsupported_member_raises() -> None:
    async def go() -> object:
        with pytest.raises(UnsupportedPathOperation):
            await AsyncPath("memasync://b/x").chmod(0o644)

    _run(go())


# -- per-loop filesystem lifecycle ------------------------------------------
def test_works_across_separate_event_loops() -> None:
    # Each asyncio.run builds a fresh loop; the driver resolves a filesystem
    # per running loop, so a path used in two runs is not bound to a dead one.
    p = AsyncPath("memasync://b/loop.txt")

    async def write() -> None:
        await p.write_bytes(b"v1")

    async def read() -> bytes:
        return await p.read_bytes()

    _run(write())
    assert _run(read()) == b"v1"


# -- unit: helpers ----------------------------------------------------------
def test_is_async_filesystem() -> None:
    assert is_async_filesystem("memasync") is True
    assert is_async_filesystem("memory") is False  # sync backend
    assert is_async_filesystem("no-such-scheme") is False


def test_freeze_is_hashable() -> None:
    # dict, nested list, a hashable leaf, and an unhashable leaf (a set, which
    # falls back to repr) all collapse to something hashable.
    token = _freeze({"a": 1, "nested": {"k": [1, 2]}, "unhashable": {1, 2}})
    assert isinstance(hash(token), int)
