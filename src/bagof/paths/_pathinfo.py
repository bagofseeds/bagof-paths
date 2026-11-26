"""`pathinfo`: what a path is, as known at one moment.

`Path.pathinfo` and `AsyncPath.pathinfo` return one of the two front-ends
here, `PathInfo` (plain methods) or `AsyncPathInfo` (coroutines). Both read a
`Facts` record: the answers known about one path, kept once obtained.

A record is filled in one of two ways:

- **from a listing** -- the children `iterdir`, `glob`/`rglob` and `walk`
  yield, where the driver lists with detail (an fsspec info dict, an
  `os.DirEntry`, pathlib's own `DirEntryInfo`). Its answers cost nothing more.
- **by asking the store**, the first time an answer is needed and no listing
  gave it. The driver adapter answers with as few calls as its driver allows
  (`os.lstat`, `fs.info`); anything else is asked through the path's own
  status members, one question at a time.

Six facts are kept, all as seen *through* symbolic links: `symlink` (the
path itself is a link), `exists`, `dir`, `file`, `size` and `mtime`. A
question about the link itself (`follow_symlinks=False`) is answered from
them: a link exists, and is neither a directory nor a file.
"""

from __future__ import annotations

import datetime
import os
import stat as _stat

import typing_extensions as tx

from ._errors import UnsupportedPathOperation

# A fact no listing or query has given yet.
UNKNOWN: tx.Any = object()

# What a path that is not there answers. Also what a question settles to
# when its query could not say.
MISSING: tx.Dict[str, tx.Any] = {
    "symlink": False, "exists": False, "dir": False, "file": False,
    "size": None, "mtime": None,
}

# The facts a lazy listing entry answers itself (its type, as listed).
_ENTRY_KEYS = ("symlink", "dir", "file", "exists")

# fsspec backends name the modification time differently: local and gcsfs
# `mtime`, s3fs `LastModified`, adlfs `last_modified`, ... (the keys
# universal-pathlib's own stat reads). The in-memory store records only the
# time a file was written, as `created`.
_MTIME_KEYS = (
    "mtime", "LastModified", "last_modified", "timeModified",
    "modificationTime", "modified_at", "modify", "created",
)


class Facts:
    """The answers known about one path (internal).

    `listed` marks a record a directory listing gave; a walk trusts only
    those. `entry`, when set, is the listing's own lazy entry (an
    `os.DirEntry` or pathlib's `DirEntryInfo`), asked on demand: it caches
    its own answers, so asking it again costs nothing.
    """

    __slots__ = ("_known", "_entry", "listed")

    def __init__(
        self,
        known: tx.Optional[tx.Mapping[str, tx.Any]] = None,
        *,
        entry: tx.Any = None,
        listed: bool = False,
    ) -> None:
        self._known: tx.Dict[str, tx.Any] = dict(known or {})
        self._entry = entry
        self.listed = listed

    def _peek(self, key: str) -> tx.Any:
        value = self._known.get(key, UNKNOWN)
        if value is UNKNOWN and self._entry is not None:
            value = _from_entry(self._entry, key)
        return value

    def get(self, key: str) -> tx.Any:
        """A fact, or what the known ones imply, else `UNKNOWN`."""
        value = self._peek(key)
        if value is not UNKNOWN:
            return value
        if key not in ("exists", "symlink") and (
            self._peek("exists") is False
        ):
            return MISSING[key]
        if key == "exists" and (
            self._peek("dir") is True or self._peek("file") is True
        ):
            return True
        if key in ("file", "size") and self._peek("dir") is True:
            return MISSING[key]
        if key == "dir" and self._peek("file") is True:
            return False
        return UNKNOWN

    def learn(self, found: tx.Mapping[str, tx.Any]) -> None:
        """Keep new answers; an answer already known is never replaced.

        A lazy entry knows the type the listing saw, so a later query (for
        a size the entry cannot give) never overrides it.
        """
        for key, value in found.items():
            if self._entry is not None and key in _ENTRY_KEYS:
                continue
            self._known.setdefault(key, value)

    def settle(self, key: str) -> tx.Any:
        """A fact after its query: what it said, else the missing answer."""
        value = self.get(key)
        if value is UNKNOWN:
            value = self._known[key] = MISSING[key]
        return value

    def freeze(self) -> None:
        """Copy a lazy entry's type answers in, and drop the entry.

        Run where the listing ran (a worker thread, for `AsyncPath`), so an
        async caller never touches the entry: asking it can stat the file.
        Size and time stay unknown, to be asked the async way.
        """
        entry = self._entry
        if entry is None:
            return
        for key in _ENTRY_KEYS:
            self._known.setdefault(key, _from_entry(entry, key))
        self._entry = None


def _from_entry(entry: tx.Any, key: str) -> tx.Any:
    """A fact from a lazy listing entry, as pathlib 3.14's reads it."""
    try:
        if key == "symlink":
            return bool(entry.is_symlink())
        if key == "dir":
            return bool(entry.is_dir())
        if key == "file":
            return bool(entry.is_file())
        if key == "exists":
            # The listing saw the entry. Only a link can lead nowhere.
            if not entry.is_symlink():
                return True
            exists = getattr(entry, "exists", None)
            if callable(exists):  # pathlib's DirEntryInfo
                return bool(exists())
            entry.stat()
            return True
        # size / mtime: an os.DirEntry reads (and caches) its stat; pathlib's
        # DirEntryInfo has no public one, so the store is asked instead.
        read = getattr(entry, "stat", None)
        if not callable(read):
            return UNKNOWN
        if key == "size" and not entry.is_file():
            return None
        result = read()
    except (OSError, ValueError):
        return MISSING[key]
    if key == "size":
        return result.st_size
    return result.st_mtime


# -- facts from what a store reports ------------------------------------------
def info_facts(info: tx.Mapping[str, tx.Any]) -> tx.Dict[str, tx.Any]:
    """The facts in an fsspec info dict.

    A symbolic link is reported as the link itself (fsspec's local listing
    calls a link to a directory "other"), so only `symlink` is taken from
    it; what it leads to is asked.
    """
    if info.get("islink"):
        return {"symlink": True}
    kind = info.get("type")
    is_file = kind == "file"
    size = info.get("size") if is_file else None
    return {
        "symlink": False,
        "exists": True,
        "dir": kind == "directory",
        "file": is_file,
        "size": int(size) if isinstance(size, (int, float)) else None,
        "mtime": _mtime(info),
    }


def listed_facts(entry: tx.Any) -> tx.Optional[Facts]:
    """The record for one entry of an fsspec detailed listing, or None.

    A listing that ignored `detail` (a name, not a dict), or an entry that
    does not say its type, gives none.
    """
    if not isinstance(entry, dict) or "type" not in entry:
        return None
    return Facts(info_facts(entry), listed=True)


def _mtime(info: tx.Mapping[str, tx.Any]) -> tx.Optional[float]:
    for key in _MTIME_KEYS:
        value = info.get(key)
        if value is None:
            continue
        stamp = _timestamp(value)
        if stamp is not None:
            return stamp
    return None


def _timestamp(value: tx.Any) -> tx.Optional[float]:
    """A POSIX timestamp from a number, a datetime or an ISO 8601 string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:  # a store's naive time is UTC
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.timestamp()
    return None


def stat_facts(name: tx.Any) -> tx.Dict[str, tx.Any]:
    """The facts about a local file name, from `os.lstat` (and `os.stat`
    for a symbolic link). An error reads as "not there", as pathlib 3.14's
    status checks read it."""
    try:
        result = os.lstat(name)
    except (OSError, ValueError):
        return dict(MISSING)
    link = _stat.S_ISLNK(result.st_mode)
    if link:
        try:
            result = os.stat(name)
        except (OSError, ValueError):
            return dict(MISSING, symlink=True)
    is_file = _stat.S_ISREG(result.st_mode)
    return {
        "symlink": link,
        "exists": True,
        "dir": _stat.S_ISDIR(result.st_mode),
        "file": is_file,
        "size": result.st_size if is_file else None,
        "mtime": result.st_mtime,
    }


# -- facts asked through a path's own status members --------------------------
_STATUS = {"exists": "exists", "dir": "is_dir", "file": "is_file"}


def _from_stat(result: tx.Any, is_file: bool) -> tx.Dict[str, tx.Any]:
    size = getattr(result, "st_size", None)
    mtime = getattr(result, "st_mtime", None)
    return {
        "size": int(size) if is_file and isinstance(size, int) else None,
        "mtime": float(mtime) if isinstance(mtime, (int, float)) else None,
    }


def ask(path: tx.Any, key: str, facts: Facts) -> tx.Dict[str, tx.Any]:
    """One fact, asked through a synchronous path's own members.

    For a driver with no faster way: one status call per question, and one
    `stat()` for size and time. A store without symbolic links (no
    `is_symlink`) has none; a store with no `stat()` reports no size.
    """
    if key == "symlink":
        try:
            return {"symlink": bool(path.is_symlink())}
        except (UnsupportedPathOperation, OSError, ValueError):
            return {"symlink": False}
    if key in _STATUS:
        try:
            return {key: bool(getattr(path, _STATUS[key])())}
        except (OSError, ValueError):
            return {key: False}
    found: tx.Dict[str, tx.Any] = {}
    is_file = facts.get("file")
    if is_file is UNKNOWN:
        found.update(ask(path, "file", facts))
        is_file = found["file"]
    try:
        result = path.stat()
    except (UnsupportedPathOperation, OSError, ValueError):
        return dict(found, size=None, mtime=None)
    found.update(_from_stat(result, is_file))
    return found


async def ask_async(
    path: tx.Any, key: str, facts: Facts
) -> tx.Dict[str, tx.Any]:
    """`ask`, through an `AsyncPath`'s own (awaited) members."""
    if key == "symlink":
        try:
            return {"symlink": bool(await path.is_symlink())}
        except (UnsupportedPathOperation, OSError, ValueError):
            return {"symlink": False}
    if key in _STATUS:
        try:
            return {key: bool(await getattr(path, _STATUS[key])())}
        except (OSError, ValueError):
            return {key: False}
    found: tx.Dict[str, tx.Any] = {}
    is_file = facts.get("file")
    if is_file is UNKNOWN:
        found.update(await ask_async(path, "file", facts))
        is_file = found["file"]
    try:
        result = await path.stat()
    except (UnsupportedPathOperation, OSError, ValueError):
        return dict(found, size=None, mtime=None)
    found.update(_from_stat(result, is_file))
    return found


# -- the public front-ends ----------------------------------------------------
class PathInfo:
    """What a path is: a directory, a file, a link, its size and time.

    Read it from [`Path.pathinfo`][bagof.paths.Path.pathinfo]. Each answer
    is obtained once and then kept, so it says what the path was when it was
    obtained, as `pathlib`'s `Path.info` does from Python 3.14. A path that
    came from listing its directory (`iterdir`, `glob`, `rglob`, `walk`)
    already holds what the listing said, wherever the store reports it, at
    no extra cost. Ask the path itself (`Path.is_dir()`, `Path.exists()`)
    for a fresh answer.

    ```pycon
    >>> import tempfile
    >>> folder = Path(tempfile.mkdtemp())
    >>> (folder / "scan.nii").write_bytes(b"0123")
    4
    >>> (scan,) = folder.iterdir()
    >>> scan.pathinfo.is_file(), scan.pathinfo.size()
    (True, 4)
    >>> scan.unlink()
    >>> scan.pathinfo.exists(), scan.exists()
    (True, False)
    ```

    The same object on every driver and Python version. A path that
    cannot be reached answers False rather than raise, as `pathlib`'s
    status checks do from Python 3.14.
    """

    __slots__ = ("_path", "_facts")

    def __init__(self, path: tx.Any, facts: Facts) -> None:
        # `path` is a copy of the path the answers describe, holding none
        # itself, so the path and its info do not refer to each other.
        self._path = path
        self._facts = facts

    def _get(self, key: str) -> tx.Any:
        value = self._facts.get(key)
        if value is UNKNOWN:
            self._facts.learn(self._path._query_facts(key, self._facts))
            value = self._facts.settle(key)
        return value

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path existed.

        With `follow_symlinks=False`, a symbolic link exists even when what
        it leads to does not.
        """
        if not follow_symlinks and self._get("symlink"):
            return True
        return self._get("exists")

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path was a directory (or led to one)."""
        if not follow_symlinks and self._get("symlink"):
            return False
        return self._get("dir")

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path was a regular file (or led to one)."""
        if not follow_symlinks and self._get("symlink"):
            return False
        return self._get("file")

    def is_symlink(self) -> bool:
        """Whether the path was a symbolic link."""
        return self._get("symlink")

    def size(self) -> tx.Optional[int]:
        """The size of the file in bytes.

        None for a directory, a path that is not there, or a store that
        does not report sizes.
        """
        return self._get("size")

    def mtime(self) -> tx.Optional[float]:
        """When the file was last modified, as a POSIX timestamp.

        Seconds since the epoch, as `stat().st_mtime` gives them. None for
        a path that is not there or a store that does not report it (an
        object store reports none for a directory).
        """
        return self._get("mtime")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} of {self._path!r}>"


class AsyncPathInfo:
    """The `await` version of [`PathInfo`][bagof.paths.PathInfo].

    Read it from [`AsyncPath.pathinfo`][bagof.paths.AsyncPath.pathinfo].
    Every method is a coroutine; an answer the path already holds (from a
    listing, or asked before) returns without touching the store.

    ```pycon
    >>> import asyncio, tempfile
    >>> async def main() -> None:
    ...     folder = AsyncPath(tempfile.mkdtemp())
    ...     await (folder / "scan.nii").write_bytes(b"0123")
    ...     async for child in folder.iterdir():
    ...         print(child.name, await child.pathinfo.size())
    >>> asyncio.run(main())
    scan.nii 4
    ```
    """

    __slots__ = ("_path", "_facts")

    def __init__(self, path: tx.Any, facts: Facts) -> None:
        self._path = path
        self._facts = facts

    async def _get(self, key: str) -> tx.Any:
        value = self._facts.get(key)
        if value is UNKNOWN:
            found = await self._path._aquery_facts(key, self._facts)
            self._facts.learn(found)
            value = self._facts.settle(key)
        return value

    async def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path existed (see `PathInfo.exists`)."""
        if not follow_symlinks and await self._get("symlink"):
            return True
        return await self._get("exists")

    async def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path was a directory (or led to one)."""
        if not follow_symlinks and await self._get("symlink"):
            return False
        return await self._get("dir")

    async def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path was a regular file (or led to one)."""
        if not follow_symlinks and await self._get("symlink"):
            return False
        return await self._get("file")

    async def is_symlink(self) -> bool:
        """Whether the path was a symbolic link."""
        return await self._get("symlink")

    async def size(self) -> tx.Optional[int]:
        """The size of the file in bytes (see `PathInfo.size`)."""
        return await self._get("size")

    async def mtime(self) -> tx.Optional[float]:
        """When the file was last modified (see `PathInfo.mtime`)."""
        return await self._get("mtime")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} of {self._path!r}>"


InfoView = (PathInfo, AsyncPathInfo)
