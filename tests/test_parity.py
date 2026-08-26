"""Sync and async surfaces stay in lockstep.

The two wrappers share the pure-path mixin and the whole sync implementation;
the only thing written twice is the thin I/O signatures. This test fails the
moment they drift -- a member added to one and not the other, or with a
different signature.
"""

import inspect

from bagof.paths import AsyncPath, Path


def _own_functions(cls: type) -> dict:
    return {
        name: value
        for name, value in vars(cls).items()
        if inspect.isfunction(value) and not name.startswith("_")
    }


def test_async_mirrors_every_sync_io_member() -> None:
    sync_io = _own_functions(Path)
    for name in sync_io:
        member = getattr(AsyncPath, name, None)
        assert member is not None, f"AsyncPath is missing {name}"
        is_async = inspect.iscoroutinefunction(
            member
        ) or inspect.isasyncgenfunction(member)
        assert is_async, f"AsyncPath.{name} should be async"


def test_async_adds_no_extra_io_members() -> None:
    sync_io = set(_own_functions(Path))
    async_io = {
        name
        for name, value in vars(AsyncPath).items()
        if not name.startswith("_")
        and (
            inspect.iscoroutinefunction(value)
            or inspect.isasyncgenfunction(value)
        )
    }
    assert async_io == sync_io


def test_signatures_match_modulo_async() -> None:
    for name in _own_functions(Path):
        sync_params = list(inspect.signature(getattr(Path, name)).parameters)
        async_params = list(
            inspect.signature(getattr(AsyncPath, name)).parameters
        )
        assert sync_params == async_params, f"{name} parameters differ"
