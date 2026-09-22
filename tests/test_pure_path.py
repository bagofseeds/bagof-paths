"""The public pure-path type and its dependency-free lexical cloud driver.

``PurePath`` is the lexical level of the path model: it parses and manipulates
a path, local or remote, with no backend installed. These tests cover its
surface, its identity parity with ``Path`` (the highest-risk part -- verified
by *running* the real backends, not by reasoning about them), and the
graceful-degradation tail that lets a full ``Path`` do lexical work with no
backend.
"""

import asyncio
import pathlib
import sys

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
    # I/O is refused with a hint that names the remedy, and never leaks the
    # internal driver class name.
    with pytest.raises(UnsupportedPathOperation) as info:
        p.read_bytes()
    message = str(info.value)
    assert "backend" in message
    assert "PureCloudPath" not in message


# -- cross-scheme relative_to (a different store is never relative) ---------
def test_relative_to_rejects_a_different_scheme() -> None:
    p = PurePath("s3://bucket/dir/key.txt")
    # A different store is not a parent, matching universal-pathlib.
    assert p.is_relative_to("gs://bucket/dir") is False
    with pytest.raises(ValueError):
        p.relative_to("gs://bucket/dir")


def test_relative_to_accepts_a_same_store_alias() -> None:
    p = PurePath("s3://bucket/dir/key.txt")
    # s3 and s3a name the same store, so one is relative to the other.
    assert p.is_relative_to("s3a://bucket/dir") is True
    assert p.is_relative_to("s3://bucket/dir") is True


def test_relative_to_result_is_scheme_less() -> None:
    p = PurePath("s3://bucket/dir/key.txt")
    rel = p.relative_to("s3://bucket/dir")
    # The result is a plain relative path, with no scheme prefix in its text.
    assert str(rel) == "key.txt"
    assert rel.path == "key.txt"
    assert rel.protocol == ""
    # A path-object operand is accepted too.
    assert str(p.relative_to(PurePath("s3://bucket"))) == "dir/key.txt"
    # A bare relative operand is not a parent of an absolute remote path.
    assert p.is_relative_to("key.txt") is False


def test_relative_to_on_a_relative_scheme() -> None:
    # An unregistered scheme is neither bucketed nor absolute, so its key path
    # is relative; relative_to still compares within the scheme.
    p = PureCloudPath.from_url("thing://a/b/c", "thing")
    rel = p.relative_to(PureCloudPath.from_url("thing://a", "thing"))
    assert str(rel) == "b/c"


def test_relative_to_walk_up() -> None:
    p = PurePath("s3://bucket/dir/key.txt")
    if sys.version_info >= (3, 12):
        rel = p.relative_to("s3://bucket/other", walk_up=True)
        assert str(rel) == "../dir/key.txt"
    else:
        # walk_up reached PurePath in 3.12; older floors name the limitation
        # rather than surface a raw TypeError.
        with pytest.raises(UnsupportedPathOperation):
            p.relative_to("s3://bucket/other", walk_up=True)


# -- the "." / "//" normalization residual (pinned; see design §5) ----------
def test_dot_and_double_slash_collapse() -> None:
    # The lexical driver delegates to PurePosixPath, which collapses a "."
    # segment and a doubled slash. universal-pathlib preserves both; this
    # documents and pins the residual divergence for ordinary use.
    assert PurePath("s3://bucket/a//b").path == "bucket/a/b"
    assert PurePath("s3://bucket/a/./b").path == "bucket/a/b"


# -- the lexical accessors that need no backend -----------------------------
def test_remote_lexical_accessors() -> None:
    p = PurePath("s3://bucket/dir/file.tar.gz")
    assert p.stem == "file.tar"
    assert p.suffixes == [".tar", ".gz"]
    assert p.as_posix() == "s3://bucket/dir/file.tar.gz"
    assert p.as_uri() == "s3://bucket/dir/file.tar.gz"
    assert p.is_absolute() is True
    assert p.is_reserved() is False
    assert p.storage_options == {}
    # The driver's own repr names it plainly.
    assert repr(p.wrapped) == "PureCloudPath('s3://bucket/dir/file.tar.gz')"


# -- construction routes: explicit driver, chain, and file URL --------------
def test_construction_with_explicit_driver() -> None:
    built = []

    def factory(text: str) -> pathlib.PurePosixPath:
        built.append(text)
        return pathlib.PurePosixPath(text)

    p = PurePath("s3://bucket/key", driver=factory)
    assert built == ["s3://bucket/key"]
    assert isinstance(p.wrapped, pathlib.PurePath)


def test_construction_of_an_fsspec_chain() -> None:
    p = PurePath("simplecache::s3://bucket/key")
    assert isinstance(p.wrapped, PureCloudPath)
    # A chain has no single identity scheme.
    assert p.protocol == ""


def test_construction_from_a_file_url(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "a.txt"
    p = PurePath(target.as_uri())  # file://...
    assert isinstance(p.wrapped, pathlib.PurePath)
    assert str(p) == str(target)
