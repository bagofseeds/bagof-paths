"""A URL's query is not part of its path (issue #17).

A `?` in a remote URL starts its query, on every scheme and every driver. The
lexical members (`name`, `suffix`, `parts`, `parent`, ...) leave it out, so a
name moves from one store to another (a cloud input to a local output) without
a token or a version id riding along. Only an `http(s)` URL also has a
fragment, from its first `#`; elsewhere `#` is an ordinary character of a name
(`Invoice #12.pdf`), as every store library reads it. The string of the path
keeps everything, since I/O needs it. A derived path names a different object,
so it does not carry the query. A local path has no query: a `?` or `#` in a
local name is an ordinary character. A scheme whose keys hold `?` literally
opts out with `register_protocol(..., query=False)`.
"""

import os
import pathlib
import sys

import pytest
import typing_extensions as tx

from bagof.paths import (
    AsyncPath,
    Path,
    ProtocolTraits,
    PurePath,
    UnsupportedPathOperation,
    register_protocol,
    set_storage_options,
)
from bagof.paths._protocols import strip_url_query, traits_for

FRONTS = [PurePath, Path, AsyncPath]


# -- the lexical members leave the query and the fragment out --------------
@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "url, name, suffix, parent",
    [
        ("https://h/dir/x.nii.gz?token=1", "x.nii.gz", ".gz", "https://h/dir"),
        ("https://h/dir/x.nii.gz#part", "x.nii.gz", ".gz", "https://h/dir"),
        ("http://h/dir/x.nii.gz?token=1", "x.nii.gz", ".gz", "http://h/dir"),
        # A slash inside the query is not a separator of the path.
        ("https://h/dir/x.nii.gz?sig=a/b.zip", "x.nii.gz", ".gz",
         "https://h/dir"),
        # A "?" inside the fragment belongs to the fragment.
        ("https://h/dir/x.nii.gz#a?b/c", "x.nii.gz", ".gz", "https://h/dir"),
        ("s3://b/dir/store.zarr?version=2", "store.zarr", ".zarr",
         "s3://b/dir"),
        ("s3://b/dir/x.nii.gz?versionId=abc", "x.nii.gz", ".gz",
         "s3://b/dir"),
        ("s3://b/dir/x.nii.gz?sig=a/b.zip", "x.nii.gz", ".gz", "s3://b/dir"),
        ("gs://b/dir/x.nii.gz?generation=123", "x.nii.gz", ".gz",
         "gs://b/dir"),
        ("gs://b/dir/k.txt?x=1#f", "k.txt", ".txt", "gs://b/dir"),
        ("memory://dir/x.nii.gz?q=1#f", "x.nii.gz", ".gz", "memory://dir"),
    ],
)
def test_lexical_members_leave_out_the_query(
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
@pytest.mark.parametrize(
    "url",
    ["https://h/dir/x.nii.gz?token=1#part", "s3://b/dir/x.nii.gz?version=2"],
)
def test_the_path_itself_keeps_the_query(cls: type, url: str) -> None:
    p = cls(url)
    assert str(p) == url
    assert str(p.wrapped) == url


@pytest.mark.parametrize("cls", FRONTS)
def test_suffixes_and_match_leave_out_the_query(cls: type) -> None:
    p = cls("s3://b/dir/x.nii.gz?token=1")
    assert p.suffixes == [".nii", ".gz"]
    assert p.match("*.nii.gz")
    assert p.full_match("**/x.nii.gz")
    assert not p.match("*token*")


@pytest.mark.parametrize("cls", FRONTS)
def test_a_derived_path_drops_the_query(cls: type) -> None:
    # As cloudpathlib's HttpPath and urljoin do: a derived path names a
    # different object, so the query does not ride onto it.
    p = cls("https://h/dir/x.nii.gz?token=1")
    assert str(p.with_suffix(".json")) == "https://h/dir/x.nii.json"
    assert str(p.with_name("y.txt")) == "https://h/dir/y.txt"
    assert str(p.with_stem("y")) == "https://h/dir/y.gz"
    base = cls("s3://b/dir?versionId=1")
    assert str(base / "x.txt") == "s3://b/dir/x.txt"
    assert str(base.joinpath("a", "x.txt")) == "s3://b/dir/a/x.txt"


@pytest.mark.parametrize("cls", FRONTS)
def test_a_joined_query_is_kept_and_left_out_of_the_name(cls: type) -> None:
    p = cls("https://h/dir") / "x.nii.gz?token=1"
    assert str(p) == "https://h/dir/x.nii.gz?token=1"
    assert p.name == "x.nii.gz"


@pytest.mark.parametrize("cls", FRONTS)
def test_relative_to_leaves_out_both_queries(cls: type) -> None:
    p = cls("https://h/dir/x.nii.gz?token=1")
    assert str(p.relative_to("https://h/dir?other=2")) == "x.nii.gz"
    assert str(p.relative_to(cls("https://h/dir#frag"))) == "x.nii.gz"
    assert p.is_relative_to(cls("https://h/dir?other=2"))
    assert p.is_relative_to("https://h/dir?other=2")


def test_a_name_moves_between_stores_without_the_query(
    tmp_path: object,
) -> None:
    # The use case: a local output named after a cloud input.
    remote = PurePath("s3://bucket/in/brain.nii.gz?versionId=abc")
    local = Path(str(tmp_path)) / remote.name
    assert local.name == "brain.nii.gz"
    assert local.with_suffix("").with_suffix(".mgz").name == "brain.mgz"


def test_cloudpathlib_name_leaves_out_the_query_too() -> None:
    local = pytest.importorskip("cloudpathlib.local")
    # cloudpathlib keeps "?versionId=abc" in its key, but the name is the
    # portable one, the same as over any other driver.
    p = Path(local.LocalS3Client().CloudPath("s3://b/dir/x?versionId=abc"))
    assert p.wrapped.key == "dir/x?versionId=abc"
    assert p.name == "x"


# -- "#" is a fragment only in an http URL ---------------------------------
@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "url, name",
    [
        ("s3://b/dir/Invoice #12.pdf", "Invoice #12.pdf"),
        ("s3://b/dir/x.nii.gz#f", "x.nii.gz#f"),
        # The query starts at the "?", after the "#" that is part of the name.
        ("s3://b/dir/x#a?b", "x#a"),
        # gcsfs's "#<generation>" shorthand is part of the name;
        # "?generation=" is the query spelling.
        ("gs://b/dir/x.nii.gz#123", "x.nii.gz#123"),
        ("memory://dir/x#1.txt?q=1", "x#1.txt"),
    ],
)
def test_hash_is_part_of_a_name_outside_http(
    cls: type, url: str, name: str
) -> None:
    p = cls(url)
    assert p.name == name
    assert str(p) == url


@pytest.mark.parametrize("cls", FRONTS)
def test_hash_starts_an_http_fragment(cls: type) -> None:
    p = cls("https://h/dir/Invoice #12.pdf")
    assert p.name == "Invoice "
    assert p.without_query().name == "Invoice "


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
def test_query_false_keeps_the_characters_in_the_name() -> None:
    register_protocol("qoff", absolute=True, query=False)
    assert traits_for("qoff").query is False
    p = PurePath("qoff://dir/a?b.txt")
    assert p.name == "a?b.txt"
    assert p.suffix == ".txt"
    assert p.without_query() is p


def test_set_storage_options_keeps_the_query_trait() -> None:
    register_protocol("qkeep", absolute=True, query=False)
    set_storage_options("qkeep", {"k": "v"})
    assert traits_for("qkeep").query is False
    assert PurePath("qkeep://dir/a?b").name == "a?b"


def test_fragment_trait_opts_a_scheme_in() -> None:
    register_protocol("fragon", absolute=True, fragment=True)
    assert traits_for("fragon").fragment is True
    assert PurePath("fragon://dir/a.txt#part").name == "a.txt"
    set_storage_options("fragon", {"k": "v"})
    assert traits_for("fragon").fragment is True


def test_query_trait_defaults_on_and_shows_in_repr() -> None:
    assert ProtocolTraits().query is True
    assert ProtocolTraits().fragment is False
    assert traits_for("s3").fragment is False
    assert traits_for("http").fragment is True
    assert traits_for("https").fragment is True
    assert "fragment=True" in repr(ProtocolTraits(fragment=True))
    assert traits_for("an-unregistered-scheme").query is True
    assert traits_for("s3").query is True
    assert PurePath("an-unregistered-scheme://d/x.txt?q=1").name == "x.txt"
    assert "query=False" in repr(ProtocolTraits(query=False))


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


@pytest.mark.parametrize(
    "text, query, fragment, bare",
    [
        ("s3://b/x#a?b", True, False, "s3://b/x#a"),
        ("s3://b/x#a?b", False, True, "s3://b/x"),
        ("s3://b/x#a?b", False, False, "s3://b/x#a?b"),
    ],
)
def test_strip_url_query_by_delimiter(
    text: str, query: bool, fragment: bool, bare: str
) -> None:
    assert strip_url_query(text, query=query, fragment=fragment) == bare


# -- without_query: the whole path without its query -----------------------
@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize(
    "url, bare",
    [
        ("s3://b/dir/store.zarr?version=2", "s3://b/dir/store.zarr"),
        ("s3://b/dir/x?sig=a/b.zip", "s3://b/dir/x"),
        ("gs://b/dir/x.nii.gz?x=1#part", "gs://b/dir/x.nii.gz"),
        ("memory://dir/x.nii.gz?q=1#f", "memory://dir/x.nii.gz"),
        ("https://h/dir/x.nii.gz?token=1#f", "https://h/dir/x.nii.gz"),
    ],
)
def test_without_query(cls: type, url: str, bare: str) -> None:
    p = cls(url)
    q = p.without_query()
    assert type(q) is cls
    assert str(q) == bare
    assert q.name == p.name
    assert q.without_query() == q
    assert str(p) == url  # the original is untouched


@pytest.mark.parametrize("cls", FRONTS)
def test_without_query_leaves_a_plain_url_and_a_local_path(cls: type) -> None:
    p = cls("s3://b/dir/x.txt")
    assert p.without_query() is p
    local = cls("/tmp/x?.nii")
    assert local.without_query() is local


def test_without_query_is_listed_as_supported() -> None:
    p = PurePath("s3://b/k?v=1")
    assert p.supports("without_query")
    assert "without_query" in p.capabilities()


def test_without_query_carries_subclass_state() -> None:
    class Tagged(PurePath):
        pass

    p = Tagged("https://h/x?t=1")
    p.tag = "kept"
    assert p.without_query().tag == "kept"


# -- drivers ---------------------------------------------------------------
class _NoSegments:
    """A non-local driver that cannot rebuild itself from a string."""

    protocol = "https"
    name = "k?v=1"

    def __str__(self) -> str:
        return "https://h/k?v=1"

    def __fspath__(self) -> str:  # pragma: no cover - never called
        return str(self)


def test_a_driver_without_with_segments_keeps_its_own_name() -> None:
    # Rebuilding it some other way could drop its configuration, so the
    # driver's own name is returned rather than a guessed one.
    assert Path(_NoSegments()).name == "k?v=1"


def test_without_query_refuses_a_driver_it_cannot_rebuild() -> None:
    with pytest.raises(UnsupportedPathOperation, match="without_query"):
        Path(_NoSegments()).without_query()


def test_upath_derived_path_keeps_storage_options() -> None:
    pytest.importorskip("upath")
    # universal-pathlib reads the version id itself (it turns version_aware
    # on), so the query is part of what reaches the object.
    p = Path("s3://bucket/dir/k.txt?versionId=1", storage_options={"anon": 1})
    assert type(p.wrapped).__name__ == "S3Path"
    assert p.wrapped.storage_options["version_aware"] is True
    assert str(p) == "s3://bucket/dir/k.txt?versionId=1"
    assert p.name == "k.txt"
    assert p.parent.storage_options["anon"] == 1
    assert (p.parent / "y").storage_options["anon"] == 1
    assert p.without_query().storage_options["anon"] == 1


def test_fsspec_chain_leaves_out_the_query() -> None:
    pytest.importorskip("upath")
    p = Path("simplecache::s3://b/dir/x.nii.gz?v=1")
    assert p.name == "x.nii.gz"


# -- file: and local: URLs -------------------------------------------------
# A file URI is a URL (RFC 8089): pathlib reads it so from Python 3.14 on,
# and the wrapper does on every version. A "?" or "#" in a file name is
# written %3F / %23, as as_uri() writes it.
@pytest.mark.parametrize(
    "uri, path",
    [
        ("file:///tmp/x?.nii", "/tmp/x"),
        ("file:///tmp/x.nii?q=1#f", "/tmp/x.nii"),
        ("file:///tmp/x%3F.nii", "/tmp/x?.nii"),
        ("file:///tmp/x%23.nii", "/tmp/x#.nii"),
        ("file://localhost/tmp/x", "/tmp/x"),
        ("FILE:///tmp/x", "/tmp/x"),
        ("file:///tmp/a%20b", "/tmp/a b"),
        ("file:////tmp/x", "//tmp/x"),
    ],
)
def test_file_uri_is_read_as_a_url(uri: str, path: str) -> None:
    if os.name == "nt":  # pragma: no cover - POSIX spellings
        pytest.skip("POSIX paths")
    assert str(Path(uri)) == path
    assert str(Path.from_uri(uri)) == path
    assert str(PurePath(uri)) == path


def test_file_uri_round_trips_a_name_with_question_mark_and_hash(
    tmp_path: object,
) -> None:
    local = pathlib.Path(str(tmp_path)) / "x?#%.nii"
    assert Path(local.as_uri()) == Path(local)
    assert Path.from_uri(local.as_uri()).name == "x?#%.nii"


@pytest.mark.parametrize("uri", ["file://elsewhere/share/x", "file:rel"])
def test_file_uri_that_is_not_a_local_absolute_path_is_refused(
    uri: str,
) -> None:
    if os.name == "nt" and "elsewhere" in uri:  # pragma: no cover
        pytest.skip("a UNC path on Windows")
    with pytest.raises(ValueError):
        Path.from_uri(uri)
    if "://" in uri:
        with pytest.raises(ValueError):
            Path(uri)


def test_local_url_leaves_out_the_query() -> None:
    # local:// is a URL like file://: its query and fragment are not part of
    # the path. What remains is read as fsspec reads it, undecoded.
    assert str(Path("local:///tmp/x?.nii")) == "/tmp/x"
    assert str(Path("local:///tmp/a%20b?q=1")) == "/tmp/a%20b"
    assert str(Path("local:///tmp/Track #2.mp3")) == "/tmp/Track #2.mp3"
    assert str(Path("local://tmp/thing")) == "tmp/thing"


_FILE_URIS = [
    "file:///tmp/x?.nii", "file:///tmp/x.nii?q=1#f", "file:///tmp/x%3F.nii",
    "file://localhost/tmp/x", "FILE:///tmp/x", "file:/tmp/x",
    "file:///tmp/a%20b", "file://elsewhere/share/x", "file:rel", "/tmp/x",
    "file:///tmp/%C3%A9", "file:///tmp/%FF", "file:////tmp/x", "file://",
    "file:///", "file:///tmp/x#", "file:///tmp/a;b", "file:///tmp/a%2Fb",
    "file:", "http://h/x",
]


@pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="the reference is Python 3.14's Path.from_uri",
)
@pytest.mark.parametrize("uri", _FILE_URIS)
def test_file_uri_matches_python_314_from_uri(uri: str) -> None:
    from bagof.paths._base import _local_from_file_uri

    def read(func: object) -> str:
        try:
            return str(func(uri))
        except ValueError:
            return "ValueError"

    assert read(_local_from_file_uri) == read(pathlib.Path.from_uri)


# -- copy_into / move_into / walk: the store's names inside a store --------
@pytest.fixture
def _memory_tree() -> tx.Iterator[Path]:
    pytest.importorskip("upath")
    from fsspec.implementations.memory import MemoryFileSystem

    MemoryFileSystem.store.clear()
    src = Path("memory://b/src")
    for key, data in [("x?v=1", b"one"), ("x?v=2", b"two"),
                      ("d?s=a/f.txt", b"f")]:
        (src / key).write_bytes(data)
    yield src
    MemoryFileSystem.store.clear()


def test_copy_into_the_same_store_keeps_objects_apart(
    _memory_tree: Path,
) -> None:
    # Regression: with the portable name, both copies landed on "dst/x" and
    # the second silently overwrote the first.
    src, dst = _memory_tree, Path("memory://b/dst")
    for key in ("x?v=1", "x?v=2"):
        (src / key).copy_into(dst)
    assert sorted(p.read_bytes() for p in dst.iterdir()) == [b"one", b"two"]
    assert (dst / "x?v=1").read_bytes() == b"one"


def test_move_into_the_same_store_keeps_the_store_name(
    _memory_tree: Path,
) -> None:
    src, dst = _memory_tree, Path("memory://b/dst")
    moved = (src / "x?v=2").move_into(dst)
    assert str(moved) == "memory://b/dst/x?v=2"
    assert moved.read_bytes() == b"two"


def test_async_copy_into_the_same_store_keeps_objects_apart(
    _memory_tree: Path,
) -> None:
    import asyncio

    async def go() -> list:
        dst = AsyncPath("memory://b/adst")
        for key in ("x?v=1", "x?v=2"):
            await AsyncPath(str(_memory_tree / key)).copy_into(dst)
        return sorted([await p.read_bytes() async for p in dst.iterdir()])

    assert asyncio.run(go()) == [b"one", b"two"]


def test_copy_into_another_store_takes_the_portable_name(
    _memory_tree: Path, tmp_path: object
) -> None:
    out = (_memory_tree / "x?v=1").copy_into(Path(str(tmp_path)))
    assert out.name == "x"
    assert (pathlib.Path(str(tmp_path)) / "x").read_bytes() == b"one"


def test_into_from_local_keeps_a_local_name(_memory_tree: Path) -> None:
    # A local name has no query: its "?" is a character, and it travels.
    local = Path("/tmp/y?.txt")
    target = local._into(Path("memory://b/dst"))
    assert str(target) == "memory://b/dst/y?.txt"


def test_walk_fallback_reports_the_store_names(_memory_tree: Path) -> None:
    from bagof.paths._drivers import GenericAdapter

    walked = [
        (str(d), sorted(files))
        for d, _, files in GenericAdapter()._walk_fallback(
            _memory_tree, top_down=True, on_error=None, follow_symlinks=False
        )
    ]
    native = [(str(d), sorted(files)) for d, _, files in _memory_tree.walk()]
    assert walked == native
    assert ("memory://b/src", ["x?v=1", "x?v=2"]) in walked
    assert ("memory://b/src/d?s=a", ["f.txt"]) in walked


def test_same_store_folds_aliases_and_local() -> None:
    pytest.importorskip("upath")
    s3 = Path("s3://b/x", storage_options={"anon": True})
    assert s3._same_store(Path("s3a://c/y", storage_options={"anon": True})
                          .wrapped)
    assert not s3._same_store(pathlib.Path("/tmp"))
    assert Path("/tmp/x")._same_store(pathlib.Path("/elsewhere"))
