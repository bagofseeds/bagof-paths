"""The extended surface: pathlib/UPath/cloudpathlib members made reachable.

Covers the status, permission, link, classmethod, driver-accessor and
transfer members added to close the parity gap -- delegated to the real
backends where the interpreter has them, and synthesized (with fake drivers
that hide a member) where a driver or Python version lacks it.
"""

import asyncio
import os
import pathlib

import pytest

from bagof.paths import AsyncPath, Path, UnsupportedPathOperation


def _run(coro: object) -> object:
    return asyncio.run(coro)


# -- a fake driver that hides selected members, to force fallbacks ----------
class _Fake(os.PathLike):
    """Wrap a real ``pathlib.Path`` but hide the members named in ``hide``.

    Its constructor takes path segments (like ``pathlib.Path``), so the
    ``with_segments`` fallback -- ``type(wrapped)(*segments)`` -- reconstructs
    one; everything not hidden delegates to the wrapped real path.
    """

    hide = frozenset()

    def __init__(self, *segments: object) -> None:
        object.__setattr__(self, "_real", pathlib.Path(*segments))

    def __fspath__(self) -> str:
        return os.fspath(self._real)

    def __str__(self) -> str:
        return str(self._real)

    def __getattr__(self, name: str) -> object:
        if name in type(self).hide:
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_real"), name)


class _Cloudish(os.PathLike):
    """A minimal non-local driver: no ``is_junction``, reports an s3 scheme."""

    protocol = "s3"

    def __fspath__(self) -> str:
        return "s3://b/k"

    def __str__(self) -> str:
        return "s3://b/k"


# -- extended status queries (stdlib pathlib) -------------------------------
def test_extended_status_queries(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a.txt"
    f.write_text("x")
    assert f.is_mount() is False
    assert f.is_socket() is False
    assert f.is_fifo() is False
    assert f.is_block_device() is False
    assert f.is_char_device() is False
    assert f.is_junction() is False
    assert f.is_reserved() is False


def test_is_junction_fallback_local(tmp_path: pathlib.Path) -> None:
    class NoJunction(_Fake):
        hide = frozenset({"is_junction"})

    f = tmp_path / "a.txt"
    f.write_text("x")
    # No native is_junction: synthesized -- a regular file is not a junction.
    assert Path(NoJunction(f)).is_junction() is False


def test_is_junction_fallback_nonlocal() -> None:
    # A non-local driver without the concept is synthesized to False.
    assert Path(_Cloudish()).is_junction() is False


# -- with_segments (lexical; synthesized before pathlib 3.12) ---------------
def test_with_segments_delegates(tmp_path: pathlib.Path) -> None:
    p = Path(tmp_path) / "a"
    q = p.with_segments(str(tmp_path), "b", "c")
    assert isinstance(q, Path)
    assert q == Path(tmp_path) / "b" / "c"


def test_with_segments_fallback(tmp_path: pathlib.Path) -> None:
    class NoWithSegments(_Fake):
        hide = frozenset({"with_segments"})

    p = Path(NoWithSegments(tmp_path, "a"))
    q = p.with_segments(str(tmp_path), "b")
    assert isinstance(q, Path)
    assert q == Path(tmp_path) / "b"


# -- permissions and ownership ----------------------------------------------
def test_chmod(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a"
    f.touch()
    f.chmod(0o600)
    assert (f.stat().st_mode & 0o777) == 0o600


class _RecordingLchmod(os.PathLike):
    """A driver whose lchmod just records the call (no platform dependency).

    lchmod is unavailable on many platforms (no os.lchmod on Linux), so a
    recording double lets the wrapper's delegation be tested without the
    operation actually being supported by the OS.
    """

    def __init__(self) -> None:
        self.calls = []

    def __fspath__(self) -> str:
        return "/rec"

    def __str__(self) -> str:
        return "/rec"

    def lchmod(self, mode: int) -> None:
        self.calls.append(mode)


def test_lchmod_delegates() -> None:
    driver = _RecordingLchmod()
    Path(driver).lchmod(0o640)
    assert driver.calls == [0o640]


def test_lchmod_raises_without_native() -> None:
    # No native lchmod and no portable synthesis: delegate-or-raise.
    with pytest.raises(UnsupportedPathOperation):
        Path(_Cloudish()).lchmod(0o640)


def test_hardlink_to_fallback(tmp_path: pathlib.Path) -> None:
    class NoHardlink(_Fake):
        hide = frozenset({"hardlink_to"})

    target = tmp_path / "t.txt"
    target.write_text("x")
    # No native hardlink_to (stdlib pathlib < 3.10): synthesized via os.link.
    Path(NoHardlink(tmp_path, "h.txt")).hardlink_to(target)
    assert (tmp_path / "h.txt").read_text() == "x"


def test_hardlink_to_fallback_refuses_nonlocal() -> None:
    # Hard links are a local-filesystem operation; a non-local driver without
    # a native hardlink_to is refused rather than guessed at.
    with pytest.raises(UnsupportedPathOperation):
        Path(_Cloudish()).hardlink_to("x")


def test_owner_and_group(tmp_path: pathlib.Path) -> None:
    f = Path(tmp_path) / "a"
    f.touch()
    assert isinstance(f.owner(), str)
    assert isinstance(f.group(), str)


# -- links ------------------------------------------------------------------
def test_symlink_and_hardlink(tmp_path: pathlib.Path) -> None:
    target = Path(tmp_path) / "t.txt"
    target.write_text("x")
    sym = Path(tmp_path) / "sym"
    sym.symlink_to(target)
    assert sym.is_symlink() is True
    hard = Path(tmp_path) / "hard"
    hard.hardlink_to(target)
    assert hard.read_text() == "x"


def test_link_to(tmp_path: pathlib.Path) -> None:
    # link_to makes its argument a hard link to this path (reverse of
    # hardlink_to); delegated where pathlib still has it, synthesized where
    # it was removed (3.12+).
    src = Path(tmp_path) / "src.txt"
    src.write_text("data")
    src.link_to(tmp_path / "dst.txt")
    assert (tmp_path / "dst.txt").read_text() == "data"


def test_link_to_fallback(tmp_path: pathlib.Path) -> None:
    class NoLinkTo(_Fake):
        hide = frozenset({"link_to"})

    real = tmp_path / "src.txt"
    real.write_text("data")
    Path(NoLinkTo(real)).link_to(tmp_path / "dst.txt")
    assert (tmp_path / "dst.txt").read_text() == "data"


# -- recursive copy / remove aliases ----------------------------------------
def test_rmtree_and_copytree(tmp_path: pathlib.Path) -> None:
    d = Path(tmp_path) / "tree"
    d.mkdir()
    (d / "f.txt").write_text("1")
    d.copytree(tmp_path / "tree2")
    assert (tmp_path / "tree2" / "f.txt").read_text() == "1"
    d.rmtree()
    assert not (tmp_path / "tree").exists()


# -- local-filesystem constructors ------------------------------------------
def test_home_cwd_from_uri(tmp_path: pathlib.Path) -> None:
    assert isinstance(Path.home(), Path)
    assert isinstance(Path.cwd(), Path)
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert Path.from_uri(f.as_uri()) == Path(f)


def test_from_uri_rejects_foreign_scheme() -> None:
    with pytest.raises(ValueError):
        Path.from_uri("s3://bucket/key")


# -- driver-specific accessors: absent on local -----------------------------
# `info` is intentionally omitted: stdlib pathlib grew a `.info` property in
# 3.14, so a local path delegates it there rather than raising. The rest are
# universal-pathlib/cloudpathlib concepts absent from stdlib on every version.
@pytest.mark.parametrize(
    "name",
    ["storage_options", "fs", "bucket", "key",
     "client", "cloud_prefix", "fspath", "etag"],
)
def test_accessor_raises_on_local(name: str, tmp_path: pathlib.Path) -> None:
    with pytest.raises(UnsupportedPathOperation):
        getattr(Path(tmp_path), name)


def test_as_url_synthesizes_from_as_uri_on_local(
    tmp_path: pathlib.Path,
) -> None:
    f = Path(tmp_path) / "a.txt"
    f.write_text("x")
    # No native as_url on stdlib pathlib: synthesized as the file URI.
    assert f.as_url() == f.as_uri()
    assert f.as_url().startswith("file://")
    # But a presign/option request cannot be honored from a plain URI: refuse
    # rather than hand back a silently-unsigned URL.
    with pytest.raises(UnsupportedPathOperation):
        f.as_url(presign=True)


def test_with_segments_fallback_refuses_nonlocal() -> None:
    # A non-local driver without with_segments cannot be rebuilt from segments
    # without dropping its configuration, so the fallback refuses.
    with pytest.raises(UnsupportedPathOperation):
        Path(_Cloudish()).with_segments("a", "b")


def test_supports_reports_accessors_without_raising(
    tmp_path: pathlib.Path,
) -> None:
    p = Path(tmp_path)
    # supports() must answer False for an absent accessor, never raise.
    # (info is omitted: stdlib pathlib has a native .info from 3.14.)
    for name in ("storage_options", "fs", "bucket", "etag"):
        assert p.supports(name) is False
    assert "bucket" not in p.capabilities()


def test_driver_specific_methods_raise_on_local(
    tmp_path: pathlib.Path,
) -> None:
    f = Path(tmp_path) / "a.txt"
    f.write_text("x")
    for call in (
        lambda: f.download_to(tmp_path / "o"),
        lambda: f.upload_from(tmp_path / "a.txt"),
        lambda: f.clear_cache(),
        lambda: f.joinuri("x"),
    ):
        with pytest.raises(UnsupportedPathOperation):
            call()


# -- universal-pathlib backend ----------------------------------------------
def test_upath_accessors_and_joinuri() -> None:
    UPath = pytest.importorskip("upath").UPath
    import collections.abc

    u = UPath("memory://bagofpaths-parity/a.txt")
    p = Path(u)
    assert isinstance(p.storage_options, collections.abc.Mapping)
    assert p.fs is not None
    assert p.info is not None
    joined = p.joinuri("b.txt")
    assert isinstance(joined, Path)


# -- cloudpathlib backend ---------------------------------------------------
def _cloud_path() -> object:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    return local.LocalS3Path("s3://bagofpaths-parity/a.txt", client=client)


def test_cloud_accessors() -> None:
    raw = _cloud_path()
    raw.write_text("x")
    p = Path(raw)
    assert p.bucket == "bagofpaths-parity"
    assert p.key == "a.txt"
    assert p.cloud_prefix == "s3://"
    assert p.client is not None
    assert p.etag is not None
    assert p.info is not None
    assert isinstance(p.fspath, str)
    # supports()/capabilities() see the accessors the driver actually has.
    assert p.supports("bucket") is True
    assert p.supports("storage_options") is False  # cloudpathlib has no fs
    assert "bucket" in p.capabilities()


def test_cloud_transfer_methods(tmp_path: pathlib.Path) -> None:
    raw = _cloud_path()
    raw.write_text("payload")
    p = Path(raw)
    assert p.as_url() == "s3://bagofpaths-parity/a.txt"
    out = p.download_to(tmp_path / "out.txt")
    assert pathlib.Path(out).read_text() == "payload"
    src = tmp_path / "up.txt"
    src.write_text("fresh")
    up = Path(
        _cloud_path().client.CloudPath("s3://bagofpaths-parity/up.txt")
    )
    returned = up.upload_from(src)
    assert isinstance(returned, Path)  # re-wrapped, not the raw driver path
    assert up.read_text() == "fresh"
    p.clear_cache()  # no raise


# -- async mirrors ----------------------------------------------------------
def test_async_extended_local(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        f = AsyncPath(tmp_path) / "a.txt"
        await f.write_text("x")
        assert await f.is_mount() is False
        assert await f.is_socket() is False
        assert await f.is_fifo() is False
        assert await f.is_block_device() is False
        assert await f.is_char_device() is False
        assert await f.is_junction() is False
        await f.chmod(0o600)
        assert isinstance(await f.owner(), str)
        assert isinstance(await f.group(), str)
        sym = AsyncPath(tmp_path) / "sym"
        await sym.symlink_to(f)
        assert await sym.is_symlink() is True
        hard = AsyncPath(tmp_path) / "hard"
        await hard.hardlink_to(f)
        await f.link_to(tmp_path / "dst.txt")
        assert (tmp_path / "dst.txt").exists()

    _run(go())


def test_async_lchmod_delegates() -> None:
    driver = _RecordingLchmod()

    async def go() -> None:
        await AsyncPath(driver).lchmod(0o600)

    _run(go())
    assert driver.calls == [0o600]


def test_async_rmtree_copytree(tmp_path: pathlib.Path) -> None:
    async def go() -> None:
        d = AsyncPath(tmp_path) / "tree"
        await d.mkdir()
        await (d / "f.txt").write_text("1")
        await d.copytree(tmp_path / "tree2")
        assert (tmp_path / "tree2" / "f.txt").read_text() == "1"
        await d.rmtree()
        assert not (tmp_path / "tree").exists()

    _run(go())


def test_async_cloud_transfer(tmp_path: pathlib.Path) -> None:
    raw = _cloud_path()
    raw.write_text("payload")

    async def go() -> None:
        p = AsyncPath(raw)
        assert await p.as_url() == "s3://bagofpaths-parity/a.txt"
        out = await p.download_to(tmp_path / "out.txt")
        assert pathlib.Path(out).read_text() == "payload"
        src = tmp_path / "up.txt"
        src.write_text("fresh")
        up = AsyncPath(
            raw.client.CloudPath("s3://bagofpaths-parity/up2.txt")
        )
        returned = await up.upload_from(src)
        assert isinstance(returned, AsyncPath)  # re-wrapped
        assert up.wrapped.read_text() == "fresh"
        await p.clear_cache()

    _run(go())
