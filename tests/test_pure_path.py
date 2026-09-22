"""The public pure-path type and its dependency-free lexical cloud driver.

``PurePath`` is the lexical level of the path model: it parses and manipulates
a path, local or remote, with no backend installed. These tests cover its
surface, its identity parity with ``Path`` (the highest-risk part -- verified
by *running* the real backends, not by reasoning about them), and the
graceful-degradation tail that lets a full ``Path`` do lexical work with no
backend.
"""

import asyncio

import pytest

from bagof.paths import AsyncPath, Path, PurePath, UnsupportedPathOperation
from bagof.paths._pure_driver import PureCloudPath

# The bucketed object stores, with an alias each, and a non-bucketed absolute
# scheme. Each entry is a URL that does not use "." or "//" in its key, so its
# normalization is unambiguous across backends.
REMOTE_URLS = [
    "s3://bucket/dir/key.txt",
    "s3://bucket/key",
    "s3a://bucket/dir/key.txt",
    "gs://bucket/dir/key.txt",
    "gcs://bucket/dir/key.txt",
    "az://container/dir/key.txt",
    "abfs://container/dir/key.txt",
    "memory://root/dir/key.txt",
]


# -- the public surface, with no backend needed -----------------------------
def test_local_lexical_surface() -> None:
    p = PurePath("/data/sets/train.zarr")
    assert p.name == "train.zarr"
    assert p.stem == "train"
    assert p.suffix == ".zarr"
    assert str(p.parent) == "/data/sets"
    assert str(p / "chunks") == "/data/sets/train.zarr/chunks"
    assert p.with_name("val.zarr").name == "val.zarr"


def test_remote_lexical_surface_uses_no_backend() -> None:
    # A remote URL is always backed by the dependency-free driver, so lexical
    # work never needs universal-pathlib or cloudpathlib.
    p = PurePath("s3://bucket/dir/key.txt")
    assert isinstance(p.wrapped, PureCloudPath)
    assert p.protocol == "s3"
    assert p.path == "bucket/dir/key.txt"
    assert p.drive == "bucket"
    assert p.name == "key.txt"
    assert str(p.parent) == "s3://bucket/dir"
    assert p.match("*.txt") is True
    assert p.full_match("bucket/**/key.txt") is True


def test_pure_path_has_no_io_members() -> None:
    p = PurePath("/tmp/x")
    for name in ("read_bytes", "write_bytes", "open", "iterdir", "mkdir"):
        assert not hasattr(p, name), name


def test_pure_path_rejects_storage_options_on_local() -> None:
    with pytest.raises(TypeError):
        PurePath("/tmp/x", storage_options={"key": "v"})


# -- identity parity: PurePath equals Path ----------------------------------
@pytest.mark.parametrize("url", REMOTE_URLS)
def test_pure_equals_full_path(url: str) -> None:
    pure = PurePath(url)
    full = Path(url)
    assert pure == full
    assert full == pure
    assert hash(pure) == hash(full)
    # Usable interchangeably as a mapping key.
    assert {pure: 1}[full] == 1


def test_alias_identity() -> None:
    assert PurePath("s3://b/k") == PurePath("s3a://b/k")
    assert PurePath("gs://b/k") == PurePath("gcs://b/k")
    assert PurePath("az://c/k") == PurePath("abfs://c/k")
    assert hash(PurePath("s3://b/k")) == hash(PurePath("s3a://b/k"))


def test_cross_family_inequality() -> None:
    async def build() -> AsyncPath:
        return AsyncPath("/a/b")

    other = asyncio.run(build())
    assert (PurePath("/a/b") == other) is False


# -- identity parity against the real universal-pathlib backend -------------
@pytest.mark.parametrize("url", REMOTE_URLS)
def test_matches_upath_location(url: str) -> None:
    UPath = pytest.importorskip("upath").UPath
    pure = PurePath(url)
    real = UPath(url)
    assert pure.path == real.path
    assert pure.drive == (real.drive or "")
    assert pure.root == (real.root or "")
    assert pure.name == real.name
    assert str(pure) == str(real)
    assert tuple(pure.parts) == tuple(real.parts)
    # Identity: PurePath and a UPath-backed Path agree.
    assert pure == Path(real)


def test_matches_upath_derivation() -> None:
    UPath = pytest.importorskip("upath").UPath
    pure = PurePath("s3://bucket/dir/file.txt")
    real = UPath("s3://bucket/dir/file.txt")
    for get in (
        lambda p: p.parent,
        lambda p: p.parent.parent,
        lambda p: p.parent.parent.parent,  # stays at the bucket root
        lambda p: p.joinpath("x", "y"),
        lambda p: p / "sub",
        lambda p: p.with_name("z.bin"),
        lambda p: p.with_suffix(".dat"),
        lambda p: p.with_stem("q"),
    ):
        assert get(pure).path == get(real).path
    assert [p.path for p in pure.parents] == [
        p.path for p in real.parents
    ]
    assert pure.is_relative_to("s3://bucket/dir") is True
    assert pure.is_relative_to("s3://other") is False


# -- identity parity against the real cloudpathlib backend ------------------
def test_matches_cloudpathlib_location() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    client = local.LocalS3Client()
    raw = local.LocalS3Path("s3://bucket/dir/key.txt", client=client)
    # A PurePath and a cloudpathlib-backed Path agree on a keyed path.
    assert PurePath("s3://bucket/dir/key.txt") == Path(raw)
    assert PurePath("s3://bucket/dir/key.txt").drive == raw.drive


# -- the pinned bucket-root normalization (a UPath / cloudpathlib divergence)
def test_bucket_root_matches_upath() -> None:
    # The two backends disagree on a bare bucket: universal-pathlib normalizes
    # it to a trailing slash (path "bucket/", name ""), while cloudpathlib
    # keeps "bucket" (name "bucket"). universal-pathlib is the package default,
    # so the lexical driver converges on its spelling. This test pins that.
    UPath = pytest.importorskip("upath").UPath
    for url in ("s3://bucket", "s3://bucket/"):
        pure = PurePath(url)
        assert pure.path == "bucket/"
        assert pure.name == ""
        assert pure.drive == "bucket"
        assert pure.path == UPath(url).path
    # The parent of a bucket root is the bucket root again.
    assert str(PurePath("s3://bucket/only").parent) == "s3://bucket/"
    assert PurePath("s3://bucket/only").parent == PurePath("s3://bucket")


# -- the pure cloud driver directly -----------------------------------------
def test_pure_driver_default_scheme_is_relative() -> None:
    # An unregistered scheme has neither a bucket nor an absolute root: the
    # remainder is kept verbatim.
    p = PureCloudPath.from_url("thing://a/b/c", "thing")
    assert p.protocol == "thing"
    assert p.drive == ""
    assert p.root == ""
    assert p.path == "a/b/c"
    assert p.name == "c"


def test_pure_driver_memory_is_absolute_without_a_drive() -> None:
    p = PureCloudPath.from_url("memory://a/b", "memory")
    assert p.drive == ""
    assert p.root == "/"
    assert p.path == "/a/b"
    assert str(p) == "memory://a/b"


# -- the graceful-degradation tail on a full Path ---------------------------
def test_no_backend_path_is_lexical_then_refuses_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bagof.paths._select as sel

    monkeypatch.setattr(sel, "_upath_class", lambda: None)
    monkeypatch.setattr(sel, "_cloudpathlib_impl", lambda scheme: None)
    p = Path("s3://bucket/dir/key.txt")
    assert isinstance(p.wrapped, PureCloudPath)
    # Lexical operations work.
    assert p.name == "key.txt"
    assert p.suffix == ".txt"
    assert str(p.parent) == "s3://bucket/dir"
    assert (p == PurePath("s3://bucket/dir/key.txt")) is True
    # I/O is refused with a hint that names the remedy.
    with pytest.raises(UnsupportedPathOperation) as info:
        p.read_bytes()
    assert "backend" in str(info.value)
