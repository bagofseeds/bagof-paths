"""The storage-options surface: forwarding connection options and secrets.

A remote URL can carry ``storage_options`` (endpoint, credentials, ...) to
the driver, either per call or as a per-scheme default. These tests pin the
forwarding, the misuse errors, and that secrets never leak into the parts of
the surface a debug log would print.
"""

import asyncio
import pathlib

import pytest

from bagof.paths import (
    AsyncPath,
    Path,
    ProtocolTraits,
    UnsupportedPathOperation,
    register_protocol,
)
from bagof.paths._protocols import traits_for


class _Recorder:
    """A path-shaped driver that records how its factory was called."""

    calls: list = []

    def __init__(self, text: str, **options: object) -> None:
        self.text = text
        self.options = options
        _Recorder.calls.append((text, options))

    def __fspath__(self) -> str:
        return self.text

    def __str__(self) -> str:
        return self.text


@pytest.fixture(autouse=True)
def _reset_recorder() -> None:
    _Recorder.calls = []


class _AsyncDriver:
    """A path-shaped object whose I/O members are coroutines."""

    def __init__(self, text: str = "mem://x") -> None:
        self.path = text

    def __str__(self) -> str:
        return self.path

    async def exists(self) -> bool:  # marks the driver asynchronous
        return True


# -- forwarding -------------------------------------------------------------
def test_options_reach_explicit_driver() -> None:
    Path("s3://b/k", driver=_Recorder, storage_options={"key": "AKIA", "x": 1})
    assert _Recorder.calls == [("s3://b/k", {"key": "AKIA", "x": 1})]


def test_no_options_calls_driver_with_url_only() -> None:
    # The historical str -> path factory contract still holds: no options
    # means the factory is called with the URL alone, no keywords.
    Path("s3://b/k", driver=_Recorder)
    assert _Recorder.calls == [("s3://b/k", {})]


def test_options_reach_preferred_driver() -> None:
    register_protocol("credproto", driver=_Recorder)
    Path("credproto://b/k", storage_options={"token": "t"})
    assert _Recorder.calls == [("credproto://b/k", {"token": "t"})]


def test_per_scheme_default_options() -> None:
    register_protocol(
        "credflat", driver=_Recorder, storage_options={"endpoint": "e"}
    )
    Path("credflat://b/k")
    assert _Recorder.calls == [("credflat://b/k", {"endpoint": "e"})]


def test_per_call_overrides_per_scheme_default() -> None:
    register_protocol(
        "credmerge",
        driver=_Recorder,
        storage_options={"endpoint": "default", "anon": True},
    )
    Path("credmerge://b/k", storage_options={"endpoint": "override"})
    # Per-call wins key by key; untouched defaults survive.
    assert _Recorder.calls == [
        ("credmerge://b/k", {"endpoint": "override", "anon": True})
    ]


# -- misuse -----------------------------------------------------------------
def test_options_on_a_path_object_raise() -> None:
    with pytest.raises(TypeError):
        Path(pathlib.Path("/tmp/x"), storage_options={"key": "v"})


def test_options_on_a_local_path_raise() -> None:
    with pytest.raises(TypeError):
        Path("/tmp/x", storage_options={"key": "v"})


def test_options_on_a_local_url_raise() -> None:
    with pytest.raises(TypeError):
        Path("file:///tmp/x", storage_options={"key": "v"})


def test_empty_options_on_local_path_is_fine() -> None:
    # An empty (or absent) mapping is not "using" storage options.
    assert isinstance(Path("/tmp/x", storage_options={}).wrapped, pathlib.Path)


# -- secrets never leak -----------------------------------------------------
def test_traits_repr_redacts_options() -> None:
    secret = "super-secret-token"
    register_protocol(
        "credleak", driver=_Recorder, storage_options={"k": secret}
    )
    text = repr(traits_for("credleak"))
    assert secret not in text
    assert "<redacted>" in text


def test_traits_repr_shows_empty_when_no_options() -> None:
    assert "storage_options={}" in repr(ProtocolTraits())


def test_options_stored_on_traits() -> None:
    register_protocol("credstore", storage_options={"endpoint": "e"})
    assert traits_for("credstore").storage_options == {"endpoint": "e"}


def test_wrapper_repr_and_str_hide_options() -> None:
    secret = "AKIA-do-not-print"
    p = Path("s3://b/k", driver=_Recorder, storage_options={"key": secret})
    assert secret not in repr(p)
    assert secret not in str(p)


# -- Path refuses an async driver (B7) --------------------------------------
def test_sync_path_refuses_async_driver() -> None:
    with pytest.raises(UnsupportedPathOperation) as info:
        Path(_AsyncDriver())
    assert "AsyncPath" in str(info.value)


def test_async_path_accepts_async_driver() -> None:
    driver = _AsyncDriver()

    async def go() -> object:
        return AsyncPath(driver).wrapped

    assert asyncio.run(go()) is driver


# -- cloudpathlib cannot take an options dict -------------------------------
def test_cloudpathlib_rejects_options(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("cloudpathlib")
    import bagof.paths._select as sel

    monkeypatch.setattr(sel, "_upath_class", lambda: None)
    with pytest.raises(TypeError) as info:
        Path("s3://b/k", storage_options={"key": "v"})
    # The message points at the escape hatch, not an internal name.
    assert "driver=" in str(info.value)
    assert "Client" in str(info.value)


# -- with universal-pathlib -------------------------------------------------
def test_options_forwarded_to_real_upath() -> None:
    pytest.importorskip("upath")
    p = Path("memory://credtest/a.txt", storage_options={"marker": "seen"})
    # UPath stores the options and exposes them; the accessor reads them back.
    assert p.storage_options.get("marker") == "seen"


def test_async_path_forwards_options_from_url() -> None:
    pytest.importorskip("upath")

    async def go() -> object:
        return AsyncPath(
            "memory://credtest/b.txt", storage_options={"marker": "async"}
        ).storage_options

    assert asyncio.run(go()).get("marker") == "async"
