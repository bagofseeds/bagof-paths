"""Building a wrapper from a URL string: driver selection and protocols."""

import asyncio
import pathlib

import pytest

from bagof.paths import (
    AsyncPath,
    NoDriverError,
    Path,
    ProtocolTraits,
    register_protocol,
)
from bagof.paths._protocols import canonical_scheme, traits_for


class _FakeDriver:
    """A minimal path-shaped object a driver factory can return."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __fspath__(self) -> str:
        return self.text

    def __str__(self) -> str:
        return self.text


# -- backend-independent ----------------------------------------------------
def test_unknown_scheme_raises_no_driver() -> None:
    with pytest.raises(NoDriverError) as info:
        Path("bogus+unknown://x/y")
    assert info.value.scheme == "bogus+unknown"
    # NoDriverError is a ValueError, keeping the constructor's historical type.
    assert isinstance(info.value, ValueError)


def test_explicit_driver_class() -> None:
    p = Path("s3://bucket/key", driver=_FakeDriver)
    assert isinstance(p.wrapped, _FakeDriver)
    assert str(p) == "s3://bucket/key"
    assert p.protocol == "s3"  # parsed from the string


def test_explicit_driver_callable_wins_even_for_local() -> None:
    built = []
    p = Path("/tmp/x", driver=lambda t: built.append(t) or _FakeDriver(t))
    assert isinstance(p.wrapped, _FakeDriver)
    assert built == ["/tmp/x"]


def test_driver_kwarg_on_a_path_object_raises() -> None:
    with pytest.raises(TypeError):
        Path(_FakeDriver("s3://b/k"), driver=_FakeDriver)


def test_local_string_is_unchanged() -> None:
    assert isinstance(Path("/tmp/x").wrapped, pathlib.Path)
    assert Path("/tmp/x") == Path("/tmp/x")


def test_file_uri_becomes_local(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "a.txt"
    p = Path(f.as_uri())  # file://...
    assert isinstance(p.wrapped, pathlib.Path)
    assert str(p) == str(f)


def test_register_protocol_traits_and_aliases() -> None:
    register_protocol(
        "selproto", bucketed=True, aliases=("selp",), driver=_FakeDriver
    )
    assert traits_for("selproto").bucketed is True
    assert traits_for("selp").bucketed is True  # alias resolves
    assert canonical_scheme("selp") == "selproto"

    p = Path("selproto://bucket/obj")
    assert isinstance(p.wrapped, _FakeDriver)
    # A spelling and its alias name the same location.
    assert p == Path("selp://bucket/obj")
    assert hash(p) == hash(Path("selp://bucket/obj"))


def test_register_protocol_case_insensitive_keys() -> None:
    register_protocol("SelUpper", driver=_FakeDriver, bucketed=True)
    assert traits_for("selupper").bucketed is True
    assert Path("SELUPPER://b/k").protocol == "selupper"


def test_protocol_traits_is_keyword_only() -> None:
    t = ProtocolTraits(bucketed=True, aliases=("x",))
    assert t.bucketed is True and t.aliases == ("x",)
    assert "bucketed=True" in repr(t)
    with pytest.raises(TypeError):
        ProtocolTraits(True)  # positional is rejected -> future fields safe


def test_local_url_scheme() -> None:
    p = Path("local://tmp/thing")
    assert isinstance(p.wrapped, pathlib.Path)


def test_selection_without_upath_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bagof.paths._select as sel

    # With universal-pathlib unavailable and no cloudpathlib impl for the
    # scheme, selection has no factory left and raises NoDriverError.
    monkeypatch.setattr(sel, "_upath_class", lambda: None)
    with pytest.raises(NoDriverError):
        sel.build("weird-scheme://x", "weird-scheme")


def test_double_colon_local_filename_stays_local() -> None:
    # "::" without an inner "://" is a legal local filename, not an fsspec
    # chain -- it must not be routed to driver selection.
    assert isinstance(Path("weird::name.txt").wrapped, pathlib.Path)


def test_preferred_driver_error_is_not_masked() -> None:
    def boom(text: str) -> object:
        raise ValueError("bad config")

    register_protocol("perr1", driver=boom)
    with pytest.raises(ValueError) as info:
        Path("perr1://x")
    assert "bad config" in str(info.value)
    assert not isinstance(info.value, NoDriverError)  # not masked


def test_upath_real_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    import bagof.paths._select as sel

    class _FakeUP:
        def __init__(self, text: str) -> None:
            raise ValueError("must provide `base_url` storage option")

    monkeypatch.setattr(sel, "_upath_class", lambda: _FakeUP)
    with pytest.raises(ValueError) as info:
        Path("s3://b/k")
    assert not isinstance(info.value, NoDriverError)
    assert "base_url" in str(info.value)


def test_upath_unknown_scheme_becomes_no_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bagof.paths._select as sel

    class _FakeUP:
        def __init__(self, text: str) -> None:
            raise ValueError("Unsupported filesystem: 'zzz'")

    monkeypatch.setattr(sel, "_upath_class", lambda: _FakeUP)
    with pytest.raises(NoDriverError):
        Path("zzz://b/k")


def test_register_protocol_replace_purges_aliases() -> None:
    register_protocol("rev1", aliases=("rev1a",))
    assert canonical_scheme("rev1a") == "rev1"
    register_protocol("rev1")  # re-register with no aliases
    assert canonical_scheme("rev1a") == "rev1a"  # stale alias dropped


def test_register_protocol_refuses_alias_hijack() -> None:
    register_protocol("canon1", bucketed=True)
    with pytest.raises(ValueError):
        register_protocol("other1", aliases=("canon1",))


def test_from_uri_file_scheme_is_case_insensitive(
    tmp_path: pathlib.Path,
) -> None:
    f = tmp_path / "a.txt"
    p = Path.from_uri("FILE://" + str(f))  # upper-case scheme
    assert isinstance(p.wrapped, pathlib.Path)
    assert str(p) == str(f)


def test_no_driver_error_empty_scheme_message() -> None:
    e = NoDriverError("")  # a chained URL has no single scheme
    assert e.scheme == ""
    assert "this URL" in str(e)


def test_bucket_root_hint() -> None:
    from bagof.paths import UnsupportedPathOperation

    # A bucketed protocol whose path names no bucket (the driver has no drive).
    register_protocol("bktless", bucketed=True, driver=_FakeDriver)
    with pytest.raises(UnsupportedPathOperation) as info:
        _ = Path("bktless://x").bucket
    assert "root" in str(info.value)


def test_cloudpathlib_factory_rewrites_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cloudpathlib")
    import cloudpathlib.cloudpath as cc

    import bagof.paths._select as sel

    class _FakePathClass:
        cloud_prefix = "s3://"

        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeImpl:
        path_class = _FakePathClass

    monkeypatch.setitem(cc.implementation_registry, "s3", _FakeImpl)
    monkeypatch.setattr(sel, "_upath_class", lambda: None)
    # s3a (an alias) -> canonical s3 -> the (faked) concrete class, with the
    # prefix rewritten from s3a:// to the class's own s3://.
    p = Path("s3a://bucket/key")
    assert isinstance(p.wrapped, _FakePathClass)
    assert p.wrapped.text == "s3://bucket/key"


def test_cloud_key_maps_aliases() -> None:
    import bagof.paths._select as sel

    assert sel._cloud_key("abfs") == "azure"
    assert sel._cloud_key("s3a") == "s3"
    assert sel._cloud_key("gcs") == "gs"


def test_cloudpathlib_selected_for_azure_and_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("cloudpathlib")
    from cloudpathlib.exceptions import (
        InvalidPrefixError,
        MissingDependenciesError,
    )

    import bagof.paths._select as sel

    # cloudpathlib as the fallback: an aliased/azure scheme must resolve to the
    # right concrete class with the prefix rewritten, never NoDriverError and
    # never an InvalidPrefix rejection.
    monkeypatch.setattr(sel, "_upath_class", lambda: None)
    for url in ("az://c/b", "abfs://c/b", "s3a://b/k"):
        try:
            Path(url)
        except NoDriverError:
            pytest.fail(f"{url} wrongly raised NoDriverError")
        except InvalidPrefixError:
            pytest.fail(f"{url} prefix was not rewritten")
        except MissingDependenciesError:
            pass  # correct class selected; the cloud SDK is just absent


def test_cloudpathlib_class_lookup() -> None:
    pytest.importorskip("cloudpathlib")
    import bagof.paths._select as sel

    assert sel._cloudpathlib_class("") is None
    assert sel._cloudpathlib_class("not-a-cloud-scheme") is None
    from cloudpathlib.exceptions import MissingDependenciesError

    try:
        cls = sel._cloudpathlib_class("s3")
        assert cls.__name__ == "S3Path"
    except MissingDependenciesError:
        pass  # cloud SDK absent: the intended fail-fast


def test_unregistered_scheme_gets_default_traits() -> None:
    t = traits_for("never-registered")
    assert t.bucketed is False and t.absolute is False


# -- with universal-pathlib -------------------------------------------------
def _upath() -> type:
    return pytest.importorskip("upath").UPath


def test_construct_s3_via_upath() -> None:
    _upath()
    p = Path("s3://bucket/key.txt")
    assert type(p.wrapped).__name__ == "S3Path"
    assert p.protocol == "s3"
    assert p.path == "bucket/key.txt"


def test_bucket_derived_from_drive() -> None:
    _upath()
    p = Path("s3://bucket/key.txt")
    assert p.bucket == "bucket"  # UPath has no .bucket; derived from drive
    assert p.supports("bucket") is True
    assert "bucket" in p.capabilities()


def test_alias_identity_s3a() -> None:
    _upath()
    assert Path("s3://b/k") == Path("s3a://b/k")
    assert hash(Path("s3://b/k")) == hash(Path("s3a://b/k"))


def test_string_and_object_construction_agree() -> None:
    UPath = _upath()
    assert Path("s3://b/k") == Path(UPath("s3://b/k"))


def test_scheme_is_case_insensitive() -> None:
    _upath()
    p = Path("S3://Bucket/Key")
    assert p.protocol == "s3"
    assert p.path == "Bucket/Key"  # only the scheme is lower-cased


def test_fsspec_chain_routes_to_a_driver() -> None:
    _upath()
    # A chain with no leading scheme:// must not become a local path.
    p = Path("simplecache::s3://bucket/key")
    assert type(p.wrapped).__name__ == "SimpleCachePath"


def test_from_uri_remote_matches_constructor() -> None:
    _upath()
    assert Path.from_uri("s3://b/k") == Path("s3://b/k")


def test_capabilities_survive_a_missing_backend_sdk() -> None:
    # UPath's fs/info build the filesystem when read (ImportError without the
    # SDK); supports()/capabilities() must answer from the class, not crash.
    _upath()
    caps = Path("s3://b/k").capabilities()
    assert "fs" in caps and "storage_options" in caps


def test_memory_roundtrip() -> None:
    _upath()
    p = Path("memory://bagofpaths-select/a.txt")
    p.write_text("hi")
    assert p.read_text() == "hi"
    assert p.protocol == "memory"


def test_async_constructs_from_url() -> None:
    _upath()

    async def go() -> str:
        return type(AsyncPath("s3://b/k").wrapped).__name__

    assert asyncio.run(go()) == "S3Path"


def test_preferred_driver_from_register_protocol() -> None:
    UPath = _upath()
    # A protocol whose preferred driver is explicitly UPath's memory path.
    register_protocol(
        "selmem", driver=lambda t: UPath("memory://" + t.split("://", 1)[1])
    )
    p = Path("selmem://x/y.txt")
    assert type(p.wrapped).__name__ == "MemoryPath"
