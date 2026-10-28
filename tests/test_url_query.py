"""A URL's query and fragment are not part of its path (issue #17).

The lexical members (`name`, `suffix`, `parts`, `parent`, ...) leave out the
query and the fragment of a URL, on every front-end and every driver. The
string of the path keeps them, since I/O may need them (a token, a version
id). A derived path names a different object, so it does not carry them, as in
URL resolution (RFC 3986). A local path has no query: a `?` or `#` in a local
name is an ordinary character.
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
from bagof.paths._protocols import strip_query, traits_for

FRONTS = [PurePath, Path, AsyncPath]


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
        ("s3://bucket/dir/store.zarr?version=2", "store.zarr", ".zarr",
         "s3://bucket/dir"),
        ("gs://bucket/dir/k.txt?x=1#f", "k.txt", ".txt", "gs://bucket/dir"),
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
def test_the_path_itself_keeps_the_query(cls: type) -> None:
    url = "https://h/dir/x.nii.gz?token=1#part"
    p = cls(url)
    assert str(p) == url
    assert str(p.wrapped) == url
    assert p.as_posix() == url


@pytest.mark.parametrize("cls", FRONTS)
def test_suffixes_and_match_leave_out_the_query(cls: type) -> None:
    p = cls("https://h/dir/x.nii.gz?token=1")
    assert p.suffixes == [".nii", ".gz"]
    assert p.match("*.nii.gz")
    assert p.full_match("**/x.nii.gz")
    assert not p.match("*token*")


@pytest.mark.parametrize("cls", FRONTS)
def test_a_derived_path_drops_the_query(cls: type) -> None:
    p = cls("https://h/dir/x.nii.gz?token=1")
    assert str(p.with_suffix(".json")) == "https://h/dir/x.nii.json"
    assert str(p.with_name("y.txt")) == "https://h/dir/y.txt"
    assert str(p.with_stem("y")) == "https://h/dir/y.gz"
    base = cls("https://h/dir?token=1")
    assert str(base / "x.txt") == "https://h/dir/x.txt"
    assert str(base.joinpath("a", "x.txt")) == "https://h/dir/a/x.txt"


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


@pytest.mark.parametrize("cls", FRONTS)
@pytest.mark.parametrize("name", ["x?.nii", "x#1.nii", "a?b#c.txt"])
def test_a_local_name_keeps_question_mark_and_hash(
    cls: type, name: str, tmp_path: object
) -> None:
    p = cls(str(tmp_path)) / name
    assert p.name == name
    assert cls(f"/tmp/{name}").name == name
    assert str(cls(f"/tmp/{name}").parent) == "/tmp"


def test_query_false_keeps_the_characters_in_the_path() -> None:
    register_protocol("qoff", absolute=True, query=False)
    assert traits_for("qoff").query is False
    p = PurePath("qoff://dir/a?b.txt")
    assert p.name == "a?b.txt"
    assert p.suffix == ".txt"


def test_set_storage_options_keeps_the_query_trait() -> None:
    register_protocol("qkeep", absolute=True, query=False)
    set_storage_options("qkeep", {"k": "v"})
    assert traits_for("qkeep").query is False
    assert PurePath("qkeep://dir/a?b").name == "a?b"


def test_query_trait_defaults_on_and_shows_in_repr() -> None:
    assert ProtocolTraits().query is True
    assert traits_for("an-unregistered-scheme").query is True
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
def test_strip_query(text: str, bare: str) -> None:
    assert strip_query(text) == bare


class _NoSegments:
    """A non-local driver that cannot rebuild itself from a string."""

    protocol = "s3"
    name = "k?v=1"

    def __str__(self) -> str:
        return "s3://b/k?v=1"

    def __fspath__(self) -> str:  # pragma: no cover - never called
        return str(self)


def test_a_driver_without_with_segments_keeps_its_own_answer() -> None:
    # Rebuilding it some other way could drop its configuration, so the
    # driver's own name is returned rather than a guessed one.
    assert Path(_NoSegments()).name == "k?v=1"


def test_upath_derived_path_keeps_storage_options() -> None:
    pytest.importorskip("upath")
    # universal-pathlib reads the version id itself (it turns version_aware
    # on), so the query is part of what reaches the object.
    p = Path("s3://bucket/dir/k.txt?versionId=1", storage_options={"anon": 1})
    assert type(p.wrapped).__name__ == "S3Path"
    assert str(p) == "s3://bucket/dir/k.txt?versionId=1"
    assert p.name == "k.txt"
    assert p.parent.storage_options["anon"] == 1
    assert (p.parent / "y").storage_options["anon"] == 1
