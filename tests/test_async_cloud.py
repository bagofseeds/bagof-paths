"""The native async cloud driver over an fsspec ``AsyncFileSystem``.

These tests do not need cloud credentials or a network: they register an
in-process, natively-async fsspec backend (``memasync``) backed by fsspec's
own in-memory filesystem, and drive ``AsyncPath`` against it end to end.
"""

import asyncio
import sys

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
    assert str(p.relative_to(base)) == "b/c.txt"
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
    # A sync and an async path at the same URL are equal (issue #22).
    pytest.importorskip("upath")
    assert a == Path("memasync://bucket/key")
    assert hash(a) == hash(Path("memasync://bucket/key"))


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


def test_native_driver_leaves_out_the_query() -> None:
    p = AsyncPath("memasync://b/dir/x.nii.gz?v=1")
    assert isinstance(p.wrapped, AsyncFSPath)
    assert str(p) == "memasync://b/dir/x.nii.gz?v=1"
    assert p.name == "x.nii.gz"
    assert p.suffixes == [".nii", ".gz"]
    assert str(p.parent) == "memasync://b/dir"
    assert isinstance(p.parent.wrapped, AsyncFSPath)


class _MemLiteralFS(_MemAsyncFS):
    """A natively-async backend whose keys hold "?" as a character."""

    protocol = "memliteral"

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        text = path.replace("memliteral://", "memory://")
        return MemoryFileSystem._strip_protocol(text)


def test_native_driver_honors_query_false() -> None:
    from bagof.paths import register_protocol

    fsspec.register_implementation("memliteral", _MemLiteralFS, clobber=True)
    register_protocol("memliteral", query=False)
    p = AsyncPath("memliteral://b/dir/x.nii.gz?v=1")
    assert isinstance(p.wrapped, AsyncFSPath)
    assert p.name == "x.nii.gz?v=1"


def test_native_driver_leaves_out_an_fsspec_version() -> None:
    # Built directly, so no s3fs is needed: the lexical members never reach
    # the backend.
    p = AsyncPath(AsyncFSPath("s3", "b/dir/x.nii.gz?versionId=1"))
    assert p.name == "x.nii.gz"
    assert str(p.parent) == "s3://b/dir"
    assert isinstance(p.parent.wrapped, AsyncFSPath)


class _MemVersionFS(_MemAsyncFS):
    """A backend that, like s3fs, reads version_aware out of a URL."""

    protocol = "memversion"

    @classmethod
    def _get_kwargs_from_urls(cls, path: str) -> dict:
        return {"version_aware": True} if "?versionId=" in path else {}


fsspec.register_implementation("memversion", _MemVersionFS, clobber=True)


def test_native_driver_reads_options_from_the_url() -> None:
    # fsspec.filesystem() does not apply _get_kwargs_from_urls by itself;
    # without it an s3 "?versionId=" URL would read the latest version, where
    # universal-pathlib reads the one named.
    p = AsyncPath("memversion://b/x?versionId=1")
    assert p.wrapped.storage_options == {"version_aware": True}
    assert AsyncPath("memversion://b/x").wrapped.storage_options == {}
    # The caller's options win over what the URL implies.
    q = AsyncPath(
        "memversion://b/x?versionId=1",
        storage_options={"version_aware": False},
    )
    assert q.wrapped.storage_options == {"version_aware": False}


# -- a backend whose filesystem path keeps the scheme (issue #32) -----------
# fsspec's HTTP filesystem keeps the whole URL as its path (it is natively
# async once aiohttp is installed). ``memurl`` stands in for it with no
# network: its async ``_strip_protocol`` keeps the scheme, and the in-memory
# store under it maps ``memurl://h/k`` to the key ``/h/k`` and back.
class _URLMemory(MemoryFileSystem):
    protocol = "memurl"

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        return MemoryFileSystem._strip_protocol(
            path.replace("memurl://", "memory://")
        )

    def ls(self, path: str, detail: bool = True, **kwargs: object) -> object:
        def url(name: str) -> str:
            return "memurl://" + name.lstrip("/")

        entries = super().ls(path, detail=detail, **kwargs)
        if detail:
            return [dict(e, name=url(e["name"])) for e in entries]
        return [url(e) for e in entries]


class _MemURLFS(AsyncFileSystemWrapper):
    """A natively-async filesystem whose path is the whole URL, like HTTP."""

    protocol = "memurl"
    async_impl = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        for drop in ("asynchronous", "skip_instance_cache"):
            kwargs.pop(drop, None)
        super().__init__(_URLMemory(), asynchronous=True)

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        return path


fsspec.register_implementation("memurl", _MemURLFS, clobber=True)


def test_scheme_keeping_backend_is_detected() -> None:
    from bagof.paths._async_fsspec import _keeps_scheme

    assert _keeps_scheme("memurl") is True
    assert _keeps_scheme("memasync") is False
    assert _keeps_scheme("no-such-scheme") is False


@pytest.mark.parametrize(
    "url, parts, name, parent",
    [
        ("memurl://h/dir/x.nii.gz", ("memurl://h/", "dir", "x.nii.gz"),
         "x.nii.gz", "memurl://h/dir"),
        ("memurl://u:p@h:8/a/b.txt", ("memurl://u:p@h:8/", "a", "b.txt"),
         "b.txt", "memurl://u:p@h:8/a"),
        # A trailing slash is an empty last part; the parent drops only it.
        ("memurl://h/a/b/", ("memurl://h/", "a", "b", ""), "",
         "memurl://h/a/b"),
        ("memurl://h/", ("memurl://h/", ""), "", "memurl://h/"),
    ],
)
def test_scheme_keeping_backend_lexical_members(
    url: str, parts: tuple, name: str, parent: str
) -> None:
    p = AsyncPath(url)
    assert isinstance(p.wrapped, AsyncFSPath)
    assert str(p) == url
    assert p.path == url
    assert p.parts == parts
    assert p.name == name
    assert str(p.parent) == parent


def test_scheme_keeping_backend_derivation() -> None:
    p = AsyncPath("memurl://h/dir/x.nii.gz")
    assert str(p / "y") == "memurl://h/dir/x.nii.gz/y"
    assert str(p.joinpath("a", "b.txt")) == "memurl://h/dir/x.nii.gz/a/b.txt"
    assert str(p.with_name("y.txt")) == "memurl://h/dir/y.txt"
    assert str(p.with_suffix(".json")) == "memurl://h/dir/x.nii.json"
    assert p.suffixes == [".nii", ".gz"]
    assert [str(a) for a in p.parents] == ["memurl://h/dir", "memurl://h/"]
    assert p.as_posix() == "memurl://h/dir/x.nii.gz"
    assert p.is_absolute()
    # A bare authority names its root, so both spellings are one path.
    assert AsyncPath("memurl://h").path == "memurl://h/"
    assert AsyncPath("memurl://h") == AsyncPath("memurl://h/")
    # A directory URL keeps its slash; a name replaces the empty last part.
    d = AsyncPath("memurl://h/a/")
    assert str(d.with_name("z")) == "memurl://h/a/z"
    assert [str(a) for a in d.parents] == ["memurl://h/a", "memurl://h/"]
    with pytest.raises(ValueError):
        d.with_suffix(".x")


def test_scheme_keeping_backend_query_and_relative_to() -> None:
    p = AsyncPath("memurl://h/dir/x.nii.gz?token=1")
    assert str(p) == "memurl://h/dir/x.nii.gz?token=1"
    assert p.name == "x.nii.gz"
    assert str(p.parent) == "memurl://h/dir"
    assert str(p.relative_to("memurl://h/dir?other=2")) == "x.nii.gz"
    assert str(p.relative_to(AsyncPath("memurl://h"))) == "dir/x.nii.gz"
    assert p.is_relative_to("memurl://h/dir")
    # Another authority is never a prefix, even with the same path.
    assert not p.is_relative_to("memurl://other/dir")
    with pytest.raises(ValueError):
        p.relative_to("memurl://other/dir")


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="walk_up needs Python 3.12"
)
def test_scheme_keeping_backend_relative_to_walk_up() -> None:
    p = AsyncPath("memurl://h/dir/x.txt")
    assert str(p.relative_to("memurl://h/other", walk_up=True)) == (
        "../dir/x.txt"
    )
    with pytest.raises(ValueError):
        p.relative_to("memurl://other/dir", walk_up=True)


def test_scheme_keeping_backend_io() -> None:
    async def go() -> object:
        p = AsyncPath("memurl://h/dir/a.txt")
        await p.write_text("hi")
        children = [str(c) async for c in p.parent.iterdir()]
        moved = await p.rename("b.txt")
        resolved = await AsyncPath("memurl://h/dir/../dir/b.txt").resolve()
        folder = await AsyncPath("memurl://h/x/../dir/").resolve()
        return (
            MemoryFileSystem.store.keys(), children, str(moved),
            await moved.read_text(), await p.exists(), str(resolved),
            str(folder),
        )

    keys, children, moved, text, old_exists, resolved, folder = _run(go())
    # The store is reached at the URL's own key, not a doubled scheme.
    assert "/h/dir/b.txt" in keys
    assert children == ["memurl://h/dir/a.txt"]
    assert moved == "memurl://h/dir/b.txt"
    assert text == "hi"
    assert old_exists is False
    assert resolved == "memurl://h/dir/b.txt"
    # resolve() collapses ".." but a directory URL keeps its slash.
    assert folder == "memurl://h/dir/"


# -- relative paths (issue #65) ---------------------------------------------
class _MemBucketFS(_MemAsyncFS):
    """A backend that strips ``scheme://b/k`` to ``b/k``, as s3 and gs do."""

    protocol = "membucket"

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        text = path.replace("membucket://", "memory://")
        return MemoryFileSystem._strip_protocol(text).lstrip("/")


fsspec.register_implementation("membucket", _MemBucketFS, clobber=True)


@pytest.mark.parametrize("scheme", ["memasync", "membucket"])
def test_relative_to_prints_without_the_scheme(scheme: str) -> None:
    p = AsyncPath(f"{scheme}://ds/sub-01/anat")
    r = p.relative_to(AsyncPath(f"{scheme}://ds"))
    assert isinstance(r.wrapped, AsyncFSPath)
    assert str(r) == "sub-01/anat"
    assert r.parts == ("sub-01", "anat")
    assert r.as_posix() == "sub-01/anat"
    assert r.protocol == scheme
    assert not r.is_absolute()
    assert not r.is_local()
    assert p.is_absolute()
    with pytest.raises(ValueError):
        r.as_uri()


@pytest.mark.parametrize("scheme", ["memasync", "membucket"])
def test_relative_path_derivations_stay_relative(scheme: str) -> None:
    r = AsyncPath(f"{scheme}://ds/sub-01/anat/t1.nii").relative_to(
        f"{scheme}://ds"
    )
    assert str(r / "x") == "sub-01/anat/t1.nii/x"
    assert str(r.joinpath("a", "b")) == "sub-01/anat/t1.nii/a/b"
    assert str(r.parent) == "sub-01/anat"
    assert [str(a) for a in r.parents] == ["sub-01/anat", "sub-01", "."]
    assert str(r.with_name("t2.nii")) == "sub-01/anat/t2.nii"
    assert str(r.with_stem("t2")) == "sub-01/anat/t2.nii"
    assert str(r.with_suffix(".gz")) == "sub-01/anat/t1.gz"
    for derived in (r / "x", r.parent, r.with_name("y"), r.with_suffix(".z")):
        assert not derived.is_absolute()
    # Joined onto an absolute path, it names a location again.
    joined = AsyncPath(f"{scheme}://other") / r
    assert joined == AsyncPath(f"{scheme}://other/sub-01/anat/t1.nii")
    assert joined.is_absolute()
    # A relative path is relative to a relative prefix only.
    assert str(r.relative_to("sub-01")) == "anat/t1.nii"
    assert r.is_relative_to(r.parent)
    assert not r.is_relative_to(f"{scheme}://ds")


@pytest.mark.parametrize("scheme", ["memasync", "membucket"])
def test_relative_path_identity(scheme: str) -> None:
    r = AsyncPath(f"{scheme}://ds/sub-01/anat").relative_to(f"{scheme}://ds")
    same = AsyncPath(f"{scheme}://x/sub-01/anat").relative_to(f"{scheme}://x")
    assert r == same
    assert hash(r) == hash(same)
    # Never the absolute path its parts spell, even where a key has no
    # leading slash (s3's `sub-01/anat` is the bucket `sub-01`).
    assert r != AsyncPath(f"{scheme}://sub-01/anat")
    assert r == AsyncPath(f"{scheme}://ds/sub-01/anat/t1").parent.relative_to(
        AsyncPath(f"{scheme}://ds")
    )


def test_relative_path_matches_the_sync_path() -> None:
    pytest.importorskip("upath")
    a = AsyncPath("memasync://ds/sub-01/anat").relative_to("memasync://ds")
    s = Path("memasync://ds/sub-01/anat").relative_to("memasync://ds")
    assert str(a) == str(s) == "sub-01/anat"
    assert a.parts == s.parts
    assert a.is_absolute() is s.is_absolute() is False
    assert a == s
    assert hash(a) == hash(s)
    assert s != Path("memasync://sub-01/anat")


def test_absolute_bucket_path_is_absolute() -> None:
    # A key stripped without a leading slash still names an absolute path.
    p = AsyncPath("membucket://b/k.txt")
    assert p.path == "b/k.txt"
    assert p.is_absolute()
    assert p.parent.is_absolute()
    assert str(p.parent) == "membucket://b"


def test_relative_path_refuses_io() -> None:
    r = AsyncPath("memasync://ds/a.txt").relative_to("memasync://ds")
    with pytest.raises(UnsupportedPathOperation, match="relative"):
        _run(r.read_bytes())
    with pytest.raises(UnsupportedPathOperation, match="relative"):
        _run(r.exists())


def test_relative_target_resolves_beside_the_source() -> None:
    async def go() -> object:
        src = AsyncPath("memasync://b/d/a.txt")
        await src.write_text("hi")
        rel = AsyncPath("memasync://x/c.txt").relative_to("memasync://x")
        moved = await src.rename(rel)
        return str(moved), await moved.read_text()

    # A relative target is a name beside the source, as a bare string is.
    assert _run(go()) == ("memasync://b/d/c.txt", "hi")


def test_scheme_keeping_backend_relative_path() -> None:
    r = AsyncPath("memurl://h/dir/x.txt").relative_to("memurl://h")
    assert isinstance(r.wrapped, AsyncFSPath)
    assert str(r) == "dir/x.txt"
    assert r.protocol == "memurl"
    assert not r.is_absolute()
    assert not r.is_local()
    assert str(r.parent) == "dir"
    assert AsyncPath("memurl://h/") / r == AsyncPath("memurl://h/dir/x.txt")


# -- walk from one listing per directory (#64) -------------------------------
def test_walk_lists_each_directory_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build() -> None:
        for d in range(3):
            for f in range(4):
                await AsyncPath(f"memasync://b/root/d{d}/f{f}.txt").write_bytes(
                    b"x"
                )

    _run(build())
    counted = ("ls", "isdir", "isfile", "info", "exists")
    counts = dict.fromkeys(counted, 0)
    for name in counted:
        original = getattr(MemoryFileSystem, name)

        def count(
            self: object, *args: object, _name: str = name,
            _original: object = original, **kwargs: object,
        ) -> object:
            counts[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(MemoryFileSystem, name, count)

    async def go() -> object:
        return {
            path.name: (sorted(dirs), sorted(files))
            async for path, dirs, files in AsyncPath("memasync://b/root").walk()
        }

    files = ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
    assert _run(go()) == {
        "root": (["d0", "d1", "d2"], []),
        "d0": ([], files), "d1": ([], files), "d2": ([], files),
    }
    # One listing per directory; no child is asked what it is.
    assert counts == {"ls": 4, "isdir": 0, "isfile": 0, "info": 0,
                      "exists": 0}


def test_iterdir_snapshot_stays_on_the_listed_child() -> None:
    async def go() -> object:
        await AsyncPath("memasync://b/snap/sub/f.txt").write_bytes(b"x")
        (child,) = [c async for c in AsyncPath("memasync://b/snap").iterdir()]
        listed = child.wrapped._snapshot.get("lstat", "dir")
        derived = [
            child / "f.txt", child.parent, child.with_name("other"),
            child.joinpath("x"),
        ]
        leaked = [d.wrapped._snapshot for d in derived]
        await child.rmdir(recursive=True)
        return listed, leaked, await child.is_dir(), await child.exists()

    listed, leaked, is_dir, exists = _run(go())
    assert listed is True
    assert leaked == [None, None, None, None]
    # The public status members ask the store, not the listing.
    assert (is_dir, exists) == (False, False)


# -- pathinfo on the native async driver (#64) --------------------------------
def _count_async_calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    counted = ("ls", "isdir", "isfile", "info", "exists", "glob", "find")
    counts = dict.fromkeys(counted, 0)
    for name in counted:
        original = getattr(MemoryFileSystem, name)

        def count(
            self: object, *args: object, _name: str = name,
            _original: object = original, **kwargs: object,
        ) -> object:
            counts[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(MemoryFileSystem, name, count)
    return counts


async def _build_info_tree() -> AsyncPath:
    base = AsyncPath("memasync://b/info")
    await (base / "sub" / "inner.txt").write_bytes(b"in")
    await (base / "scan.nii").write_bytes(b"0123")
    return base


async def _ask(path: AsyncPath) -> tuple:
    info = path.pathinfo
    return (
        await info.is_dir(), await info.is_file(), await info.exists(),
        await info.is_symlink(), await info.size(),
        (await info.mtime()) is not None,
    )


def test_pathinfo_on_listed_children_types_cost_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Counted from the start: the async wrapper binds the in-memory
    # filesystem's methods when the filesystem is built (once per loop).
    counts = _count_async_calls(monkeypatch)

    async def go() -> object:
        base = await _build_info_tree()
        listed = {
            "iterdir": [c async for c in base.iterdir()],
            "glob": [m async for m in base.glob("*.nii")],
            "rglob": [m async for m in base.rglob("*.txt")],
            "walk": [p async for p, _, _ in base.walk()][1:],
        }
        counts.update(dict.fromkeys(counts, 0))
        types, status = {}, {}
        for op, paths in listed.items():
            types[op] = sorted([
                (p.name, await p.pathinfo.is_dir(),
                 await p.pathinfo.is_file(), await p.pathinfo.is_symlink())
                for p in paths
            ])
        type_calls = {k: v for k, v in counts.items() if v}
        for op, paths in listed.items():
            status[op] = sorted([(p.name, await _ask(p)) for p in paths])
            await _ask(paths[0])  # kept: asked again, no call
        calls = {k: v for k, v in counts.items() if v}
        return types, type_calls, status, calls

    types, type_calls, status, calls = _run(go())
    assert types == {
        "iterdir": [
            ("scan.nii", False, True, False), ("sub", True, False, False),
        ],
        "glob": [("scan.nii", False, True, False)],
        "rglob": [("inner.txt", False, True, False)],
        "walk": [("sub", True, False, False)],
    }
    assert type_calls == {}  # what the listing said
    file4 = (False, True, True, False, 4, True)
    folder = (True, False, True, False, None, False)
    assert status == {
        "iterdir": [("scan.nii", file4), ("sub", folder)],
        "glob": [("scan.nii", file4)],
        "rglob": [("inner.txt", (False, True, True, False, 2, True))],
        "walk": [("sub", folder)],
    }
    # exists(), size() and mtime(): one fs._info per path, when first asked.
    assert calls == {"info": 5}


def test_pathinfo_on_an_unlisted_path_asks_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = _count_async_calls(monkeypatch)

    async def go() -> object:
        base = await _build_info_tree()
        scan = base / "scan.nii"
        counts.update(dict.fromkeys(counts, 0))
        first = await _ask(scan)
        await _ask(scan)
        calls = {k: v for k, v in counts.items() if v}
        await scan.unlink()
        kept = await scan.pathinfo.is_file()
        missing = await (base / "nope").pathinfo.exists()
        return first, calls, kept, await scan.exists(), missing

    first, calls, kept, live, missing = _run(go())
    assert first == (False, True, True, False, 4, True)
    # One fs._info for what the path leads to (is_dir, exists, size, ...)
    # and one for the path itself (is_symlink), as pathlib 3.14 reads its
    # stat and its lstat apart; each kept from then on.
    assert calls == {"info": 2}
    assert (kept, live, missing) == (True, False, False)


def test_pathinfo_stays_on_the_listed_child() -> None:
    async def go() -> object:
        base = await _build_info_tree()
        (sub,) = [c async for c in base.iterdir() if c.name == "sub"]
        derived = [
            sub / "inner.txt", sub.parent, sub.with_name("x"),
            await sub.absolute(), await sub.resolve(), AsyncPath(sub),
        ]
        return await sub.pathinfo.is_dir(), [d._facts() for d in derived]

    is_dir, leaked = _run(go())
    assert is_dir is True
    assert leaked == [None] * 6


# -- stores whose listings say less (#64) -------------------------------------
async def _small_tree(root: str) -> AsyncPath:
    base = AsyncPath(root)
    await (base / "sub" / "deeper" / "x.nii").write_bytes(b"xx")
    await (base / "sub" / "inner.txt").write_bytes(b"in")
    await (base / "scan.nii").write_bytes(b"0123")
    return base


@pytest.mark.parametrize("how", ["names-only", "lists-itself"])
def test_iterdir_over_a_listing_that_says_less(
    monkeypatch: pytest.MonkeyPatch, how: str,
) -> None:
    original = MemoryFileSystem.ls

    def ls(
        self: object, path: str, detail: bool = True, **kw: object
    ) -> object:
        if how == "names-only":  # a backend that ignores detail
            return original(self, path, detail=False, **kw)
        found = original(self, path, detail=detail, **kw)
        # an object store can list the directory's own marker key
        return [{"name": path.rstrip("/") + "/", "type": "directory",
                 "size": 0}] + list(found)

    base = _run(_small_tree(f"memasync://b/less-{how}"))
    # Patched before the next event loop builds its filesystem.
    monkeypatch.setattr(MemoryFileSystem, "ls", ls)

    async def go() -> object:
        children = {c.name: c async for c in base.iterdir()}
        return {
            name: (
                c.wrapped._snapshot is not None,
                await c.pathinfo.is_dir(), await c.pathinfo.is_file(),
                await c.pathinfo.size(),
            )
            for name, c in children.items()
        }

    listed = how != "names-only"
    assert _run(go()) == {
        "sub": (listed, True, False, None),
        "scan.nii": (listed, False, True, 4),
    }


@pytest.mark.parametrize("how", ["fails", "ignores-detail"])
def test_recursive_glob_without_a_useful_find(
    monkeypatch: pytest.MonkeyPatch, how: str,
) -> None:
    # Walked one listing per directory instead: the same matches.
    original = MemoryFileSystem.find
    asked = []

    def find(self: object, path: str, *args: object, **kw: object) -> object:
        asked.append(path)
        if how == "fails":
            raise PermissionError(path)
        return sorted(original(self, path, *args, **kw))

    base = _run(_small_tree(f"memasync://b/find-{how}"))
    monkeypatch.setattr(MemoryFileSystem, "find", find)

    async def go() -> object:
        found = [p async for p in base.rglob("*.nii")]
        return sorted(str(p) for p in found), [
            await p.pathinfo.is_file() for p in found
        ]

    names, files = _run(go())
    assert names == [
        f"memasync://b/find-{how}/scan.nii",
        f"memasync://b/find-{how}/sub/deeper/x.nii",
    ]
    assert files == [True, True]
    assert asked == [f"/b/find-{how}"]  # asked once, then walked


class _MemSyncWalkFS(_MemAsyncFS):
    """A backend whose `_walk` is a coroutine returning a synchronous
    generator (as fsspec's async wrapper once made it)."""

    protocol = "memsyncwalk"

    async def _walk(self, path: str, **kwargs: object) -> object:
        return self.sync_fs.walk(path, **kwargs)

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        text = path.replace("memsyncwalk://", "memory://")
        return MemoryFileSystem._strip_protocol(text)


def test_walk_over_a_synchronous_walk() -> None:
    fsspec.register_implementation("memsyncwalk", _MemSyncWalkFS, clobber=True)

    async def go() -> object:
        base = await _small_tree("memsyncwalk://b/syncwalk")
        assert isinstance(base.wrapped, AsyncFSPath)
        rows = [row async for row in base.walk()]
        return {
            top.name: (
                sorted(dirs), sorted(files), await top.pathinfo.is_dir()
            )
            for top, dirs, files in rows
        }, [top.wrapped._snapshot is not None for top, _, _ in rows]

    rows, listed = _run(go())
    assert rows == {
        "syncwalk": (["sub"], ["scan.nii"], True),
        "sub": (["deeper"], ["inner.txt"], True),
        "deeper": ([], ["x.nii"], True),
    }
    # The start was not listed; each directory below holds its listing.
    assert listed == [False, True, True]
