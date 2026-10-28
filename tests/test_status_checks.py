"""Status checks on a local path follow Python 3.14 on every version (#24).

`exists`, `is_file`, `is_dir` and the other `is_*` checks return False
rather than raise an `OSError` or `ValueError` on a local path: a name too
long, a parent that cannot be searched, an embedded NUL. `stat()` still
raises. A remote driver's checks are its own.
"""

import asyncio
import errno
import os
import pathlib
import sys

import pytest

from bagof.paths import AsyncPath, Path

CHECKS = (
    "exists", "is_file", "is_dir", "is_symlink", "is_mount", "is_socket",
    "is_fifo", "is_block_device", "is_char_device", "is_junction",
)
SIMPLE = ("exists", "is_file", "is_dir")


def _run(coro: object) -> object:
    return asyncio.run(coro)


def _too_long(tmp_path: pathlib.Path) -> str:
    return os.path.join(str(tmp_path), "a" * 300)


@pytest.fixture
def locked(tmp_path: pathlib.Path) -> object:
    """A file inside a directory that may not be searched."""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs POSIX permissions and a non-root user")
    parent = tmp_path / "locked"
    parent.mkdir()
    (parent / "f.txt").write_text("x")
    parent.chmod(0)
    yield str(parent / "f.txt")
    parent.chmod(0o700)


# -- the name too long ------------------------------------------------------
@pytest.mark.parametrize("name", CHECKS)
def test_too_long_is_false(tmp_path: pathlib.Path, name: str) -> None:
    assert getattr(Path(_too_long(tmp_path)), name)() is False


@pytest.mark.parametrize("name", CHECKS)
def test_too_long_is_false_async(tmp_path: pathlib.Path, name: str) -> None:
    p = AsyncPath(_too_long(tmp_path))
    assert _run(getattr(p, name)()) is False


def test_too_long_stat_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(OSError) as info:
        Path(_too_long(tmp_path)).stat()
    assert info.value.errno == errno.ENAMETOOLONG
    with pytest.raises(OSError):
        _run(AsyncPath(_too_long(tmp_path)).stat())


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="follow_symlinks is 3.12+ pathlib"
)
@pytest.mark.parametrize("name", SIMPLE)
def test_too_long_no_follow(tmp_path: pathlib.Path, name: str) -> None:
    p = Path(_too_long(tmp_path))
    assert getattr(p, name)(follow_symlinks=False) is False


# -- a parent that may not be searched --------------------------------------
@pytest.mark.parametrize("name", SIMPLE)
def test_permission_denied_is_false(locked: str, name: str) -> None:
    assert getattr(Path(locked), name)() is False
    assert _run(getattr(AsyncPath(locked), name)()) is False


def test_permission_denied_stat_raises(locked: str) -> None:
    with pytest.raises(PermissionError):
        Path(locked).stat()
    with pytest.raises(PermissionError):
        _run(AsyncPath(locked).stat())


# -- an embedded NUL --------------------------------------------------------
@pytest.mark.parametrize("name", CHECKS)
def test_embedded_nul_is_false(tmp_path: pathlib.Path, name: str) -> None:
    text = os.path.join(str(tmp_path), "bad\0name")
    assert getattr(Path(text), name)() is False
    assert _run(getattr(AsyncPath(text), name)()) is False


def test_embedded_nul_stat_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError):
        Path(os.path.join(str(tmp_path), "bad\0name")).stat()


# -- the guard itself, on every interpreter ---------------------------------
class _Raising(type(pathlib.Path())):  # type: ignore[misc]
    """A local stdlib driver whose checks raise, as pathlib < 3.14 does."""

    def exists(self, *args: object, **kwargs: object) -> bool:
        raise OSError(errno.EACCES, "denied")

    def is_file(self, *args: object, **kwargs: object) -> bool:
        raise ValueError("embedded null byte")


def test_guard_turns_error_into_false(tmp_path: pathlib.Path) -> None:
    p = Path(_Raising(str(tmp_path)))
    assert p.exists() is False
    assert p.is_file() is False
    assert _run(AsyncPath(_Raising(str(tmp_path))).exists()) is False


def test_guard_leaves_other_members_alone(tmp_path: pathlib.Path) -> None:
    p = Path(_Raising(str(tmp_path / "missing")))
    with pytest.raises(FileNotFoundError):
        p.stat()


class _RaisingAsync(os.PathLike):
    """A natively-async local driver whose checks raise."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __fspath__(self) -> str:
        return self._text

    def __str__(self) -> str:
        return self._text

    async def exists(self) -> bool:
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    async def stat(self) -> os.stat_result:
        raise OSError(errno.ENAMETOOLONG, "File name too long")


def test_native_async_guard(tmp_path: pathlib.Path) -> None:
    p = AsyncPath(_RaisingAsync(_too_long(tmp_path)))
    assert _run(p.exists()) is False
    with pytest.raises(OSError):
        _run(p.stat())


def test_anyio_driver(tmp_path: pathlib.Path) -> None:
    anyio = pytest.importorskip("anyio")
    p = AsyncPath(anyio.Path(_too_long(tmp_path)))
    assert _run(p.exists()) is False
    assert _run(p.is_dir()) is False
    with pytest.raises(OSError):
        _run(p.stat())


# -- a remote driver is unchanged -------------------------------------------
def test_remote_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("upath")
    p = Path("memory://bagofpaths-status/a.txt")
    assert p.exists() is False

    def boom(self: object, **kwargs: object) -> bool:
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(type(p.wrapped), "exists", boom)
    with pytest.raises(OSError):
        p.exists()
    with pytest.raises(OSError):
        _run(AsyncPath("memory://bagofpaths-status/a.txt").exists())
