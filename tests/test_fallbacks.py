"""Synthesized fallbacks: read/write built from a driver that has only open."""

import io
import os
import pathlib

import pytest

from bagof.paths import Path, UnsupportedPathOperation


class OnlyOpen(os.PathLike):
    """A driver that implements the open() primitive and nothing else.

    read_bytes/read_text/write_bytes/write_text must be synthesized.
    """

    def __init__(self, real: pathlib.Path) -> None:
        self._real = pathlib.Path(real)

    def __fspath__(self) -> str:
        return os.fspath(self._real)

    def __str__(self) -> str:
        return str(self._real)

    def open(self, mode: str = "r", **kwargs: object) -> object:
        return self._real.open(mode, **kwargs)


class NoIO(os.PathLike):
    """A driver with no I/O primitive at all."""

    def __fspath__(self) -> str:
        return "/x"

    def __str__(self) -> str:
        return "/x"


def test_bytes_synthesized_from_open(tmp_path: pathlib.Path) -> None:
    f = Path(OnlyOpen(tmp_path / "a.bin"))
    assert f.write_bytes(b"data") == 4
    assert f.read_bytes() == b"data"


def test_text_synthesized_from_bytes(tmp_path: pathlib.Path) -> None:
    f = Path(OnlyOpen(tmp_path / "a.txt"))
    f.write_text("héllo")
    assert f.read_text() == "héllo"


def test_supports_accounts_for_fallbacks(tmp_path: pathlib.Path) -> None:
    f = Path(OnlyOpen(tmp_path / "a"))
    assert f.supports("open") is True
    assert f.supports("read_bytes") is True
    assert f.supports("read_text") is True
    assert f.supports("write_bytes") is True
    assert f.supports("write_text") is True


def test_missing_primitive_is_unsupported() -> None:
    f = Path(NoIO())
    assert f.supports("read_bytes") is False
    assert f.supports("read_text") is False
    with pytest.raises(UnsupportedPathOperation):
        f.read_bytes()
    with pytest.raises(UnsupportedPathOperation):
        f.read_text()


class LexicalOnly(os.PathLike):
    """A driver missing with_stem / is_relative_to, like pathlib on 3.8.

    It exposes only the primitives those two members are synthesized from.
    """

    def __init__(self, p: object) -> None:
        self._p = pathlib.PurePosixPath(p)

    def __fspath__(self) -> str:
        return str(self._p)

    def __str__(self) -> str:
        return str(self._p)

    @property
    def suffix(self) -> str:
        return self._p.suffix

    def with_name(self, name: str) -> "LexicalOnly":
        return LexicalOnly(self._p.with_name(name))

    def relative_to(self, other: object) -> object:
        return self._p.relative_to(other)


def test_with_stem_synthesized_from_with_name() -> None:
    p = Path(LexicalOnly("/a/b/c.txt"))
    assert p.supports("with_stem") is True
    assert str(p.with_stem("z")) == "/a/b/z.txt"
    assert isinstance(p.with_stem("z"), Path)


def test_is_relative_to_synthesized_from_relative_to() -> None:
    p = Path(LexicalOnly("/a/b/c"))
    assert p.supports("is_relative_to") is True
    assert p.is_relative_to("/a") is True
    assert p.is_relative_to("/x") is False


def test_read_text_fallback_matches_native_on_crlf(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "n.txt").write_bytes(b"a\r\nb\rc\nd")
    native = Path(tmp_path / "n.txt").read_text()
    fallback = Path(OnlyOpen(tmp_path / "n.txt")).read_text()
    # universal-newline translation applied identically -> same result
    assert fallback == native == "a\nb\nc\nd"


def test_write_text_fallback_matches_native(tmp_path: pathlib.Path) -> None:
    text = "x\ny\nz"
    Path(tmp_path / "native.txt").write_text(text)
    Path(OnlyOpen(tmp_path / "fb.txt")).write_text(text)
    assert (tmp_path / "fb.txt").read_bytes() == (
        tmp_path / "native.txt"
    ).read_bytes()


class SpyOpen(os.PathLike):
    """Records the kwargs open() is actually called with."""

    def __init__(self) -> None:
        self.calls: list = []

    def __fspath__(self) -> str:
        return "/x"

    def __str__(self) -> str:
        return "/x"

    def open(self, mode: str = "r", **kwargs: object) -> object:
        self.calls.append((mode, dict(kwargs)))
        return io.BytesIO(b"")


def test_default_kwargs_are_not_forwarded() -> None:
    spy = SpyOpen()
    Path(spy).open("rb")
    # buffering/encoding/errors/newline are all at their defaults -> dropped
    assert spy.calls == [("rb", {})]


class NarrowMkdir(os.PathLike):
    """A driver whose mkdir has no mode parameter (cloudpathlib-shaped)."""

    def __init__(self, real: pathlib.Path) -> None:
        self._real = pathlib.Path(real)

    def __fspath__(self) -> str:
        return os.fspath(self._real)

    def __str__(self) -> str:
        return str(self._real)

    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> None:
        self._real.mkdir(parents=parents, exist_ok=exist_ok)


def test_mkdir_default_mode_not_forwarded(tmp_path: pathlib.Path) -> None:
    d = tmp_path / "sub"
    Path(NarrowMkdir(d)).mkdir()  # default mode dropped -> no TypeError
    assert d.is_dir()


def test_every_fallback_name_is_registered() -> None:
    from bagof.paths._fallbacks import FALLBACKS
    from bagof.paths._spec import MEMBERS

    for member in MEMBERS:
        if member.fallback is not None:
            assert member.fallback in FALLBACKS, member.name
