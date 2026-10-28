"""`samefile(other, strict=False)`: the same file, existing or not (#50).

A writer checks it before replacing the file it reads, when the file it is
about to write may not exist yet. `strict=True` (the default) is pathlib's
own `samefile`: both files must exist.
"""

import asyncio
import os
import pathlib

import pytest

from bagof.paths import AsyncPath, Path, PurePath

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic links")


def test_the_default_is_pathlibs_samefile(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError):
        Path(tmp_path / "new.bin").samefile(tmp_path / "new.bin")


def test_new_files_by_their_spelling(tmp_path: pathlib.Path) -> None:
    (tmp_path / "sub").mkdir()
    new = Path(tmp_path) / "new.bin"
    assert new.samefile(new, strict=False) is True
    assert new.samefile(tmp_path / "sub" / ".." / "new.bin", strict=False)
    assert new.samefile(str(tmp_path / "new.bin"), strict=False)
    assert new.samefile(PurePath(tmp_path / "new.bin"), strict=False)
    assert not new.samefile(tmp_path / "other.bin", strict=False)
    assert not new.samefile(tmp_path / "sub" / "new.bin", strict=False)
    # Below a directory that does not exist either.
    deep = Path(tmp_path) / "a" / "b" / "c.bin"
    assert deep.samefile(tmp_path / "a" / "b" / "c.bin", strict=False)
    assert not deep.samefile(tmp_path / "a" / "c.bin", strict=False)


def test_a_relative_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert Path("new.bin").samefile(tmp_path / "new.bin", strict=False)


def test_existing_files(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a").write_bytes(b"a")
    (tmp_path / "b").write_bytes(b"b")
    a = Path(tmp_path / "a")
    assert a.samefile(tmp_path / "a", strict=False)
    assert not a.samefile(tmp_path / "b", strict=False)
    # One exists, the other does not.
    assert not a.samefile(tmp_path / "c", strict=False)
    assert not Path(tmp_path / "c").samefile(a, strict=False)


def test_a_hard_link_is_the_same_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a").write_bytes(b"a")
    try:
        os.link(str(tmp_path / "a"), str(tmp_path / "b"))
    except OSError:  # pragma: no cover - a file system without hard links
        pytest.skip("no hard links here")
    assert Path(tmp_path / "a").samefile(tmp_path / "b", strict=False)


@POSIX
def test_symbolic_links_are_followed(tmp_path: pathlib.Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    new = Path(tmp_path / "real" / "new.bin")
    assert new.samefile(tmp_path / "link" / "new.bin", strict=False)
    # A link to a file that does not exist yet leads to that file.
    (tmp_path / "dangling").symlink_to(tmp_path / "real" / "new.bin")
    assert new.samefile(tmp_path / "dangling", strict=False)


def test_remote_paths_are_the_same_when_equal() -> None:
    # Lexical: no backend is needed, and nothing is read.
    p = Path("s3://bucket/key.bin")
    assert p.samefile("s3://bucket/key.bin", strict=False)
    assert p.samefile(PurePath("s3a://bucket/key.bin"), strict=False)
    assert not p.samefile("s3://bucket/other.bin", strict=False)
    assert not p.samefile("gs://bucket/key.bin", strict=False)
    assert not p.samefile("bucket/key.bin", strict=False)


def test_a_local_path_is_never_a_remote_one(tmp_path: pathlib.Path) -> None:
    local = Path(tmp_path / "key.bin")
    assert not local.samefile("s3://bucket/key.bin", strict=False)


def test_async(tmp_path: pathlib.Path) -> None:
    (tmp_path / "sub").mkdir()

    async def go() -> None:
        new = AsyncPath(tmp_path / "new.bin")
        assert await new.samefile(
            tmp_path / "sub" / ".." / "new.bin", strict=False
        )
        assert await new.samefile(Path(tmp_path / "new.bin"), strict=False)
        assert not await new.samefile(tmp_path / "other.bin", strict=False)
        with pytest.raises(FileNotFoundError):
            await new.samefile(tmp_path / "new.bin")
        remote = AsyncPath("s3://bucket/key.bin")
        assert await remote.samefile("s3://bucket/key.bin", strict=False)

    asyncio.run(go())
