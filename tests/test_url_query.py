"""The part of a URL that is not its path (issue #17).

What counts as "not the path" follows what the backends do on I/O, measured
against universal-pathlib 0.3.10, cloudpathlib 0.26.0, fsspec/s3fs 2026.9.0,
gcsfs 2026.10.1 and adlfs 2026.8.0:

- an `http(s)` URL has a query and a fragment (RFC 3986). fsspec sends the
  query and never the fragment, and cloudpathlib's `HttpPath` leaves both out
  of the name;
- in an object store (`s3`, `gs`, `az`) and in `memory`, `?` and `#` are
  ordinary characters of a key, apart from the object version fsspec reads out
  of the URL (s3 `?versionId=`, az `?versionid=`, gs `#<n>` or
  `?generation=<n>` when version-aware). cloudpathlib keeps even that in the
  key, so over cloudpathlib it stays part of the name;
- a local path has neither.

The string of the path keeps everything, since I/O needs it. A derived path
names a different object, so it does not carry what was left out.
"""

import pytest

from bagof.paths import (
    AsyncPath,
    Path,
    ProtocolTraits,
    PurePath,
    register_protocol,
    set_storage_options,
)
from bagof.paths._protocols import strip_url_query, traits_for

FRONTS = [PurePath, Path, AsyncPath]


# -- http: the query and the fragment are not part of the path -------------
@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "url, name, suffix, parent",
    [
        ("https://h/dir/x.nii.gz?token=1", "x.nii.gz", ".gz", "https://h/dir"),
        ("https://h/dir/x.nii.gz#part", "x.nii.gz", ".gz", "https://h/dir"),
        # A slash inside the query is not a separator of the path.
        ("https://h/dir/x.nii.gz?sig=a/b.zip", "x.nii.gz", ".gz",
         "https://h/dir"),
        # A "?" inside the fragment belongs to the fragment.
        ("https://h/dir/x.nii.gz#a?b/c", "x.nii.gz", ".gz", "https://h/dir"),
        ("http://h/dir/x.nii.gz?token=1", "x.nii.gz", ".gz", "http://h/dir"),
    ],
)
def test_http_lexical_members_leave_out_the_query(
    cls: type, url: str, name: str, suffix: str, parent: str
) -> None:
    p = cls(url)
    assert p.name == name
    assert p.suffix == suffix
    assert p.stem == name[: -len(suffix)]
    assert p.parts[-1] == name
    assert str(p.parent) == parent
    assert all("?" not in str(a) and "#" not in str(a) for a in p.parents)


@pytest.mark.parametrize("cls", FRONTS)
def test_http_path_itself_keeps_the_query(cls: type) -> None:
    url = "https://h/dir/x.nii.gz?token=1#part"
    p = cls(url)
    assert str(p) == url
    assert str(p.wrapped) == url
    assert p.as_posix() == url


@pytest.mark.parametrize("cls", FRONTS)
def test_http_suffixes_and_match_leave_out_the_query(cls: type) -> None:
    p = cls("https://h/dir/x.nii.gz?token=1")
    assert p.suffixes == [".nii", ".gz"]
    assert p.match("*.nii.gz")
    assert p.full_match("**/x.nii.gz")
    assert not p.match("*token*")


@pytest.mark.parametrize("cls", FRONTS)
def test_http_derived_path_drops_the_query(cls: type) -> None:
    # As cloudpathlib's HttpPath and urljoin do: a derived path names a
    # different object, so the query does not ride onto it.
    p = cls("https://h/dir/x.nii.gz?token=1")
    assert str(p.with_suffix(".json")) == "https://h/dir/x.nii.json"
    assert str(p.with_name("y.txt")) == "https://h/dir/y.txt"
    assert str(p.with_stem("y")) == "https://h/dir/y.gz"
    base = cls("https://h/dir?token=1")
    assert str(base / "x.txt") == "https://h/dir/x.txt"
    assert str(base.joinpath("a", "x.txt")) == "https://h/dir/a/x.txt"


@pytest.mark.parametrize("cls", FRONTS)
def test_http_joined_query_is_kept_and_left_out_of_the_name(
    cls: type,
) -> None:
    p = cls("https://h/dir") / "x.nii.gz?token=1"
    assert str(p) == "https://h/dir/x.nii.gz?token=1"
    assert p.name == "x.nii.gz"


@pytest.mark.parametrize("cls", FRONTS)
def test_http_relative_to_leaves_out_both_queries(cls: type) -> None:
    p = cls("https://h/dir/x.nii.gz?token=1")
    assert str(p.relative_to("https://h/dir?other=2")) == "x.nii.gz"
    assert str(p.relative_to(cls("https://h/dir#frag"))) == "x.nii.gz"
    assert p.is_relative_to(cls("https://h/dir?other=2"))
    assert p.is_relative_to("https://h/dir?other=2")


# -- object stores: "?" and "#" are characters of the key ------------------
@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "url, name, parent",
    [
        # s3fs, cloudpathlib and universal-pathlib all read the key
        # "dir/store.zarr?version=2": only "?versionId=" names a version.
        ("s3://b/dir/store.zarr?version=2", "store.zarr?version=2",
         "s3://b/dir"),
        # A key is a flat string, so its "/" separates even after a "?".
        ("s3://b/dir/x?sig=a/b.zip", "b.zip", "s3://b/dir/x?sig=a"),
        ("s3://b/dir/x.nii.gz#f", "x.nii.gz#f", "s3://b/dir"),
        # Not the first query parameter: s3fs keeps it in the key.
        ("s3://b/dir/x?a=1&versionId=2", "x?a=1&versionId=2", "s3://b/dir"),
        # gcsfs takes a generation only when it is an integer.
        ("gs://b/dir/x.nii.gz#part", "x.nii.gz#part", "gs://b/dir"),
        ("gs://b/dir/x.nii.gz?x=1", "x.nii.gz?x=1", "gs://b/dir"),
        ("gs://b/dir/x.nii.gz?generation=abc", "x.nii.gz?generation=abc",
         "gs://b/dir"),
        # The fragment wins over the query, so a bad one keeps both.
        ("gs://b/dir/x?generation=12#abc", "x?generation=12#abc",
         "gs://b/dir"),
        ("memory://dir/x.nii.gz?q=1#f", "x.nii.gz?q=1#f", "memory://dir"),
    ],
)
def test_store_keeps_question_mark_and_hash_in_the_key(
    cls: type, url: str, name: str, parent: str
) -> None:
    p = cls(url)
    assert p.name == name
    assert str(p.parent) == parent
    assert str(p) == url


# -- object stores: fsspec's version selector is not part of the key -------
# Path and AsyncPath run over universal-pathlib, or over the lexical fallback
# when it is absent, which parses a URL as universal-pathlib does.
@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "url, parent",
    [
        ("s3://b/dir/x.nii.gz?versionId=abc", "s3://b/dir"),
        ("s3a://b/dir/x.nii.gz?versionId=abc", "s3a://b/dir"),
        ("gs://b/dir/x.nii.gz#123", "gs://b/dir"),
        ("gs://b/dir/x.nii.gz?generation=123", "gs://b/dir"),
        ("az://c/dir/x.nii.gz?versionid=abc", "az://c/dir"),
    ],
)
def test_version_selector_is_left_out_of_the_name(
    cls: type, url: str, parent: str
) -> None:
    if url.startswith("az") and cls is not PurePath:
        pytest.importorskip("adlfs")
    p = cls(url)
    assert p.name == "x.nii.gz"
    assert p.suffixes == [".nii", ".gz"]
    assert str(p.parent) == parent
    assert str(p) == url
    # A version of this object is not a version of its sibling.
    assert str(p.with_suffix(".json")) == parent + "/x.nii.json"


@pytest.mark.parametrize("cls", [PurePath, Path])
def test_gcs_generation_needs_version_aware(cls: type) -> None:
    # gcsfs reads a generation only when version-aware; universal-pathlib
    # turns that on for such a URL unless the caller turns it off.
    url = "gs://b/dir/x.nii.gz#123"
    p = cls(url, storage_options={"version_aware": False})
    assert p.name == "x.nii.gz#123"


def test_cloudpathlib_keeps_the_version_selector_in_the_key() -> None:
    pytest.importorskip("cloudpathlib")
    local = pytest.importorskip("cloudpathlib.local")
    # cloudpathlib sends "dir/x.nii.gz?versionId=abc" as the key, so the name
    # over it must say so; fsspec would read version abc of "x.nii.gz".
    p = Path(local.LocalS3Client().CloudPath("s3://b/dir/x?versionId=abc"))
    assert p.wrapped.key == "dir/x?versionId=abc"
    assert p.name == "x?versionId=abc"


# -- local paths -----------------------------------------------------------
@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize("name", ["x?.nii", "x#1.nii", "a?b#c.txt"])
def test_a_local_name_keeps_question_mark_and_hash(
    cls: type, name: str, tmp_path: object
) -> None:
    p = cls(str(tmp_path)) / name
    assert p.name == name
    assert cls(f"/tmp/{name}").name == name
    assert str(cls(f"/tmp/{name}").parent) == "/tmp"


# -- the protocol trait ----------------------------------------------------
def test_query_true_opts_a_scheme_in() -> None:
    register_protocol("qon", absolute=True, query=True)
    assert traits_for("qon").query is True
    p = PurePath("qon://dir/a.txt?b=1")
    assert p.name == "a.txt"
    assert str(p) == "qon://dir/a.txt?b=1"


def test_set_storage_options_keeps_the_query_trait() -> None:
    register_protocol("qkeep", absolute=True, query=True)
    set_storage_options("qkeep", {"k": "v"})
    assert traits_for("qkeep").query is True
    assert PurePath("qkeep://dir/a?b").name == "a"


def test_query_trait_defaults_off_and_shows_in_repr() -> None:
    assert ProtocolTraits().query is False
    assert traits_for("an-unregistered-scheme").query is False
    assert PurePath("an-unregistered-scheme://d/a?b").name == "a?b"
    assert traits_for("http").query is True
    assert traits_for("https").query is True
    assert "query=True" in repr(ProtocolTraits(query=True))


@pytest.mark.parametrize(
    "text, bare",
    [
        ("https://h/x?a=1", "https://h/x"),
        ("https://h/x#f", "https://h/x"),
        ("https://h/x#f?a", "https://h/x"),
        ("https://h/x?a#f", "https://h/x"),
        ("h/x?a", "h/x"),
        ("https://h/x", "https://h/x"),
    ],
)
def test_strip_url_query(text: str, bare: str) -> None:
    assert strip_url_query(text) == bare


# -- drivers ---------------------------------------------------------------
class _NoSegments:
    """A non-local driver that cannot rebuild itself from a string."""

    protocol = "https"
    name = "k?v=1"

    def __str__(self) -> str:
        return "https://h/k?v=1"

    def __fspath__(self) -> str:  # pragma: no cover - never called
        return str(self)


def test_a_driver_without_with_segments_keeps_its_own_answer() -> None:
    # Rebuilding it some other way could drop its configuration, so the
    # driver's own name is returned rather than a guessed one.
    assert Path(_NoSegments()).name == "k?v=1"


def test_upath_derived_path_keeps_storage_options() -> None:
    pytest.importorskip("upath")
    # universal-pathlib reads the version id itself (it turns version_aware
    # on), so the selector is part of what reaches the object.
    p = Path("s3://bucket/dir/k.txt?versionId=1", storage_options={"anon": 1})
    assert type(p.wrapped).__name__ == "S3Path"
    assert p.wrapped.storage_options["version_aware"] is True
    assert str(p) == "s3://bucket/dir/k.txt?versionId=1"
    assert p.name == "k.txt"
    assert p.parent.storage_options["anon"] == 1
    assert (p.parent / "y").storage_options["anon"] == 1
