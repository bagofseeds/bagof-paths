"""Sync and async surfaces stay in lockstep.

The two wrappers share the pure-path mixin and the whole sync implementation;
the only thing written twice is the thin I/O signatures. These tests fail the
moment they drift -- a member on one and not the other, a forgotten ``async``,
or a changed default or parameter kind.
"""

import inspect

from bagof.paths import AsyncPath, Path


def _own_io_functions(cls: type) -> set:
    return {
        name
        for name, value in vars(cls).items()
        if inspect.isfunction(value) and not name.startswith("_")
    }


def _shape(func: object) -> list:
    return [
        (p.name, p.kind, p.default)
        for p in inspect.signature(func).parameters.values()
    ]


def test_async_mirrors_every_sync_io_member() -> None:
    for name in _own_io_functions(Path):
        member = getattr(AsyncPath, name, None)
        assert member is not None, f"AsyncPath is missing {name}"
        is_async = inspect.iscoroutinefunction(
            member
        ) or inspect.isasyncgenfunction(member)
        assert is_async, f"AsyncPath.{name} should be async"


def test_async_has_no_stray_sync_members() -> None:
    # A forgotten `async` (a plain def) on AsyncPath would be caught here.
    strays = {
        name
        for name, value in vars(AsyncPath).items()
        if not name.startswith("_")
        and callable(value)
        and not (
            inspect.iscoroutinefunction(value)
            or inspect.isasyncgenfunction(value)
        )
    }
    assert strays == set(), f"AsyncPath has non-async public methods: {strays}"


def test_signatures_match_including_defaults_and_kinds() -> None:
    for name in _own_io_functions(Path):
        assert _shape(getattr(Path, name)) == _shape(
            getattr(AsyncPath, name)
        ), f"{name} signature differs"
