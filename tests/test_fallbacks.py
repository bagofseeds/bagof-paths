"""Synthesized lexical fallbacks: with_stem / is_relative_to for 3.8."""

import os
import pathlib


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
    from bagof.paths import Path

    p = Path(LexicalOnly("/a/b/c.txt"))
    assert p.supports("with_stem") is True
    assert str(p.with_stem("z")) == "/a/b/z.txt"
    assert isinstance(p.with_stem("z"), Path)


def test_is_relative_to_synthesized_from_relative_to() -> None:
    from bagof.paths import Path

    p = Path(LexicalOnly("/a/b/c"))
    assert p.supports("is_relative_to") is True
    assert p.is_relative_to("/a") is True
    assert p.is_relative_to("/x") is False


def test_every_fallback_name_is_registered() -> None:
    from bagof.paths._fallbacks import FALLBACKS
    from bagof.paths._spec import MEMBERS

    for member in MEMBERS:
        if member.fallback is not None:
            assert member.fallback in FALLBACKS, member.name
