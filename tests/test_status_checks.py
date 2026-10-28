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

import pytest
import typing_extensions as tx

from bagof.paths import AsyncPath, Path, UnsupportedPathOperation

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


# follow_symlinks reached pathlib's exists in 3.12 and is_file/is_dir in 3.13;
# before that the wrapper answers from lstat(), so this runs everywhere.
@pytest.mark.parametrize("name", SIMPLE)
def test_too_long_no_follow(tmp_path: pathlib.Path, name: str) -> None:
    p = Path(_too_long(tmp_path))
    assert getattr(p, name)(follow_symlinks=False) is False
    a = AsyncPath(_too_long(tmp_path))
    assert _run(getattr(a, name)(follow_symlinks=False)) is False


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


# -- follow_symlinks=False where the driver lacks the keyword ---------------
# pathlib gained the keyword on exists in 3.12 and on is_file/is_dir in
# 3.13. The wrapper answers it as the newest pathlib does on every version:
# from lstat() when the driver's method does not take it.
@pytest.fixture
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "f.txt").write_text("x")
    (tmp_path / "d").mkdir()
    try:
        (tmp_path / "lf").symlink_to(tmp_path / "f.txt")
        (tmp_path / "ld").symlink_to(tmp_path / "d", True)
        (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    except (OSError, NotImplementedError):
        pass  # no symlinks here; those cases are skipped
    return tmp_path


# name -> (exists, is_file, is_dir), all with follow_symlinks=False
NO_FOLLOW = {
    "f.txt": (True, True, False),
    "d": (True, False, True),
    "lf": (True, False, False),
    "ld": (True, False, False),
    "dangling": (True, False, False),
    "missing": (False, False, False),
    "f.txt/below": (False, False, False),
}


def _cases(
    tree: pathlib.Path,
) -> tx.Iterator[tx.Tuple[str, tx.Tuple[bool, ...]]]:
    for name, expected in NO_FOLLOW.items():
        link = name in ("lf", "ld", "dangling")
        if link and not (tree / name).is_symlink():
            continue
        yield str(tree / name), expected


class _OldPathlib(type(pathlib.Path())):  # type: ignore[misc]
    """A local stdlib driver whose checks predate follow_symlinks."""

    def exists(self) -> bool:  # type: ignore[override]
        return os.path.exists(str(self))

    def is_file(self) -> bool:  # type: ignore[override]
        return os.path.isfile(str(self))

    def is_dir(self) -> bool:  # type: ignore[override]
        return os.path.isdir(str(self))


class _NoLstat(os.PathLike):
    """A local driver with old-style checks and no lstat to fall back on."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __fspath__(self) -> str:
        return self._text

    def __str__(self) -> str:
        return self._text

    def exists(self) -> bool:
        return os.path.exists(self._text)

    def is_file(self) -> bool:
        return os.path.isfile(self._text)

    def is_dir(self) -> bool:
        return os.path.isdir(self._text)


class _OldAsyncNoLstat(os.PathLike):
    """A natively-async local driver whose checks predate the keyword and
    which has no lstat: the local stdlib view answers instead."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __fspath__(self) -> str:
        return self._text

    def __str__(self) -> str:
        return self._text

    async def exists(self) -> bool:
        return os.path.exists(self._text)

    async def is_file(self) -> bool:
        return os.path.isfile(self._text)

    async def is_dir(self) -> bool:
        return os.path.isdir(self._text)


class _OldAsync(_OldAsyncNoLstat):
    """The same, with the driver's own lstat to answer from."""

    async def lstat(self) -> os.stat_result:
        return os.lstat(self._text)


def _answers(p: tx.Any) -> tx.Tuple[bool, ...]:
    return tuple(getattr(p, name)(follow_symlinks=False) for name in SIMPLE)


async def _gather(p: tx.Any) -> tx.Tuple[bool, ...]:
    return tuple([await getattr(p, n)(follow_symlinks=False) for n in SIMPLE])


def _answers_async(p: tx.Any) -> tx.Tuple[bool, ...]:
    return _run(_gather(p))  # type: ignore[return-value]


def test_no_follow_real_pathlib(tree: pathlib.Path) -> None:
    for text, expected in _cases(tree):
        assert _answers(Path(text)) == expected, text
        assert _answers_async(AsyncPath(text)) == expected, text


def test_no_follow_fallback(tree: pathlib.Path) -> None:
    for text, expected in _cases(tree):
        assert _answers(Path(_OldPathlib(text))) == expected, text
        assert _answers_async(AsyncPath(_OldPathlib(text))) == expected, text


def test_no_follow_fallback_default_is_native(tree: pathlib.Path) -> None:
    p = Path(_OldPathlib(str(tree / "f.txt")))
    assert (p.exists(), p.is_file(), p.is_dir()) == (True, True, False)


def test_no_follow_fallback_too_long(tmp_path: pathlib.Path) -> None:
    long = _too_long(tmp_path)
    assert _answers(Path(_OldPathlib(long))) == (False, False, False)
    assert _answers_async(AsyncPath(_OldPathlib(long))) == (False,) * 3


def test_no_follow_fallback_needs_lstat(tmp_path: pathlib.Path) -> None:
    p = Path(_NoLstat(str(tmp_path)))
    assert p.is_dir() is True  # the default needs no fallback
    for name in SIMPLE:
        with pytest.raises(UnsupportedPathOperation) as info:
            getattr(p, name)(follow_symlinks=False)
        assert f"{name}(follow_symlinks=False)" in str(info.value)
        assert "_NoLstat" in str(info.value)
        with pytest.raises(UnsupportedPathOperation):
            _run(getattr(AsyncPath(_NoLstat(str(tmp_path))), name)(
                follow_symlinks=False
            ))


def test_no_follow_native_async(tree: pathlib.Path) -> None:
    for text, expected in _cases(tree):
        assert _answers_async(AsyncPath(_OldAsync(text))) == expected, text
        p = AsyncPath(_OldAsyncNoLstat(text))
        assert _answers_async(p) == expected, text
    long = AsyncPath(_OldAsync(_too_long(tree)))
    assert _answers_async(long) == (False, False, False)


def test_no_follow_anyio(tree: pathlib.Path) -> None:
    anyio = pytest.importorskip("anyio")
    for text, expected in _cases(tree):
        assert _answers_async(AsyncPath(anyio.Path(text))) == expected, text


def test_no_follow_keeps_a_real_type_error(tmp_path: pathlib.Path) -> None:
    # The keyword is read from the signature, so a TypeError raised inside
    # a driver that takes it is not mistaken for a missing keyword.
    class Broken(type(pathlib.Path())):  # type: ignore[misc]
        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            raise TypeError("inside the driver")

    with pytest.raises(TypeError, match="inside the driver"):
        Path(Broken(str(tmp_path))).is_file(follow_symlinks=False)


def test_no_follow_signature_tracks_the_method(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = Path(_OldPathlib(str(tmp_path)))
    assert p.is_dir(follow_symlinks=False) is True
    # A method that gains the keyword is called with it, not the fallback.
    monkeypatch.setattr(
        _OldPathlib, "is_dir", lambda self, follow_symlinks=True: "native"
    )
    assert p.is_dir(follow_symlinks=False) == "native"
