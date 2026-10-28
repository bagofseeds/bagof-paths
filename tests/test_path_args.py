"""Path arguments are read as pathlib reads them (issues #30 and #31).

A method that takes a path accepts a ``str``, any ``os.PathLike``, a
``pathlib`` path, a driver object or one of our wrappers; ``bytes`` is
refused with ``TypeError`` before it reaches a driver. Each case runs on a
local path, an in-memory universal-pathlib path and the dependency-free
lexical driver, with the async counterparts where the member is I/O.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid

import pytest
import typing_extensions as tx

from bagof.paths import AsyncPath, Path, PurePath, UnsupportedPathOperation
from bagof.paths._async_fsspec import AsyncFSPath
from bagof.paths._pure_driver import PureCloudPath


class Fspath:
    """A minimal os.PathLike: only ``__fspath__``."""

    def __init__(self, path: tx.Any) -> None:
        self._path = path

    def __fspath__(self) -> tx.Any:
        return self._path


def _run(coro: tx.Any) -> tx.Any:
    return asyncio.run(coro)


def _memory(key: str = "") -> str:
    """A fresh in-memory bucket URL, so tests never share keys."""
    pytest.importorskip("upath")
    return f"memory://args-{uuid.uuid4().hex}{key}"


def _pure_fs(url: str) -> AsyncFSPath:
    """The native async driver built lexically, with no backend needed."""
    scheme, rest = url.split("://", 1)
    return AsyncFSPath(scheme, rest)


# -- #31: bytes are refused on every front-end and driver -------------------
def _local_path() -> Path:
    return Path("/data/x.txt")


def _memory_path() -> Path:
    return Path(_memory("/x.txt"))


def _pure_local() -> PurePath:
    return PurePath("/data/x.txt")


def _pure_remote() -> PurePath:
    return PurePath("s3://b/x.txt")


def _async_local() -> AsyncPath:
    return AsyncPath("/data/x.txt")


def _async_memory() -> AsyncPath:
    return AsyncPath(_memory("/x.txt"))


def _async_native() -> AsyncPath:
    return AsyncPath(_pure_fs("s3://b/x.txt"))


FRONT_ENDS = [
    _local_path, _memory_path, _pure_local, _pure_remote,
    _async_local, _async_memory, _async_native,
]

LEXICAL_OPS = {
    "truediv": lambda p, a: p / a,
    "rtruediv": lambda p, a: a / p,
    "joinpath": lambda p, a: p.joinpath("d", a),
    "with_segments": lambda p, a: p.with_segments(a),
    "relative_to": lambda p, a: p.relative_to(a),
    "is_relative_to": lambda p, a: p.is_relative_to(a),
    "match": lambda p, a: p.match(a),
    "full_match": lambda p, a: p.full_match(a),
}


@pytest.mark.parametrize("make", FRONT_ENDS)
@pytest.mark.parametrize("op", sorted(LEXICAL_OPS))
@pytest.mark.parametrize("arg", [b"x", Fspath(b"x")], ids=["bytes", "fsb"])
def test_bytes_path_argument_is_refused(
    make: tx.Callable[[], tx.Any], op: str, arg: tx.Any
) -> None:
    path = make()
    with pytest.raises(TypeError):
        LEXICAL_OPS[op](path, arg)


@pytest.mark.parametrize(
    "driver",
    [
        PureCloudPath.from_url("s3://b/x.txt", "s3"),
        _pure_fs("s3://b/x.txt"),
    ],
    ids=["pure", "native-async"],
)
def test_bytes_refused_by_our_own_drivers(driver: tx.Any) -> None:
    for call in (
        lambda: driver / b"x",
        lambda: driver.joinpath(b"x"),
        lambda: driver.with_segments(b"s3://b/x"),
        lambda: driver.relative_to(b"s3://b"),
    ):
        with pytest.raises(TypeError):
            call()


def test_bytes_io_target_is_refused_locally(tmp_path: pathlib.Path) -> None:
    src = Path(tmp_path / "a.txt")
    src.write_text("x")
    target = os.fsencode(str(tmp_path / "b.txt"))
    for call in (
        lambda: src.copy(target),
        lambda: src.copy_into(os.fsencode(str(tmp_path))),
        lambda: src.move(target),
        lambda: src.move_into(os.fsencode(str(tmp_path))),
        lambda: src.rename(target),
        lambda: src.replace(target),
        lambda: src.samefile(target),
        lambda: Path(tmp_path / "l").symlink_to(target),
        lambda: Path(tmp_path / "h").hardlink_to(target),
    ):
        with pytest.raises(TypeError):
            call()
    assert src.read_text() == "x"  # nothing moved


def test_bytes_io_target_is_refused_remotely() -> None:
    src = Path(_memory("/a.txt"))
    src.write_text("x")
    for call in (
        lambda: src.copy(b"b.txt"),
        lambda: src.copy_into(b"d"),
        lambda: src.move(b"b.txt"),
        lambda: src.rename(b"b.txt"),
    ):
        with pytest.raises(TypeError):
            call()
    assert src.read_text() == "x"


def test_bytes_io_target_is_refused_async(tmp_path: pathlib.Path) -> None:
    src = AsyncPath(tmp_path / "a.txt")
    _run(src.write_text("x"))
    target = os.fsencode(str(tmp_path / "b.txt"))
    for coro in (src.copy, src.move, src.rename, src.samefile):
        with pytest.raises(TypeError):
            _run(coro(target))
    assert _run(src.read_text()) == "x"


def test_write_bytes_data_is_not_a_path(tmp_path: pathlib.Path) -> None:
    # The refusal is for path arguments only; data stays bytes.
    p = Path(tmp_path / "a.bin")
    p.write_bytes(b"x")
    assert p.read_bytes() == b"x"


@pytest.mark.parametrize("cls", [Path, PurePath, AsyncPath])
def test_bytes_of_a_local_path_is_its_encoded_name(cls: type) -> None:
    assert bytes(cls("/data/x.txt")) == b"/data/x.txt"
    assert bytes(cls("/data/x.txt")) == bytes(pathlib.Path("/data/x.txt"))


def test_bytes_of_a_remote_path_is_unsupported() -> None:
    with pytest.raises(UnsupportedPathOperation):
        bytes(PurePath("s3://b/x.txt"))
    with pytest.raises(UnsupportedPathOperation):
        bytes(Path(_memory("/x.txt")))
    with pytest.raises(UnsupportedPathOperation):
        bytes(AsyncPath(_pure_fs("s3://b/x.txt")))


KINDS = ["str", "pathlib", "ours", "fs"]


# -- #30 row 1: full_match()/match() take a path object as the pattern -----
def _patterns(text: str) -> tx.List[tx.Any]:
    return [
        text, pathlib.PurePosixPath(text), PurePath(text), Fspath(text),
    ]


@pytest.mark.parametrize("kind", range(4), ids=KINDS)
@pytest.mark.parametrize(
    "make",
    [
        lambda: Path("/d/x.txt"),
        lambda: Path(_memory("/d/x.txt")),
        lambda: PurePath("s3://b/d/x.txt"),
        lambda: AsyncPath(_pure_fs("s3://b/d/x.txt")),
    ],
    ids=["local", "upath", "pure", "native-async"],
)
def test_match_pattern_may_be_a_path(
    make: tx.Callable[[], tx.Any], kind: int
) -> None:
    path = make()
    assert path.full_match(_patterns("**/*.txt")[kind])
    assert not path.full_match(_patterns("**/*.csv")[kind])
    assert path.match(_patterns("*.txt")[kind])
    assert path.match(_patterns("*.TXT")[kind], case_sensitive=False)


def test_full_match_agrees_with_pathlib() -> None:
    pattern = pathlib.PurePosixPath("**/*.txt")
    expected = pathlib.PurePosixPath("/d/x.txt").full_match(pattern) if (
        hasattr(pathlib.PurePath, "full_match")
    ) else True
    assert Path("/d/x.txt").full_match(pattern) is expected


# -- #30 row 2: copy_into()/move_into() take a plain os.PathLike -----------
def _dirs(path: pathlib.Path) -> tx.List[tx.Any]:
    return [str(path), path, Path(path), Fspath(str(path))]


@pytest.mark.parametrize("kind", range(4), ids=KINDS)
def test_into_takes_any_path_like_locally(
    tmp_path: pathlib.Path, kind: int
) -> None:
    src = Path(tmp_path / "a.txt")
    src.write_text("x")
    (tmp_path / "c").mkdir()
    (tmp_path / "m").mkdir()
    copied = src.copy_into(_dirs(tmp_path / "c")[kind])
    assert copied == Path(tmp_path / "c" / "a.txt")
    assert copied.read_text() == "x"
    moved = src.move_into(_dirs(tmp_path / "m")[kind])
    assert moved == Path(tmp_path / "m" / "a.txt")
    assert moved.read_text() == "x"
    assert not src.exists()


@pytest.mark.parametrize("kind", ["str", "ours", "fs", "driver"])
def test_into_takes_any_path_like_remotely(kind: str) -> None:
    base = _memory()
    src = Path(base + "/a.txt")
    src.write_text("x")

    def as_kind(url: str) -> tx.Any:
        return {
            "str": url,
            "ours": Path(url),
            "fs": Fspath(url),
            "driver": Path(url).wrapped,
        }[kind]

    copied = src.copy_into(as_kind(base + "/c"))
    assert copied == Path(base + "/c/a.txt")
    assert copied.read_text() == "x"
    moved = src.move_into(as_kind(base + "/m"))
    assert moved == Path(base + "/m/a.txt")
    assert moved.read_text() == "x"


@pytest.mark.parametrize("kind", range(4), ids=KINDS)
def test_into_takes_any_path_like_async(
    tmp_path: pathlib.Path, kind: int
) -> None:
    src = AsyncPath(tmp_path / "a.txt")
    _run(src.write_text("x"))
    (tmp_path / "c").mkdir()
    (tmp_path / "m").mkdir()
    copied = _run(src.copy_into(_dirs(tmp_path / "c")[kind]))
    assert copied == AsyncPath(tmp_path / "c" / "a.txt")
    moved = _run(src.move_into(_dirs(tmp_path / "m")[kind]))
    assert moved == AsyncPath(tmp_path / "m" / "a.txt")
    assert _run(moved.read_text()) == "x"


# -- #30 row 3: copy() to a plain os.PathLike returns a usable path --------
def test_copy_to_a_plain_path_like_remotely() -> None:
    base = _memory()
    src = Path(base + "/a.txt")
    src.write_text("x")
    for name, target in [
        ("b.txt", Fspath(base + "/b.txt")),
        ("c.txt", base + "/c.txt"),
        ("d.txt", Path(base + "/d.txt")),
        ("e.txt", Path(base + "/e.txt").wrapped),
    ]:
        copied = src.copy(target)
        assert copied == Path(base + "/" + name)
        assert not isinstance(copied.wrapped, Fspath)
        assert copied.read_text() == "x"


def test_copy_and_move_to_a_plain_path_like_locally(
    tmp_path: pathlib.Path,
) -> None:
    src = Path(tmp_path / "a.txt")
    src.write_text("x")
    copied = src.copy(Fspath(str(tmp_path / "b.txt")))
    assert isinstance(copied.wrapped, pathlib.Path)
    assert copied.read_text() == "x"
    moved = copied.move(Fspath(str(tmp_path / "c.txt")))
    assert isinstance(moved.wrapped, pathlib.Path)
    assert moved.read_text() == "x"
    renamed = moved.rename(Fspath(str(tmp_path / "d.txt")))
    assert renamed == Path(tmp_path / "d.txt")
    assert renamed.samefile(Fspath(str(tmp_path / "d.txt")))


def test_copy_to_a_plain_path_like_async() -> None:
    base = _memory()
    src = AsyncPath(base + "/a.txt")
    _run(src.write_text("x"))
    copied = _run(src.copy(Fspath(base + "/b.txt")))
    assert copied == AsyncPath(base + "/b.txt")
    assert _run(copied.read_text()) == "x"


# -- #30 row 4: our drivers read path-like segments with os.fspath ---------
@pytest.mark.parametrize(
    "segment",
    [
        "d", pathlib.PurePosixPath("d"), PurePath("d"), Fspath("d"),
    ],
    ids=["str", "pathlib", "ours", "fs"],
)
@pytest.mark.parametrize(
    "make",
    [
        lambda: PurePath("s3://b"),
        lambda: Path(_memory()),
        lambda: AsyncPath(_pure_fs("s3://b")),
    ],
    ids=["pure", "upath", "native-async"],
)
def test_path_like_segment_joins_by_its_name(
    make: tx.Callable[[], tx.Any], segment: tx.Any
) -> None:
    base = make()
    assert base / segment == base / "d"
    assert str(base / segment).endswith("/d")
    assert base.joinpath(segment, "e") == base / "d" / "e"


@pytest.mark.parametrize(
    "driver",
    [
        PureCloudPath.from_url("s3://b", "s3"),
        _pure_fs("s3://b"),
    ],
    ids=["pure", "native-async"],
)
def test_our_drivers_read_path_like_segments(driver: tx.Any) -> None:
    assert str(driver / Fspath("d")) == "s3://b/d"
    assert str(driver.joinpath(pathlib.PurePosixPath("d"), Fspath("e"))) == (
        "s3://b/d/e"
    )
    assert str(driver.with_segments(Fspath("s3://c/d"))) == "s3://c/d"
    child = driver / "d" / "x.txt"
    assert str(child.relative_to(Fspath("s3://b/d"))).endswith("x.txt")
    with pytest.raises(TypeError):
        driver / 3


def test_pure_with_segments_takes_a_path_like() -> None:
    assert PurePath("s3://b").with_segments(Fspath("s3://c/d")) == PurePath(
        "s3://c/d"
    )


# -- #30 row 5: relative_to() drops the query of a driver argument --------
def test_relative_to_a_driver_with_a_query_upath() -> None:
    upath = pytest.importorskip("upath")
    base = _memory()
    path = Path(base + "/a.txt")
    for other in (
        base + "?q=1",
        Path(base + "?q=1"),
        upath.UPath(base + "?q=1"),
        Fspath(base + "?q=1"),
    ):
        assert str(path.relative_to(other)) == "a.txt"
        assert path.is_relative_to(other)


def test_relative_to_a_driver_with_a_query_pure() -> None:
    path = PurePath("s3://b/a.txt")
    for other in (
        "s3://b?q=1",
        PurePath("s3://b?q=1"),
        PureCloudPath.from_url("s3://b?q=1", "s3"),
        Fspath("s3://b?q=1"),
    ):
        assert str(path.relative_to(other)) == "a.txt"
        assert path.is_relative_to(other)


def test_relative_to_a_local_path_like() -> None:
    path = Path("/d/x?.txt")
    for other in ("/d", pathlib.Path("/d"), Path("/d"), Fspath("/d")):
        assert path.relative_to(other) == Path("x?.txt")
        assert path.is_relative_to(other)


# -- #30 row 6: a URL segment starts the path over ------------------------
def test_url_segment_replaces_upath() -> None:
    base = _memory()
    path = Path(base)
    assert path / (base + "/d") == Path(base + "/d")
    assert path / Path(base + "/d") == Path(base + "/d")
    assert path.joinpath("x", base + "/d", "e") == Path(base + "/d/e")
    assert path / Fspath(base + "/d") == Path(base + "/d")
    with pytest.raises(ValueError):
        path / "s3://b/d"


@pytest.mark.parametrize(
    "make", [PurePath, lambda url: AsyncPath(_pure_fs(url))],
    ids=["pure", "native-async"],
)
def test_url_segment_replaces_our_drivers(make: tx.Callable) -> None:
    path = make("s3://b/x")
    assert str(path / "s3://c/d") == "s3://c/d"
    assert str(path / make("s3://c/d")) == "s3://c/d"
    assert str(path.joinpath("y", "s3://c/d", "e")) == "s3://c/d/e"
    with pytest.raises(ValueError):
        path / "gs://c/d"


def test_url_segment_replaces_async_upath() -> None:
    base = _memory()
    assert AsyncPath(base) / (base + "/d") == AsyncPath(base + "/d")


def test_url_segment_keeps_the_configuration() -> None:
    upath = pytest.importorskip("upath")
    base = _memory()
    path = Path(upath.UPath(base, extra_option=1))
    joined = path / (base + "/d")
    assert joined.storage_options == path.storage_options


def test_url_segment_on_a_local_path_is_a_name_as_in_pathlib() -> None:
    expected = pathlib.Path("/tmp") / "s3://b/d"
    assert Path("/tmp") / "s3://b/d" == Path(expected)


# -- the native async driver's I/O path arguments --------------------------
@pytest.fixture
def native_memory() -> tx.Iterator[str]:
    """A natively-async in-memory backend, so AsyncPath gets AsyncFSPath."""
    fsspec = pytest.importorskip("fsspec")
    wrapper = pytest.importorskip("fsspec.implementations.asyn_wrapper")
    from fsspec.implementations.memory import MemoryFileSystem

    class _MemArgs(wrapper.AsyncFileSystemWrapper):
        protocol = "memargs"
        async_impl = True

        def __init__(self, *args: tx.Any, **kwargs: tx.Any) -> None:
            super().__init__(MemoryFileSystem(), asynchronous=True)

        @classmethod
        def _strip_protocol(cls, path: str) -> str:
            text = path.replace("memargs://", "memory://")
            return MemoryFileSystem._strip_protocol(text)

    fsspec.register_implementation("memargs", _MemArgs, clobber=True)
    yield f"memargs://args-{uuid.uuid4().hex}"


def test_native_async_io_takes_any_path_like(native_memory: str) -> None:
    base = native_memory
    src = AsyncPath(base + "/a.txt")
    assert isinstance(src.wrapped, AsyncFSPath)
    _run(src.write_text("x"))
    copied = _run(src.copy(Fspath(base + "/b.txt")))
    assert copied == AsyncPath(base + "/b.txt")
    assert _run(copied.read_text()) == "x"
    into = _run(src.copy_into(Fspath(base + "/d")))
    assert into == AsyncPath(base + "/d/a.txt")
    moved = _run(copied.move_into(AsyncPath(base + "/m")))
    assert moved == AsyncPath(base + "/m/b.txt")
    for coro in (src.copy, src.copy_into, src.move, src.rename):
        with pytest.raises(TypeError):
            _run(coro(b"memargs://x/y"))
    assert _run(src.read_text()) == "x"
