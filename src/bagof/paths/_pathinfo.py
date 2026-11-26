"""`pathinfo`: what a path is, as known at one moment.

`Path.pathinfo` and `AsyncPath.pathinfo` return one of the two front-ends
here, `PathInfo` (plain methods) or `AsyncPathInfo` (coroutines). Both read a
`Facts` record: the answers known about one path, kept once obtained.

The answers come in two *views*, as pathlib 3.14's own `Path.info` keeps
them:

- `LSTAT`, the path itself (not following a symbolic link): whether it
  exists, is a link, a directory or a file. A listing (`iterdir`, `glob`,
  `rglob`, `walk`) fills it for the entries it yields, at no extra cost,
  from what the listing said (an `os.DirEntry`, pathlib's `DirEntryInfo`,
  an fsspec info dict, a cloud listing).
- `STAT`, what the path leads to: whether that exists, is a directory or a
  file, its size and its modification time. It is read from the store the
  first time one of its answers is asked, and kept.

Which view answers which question is pathlib 3.14's rule. A listed entry
answers `is_dir()`/`is_file()` from the listing unless it is a link, and
`exists()` (which follows links) from the `STAT` view; an entry that was
not listed reads each view on its first question. `size()` and `mtime()`
come from the `STAT` view, with `exists()`.

A store answers a view with as few calls as its driver allows (`os.stat` /
`os.lstat`, one `fs.info`); anything else is asked through the path's own
status members, one question at a time.
"""

from __future__ import annotations

import datetime
import os
import stat as _stat

import typing_extensions as tx

from ._errors import UnsupportedPathOperation

# A fact no listing or query has given yet.
UNKNOWN: tx.Any = object()

# The two views (see the module docstring).
LSTAT = "lstat"
STAT = "stat"

# What a path that is not there answers, per view. Also what a question
# settles to when its query could not say.
MISSING: tx.Dict[str, tx.Dict[str, tx.Any]] = {
    LSTAT: {"exists": False, "symlink": False, "dir": False, "file": False},
    STAT: {
        "exists": False, "dir": False, "file": False,
        "size": None, "mtime": None,
    },
}

# fsspec backends name the modification time differently: local and gcsfs
# `mtime`, s3fs `LastModified`, adlfs `last_modified`, ... in the order of
# universal-pathlib's own stat (MIT; NOTICE.md). The in-memory store records
# only the time a file was written, as `created`.
_MTIME_KEYS = (
    "mtime", "LastModified", "last_modified", "timeModified",
    "modificationTime", "modified_at", "modify", "created",
)


class Facts:
    """The answers known about one path, in its two views (internal).

    `listed` marks a record a directory listing gave: its `LSTAT` view is
    what the listing said. `entry`, when set, is the listing's own lazy
    entry (an `os.DirEntry` or pathlib's `DirEntryInfo`), asked on demand
    for that view; it caches its own answers.
    """

    __slots__ = ("_views", "_entry", "listed")

    def __init__(
        self,
        lstat: tx.Optional[tx.Mapping[str, tx.Any]] = None,
        *,
        entry: tx.Any = None,
        listed: bool = False,
    ) -> None:
        self._views: tx.Dict[str, tx.Dict[str, tx.Any]] = {
            LSTAT: dict(lstat or {}), STAT: {},
        }
        self._entry = entry
        self.listed = listed

    def _peek(self, view: str, key: str) -> tx.Any:
        value = self._views[view].get(key, UNKNOWN)
        if value is UNKNOWN and view == LSTAT and self._entry is not None:
            value = _from_entry(self._entry, key)
        return value

    def get(self, view: str, key: str) -> tx.Any:
        """A fact, or what the known ones of its view imply, else UNKNOWN."""
        value = self._peek(view, key)
        if value is not UNKNOWN:
            return value

        def known(name: str) -> tx.Any:
            return self._peek(view, name)

        if key != "exists" and known("exists") is False:
            return MISSING[view][key]
        if key == "exists" and (
            known("dir") is True or known("file") is True
            or (view == LSTAT and known("symlink") is True)
        ):
            return True
        if view == LSTAT and key in ("dir", "file") and (
            known("symlink") is True
        ):
            return False
        if key in ("file", "size") and known("dir") is True:
            return MISSING[view][key]
        if key == "dir" and known("file") is True:
            return False
        return UNKNOWN

    def learn(self, view: str, found: tx.Mapping[str, tx.Any]) -> None:
        """Keep new answers; an answer already known is never replaced.

        A listing's own entry answers every `LSTAT` question, so that view
        is never queried while it is held.
        """
        known = self._views[view]
        for key, value in found.items():
            known.setdefault(key, value)

    def keep(
        self, view: str, key: str, found: tx.Mapping[str, tx.Any]
    ) -> tx.Any:
        """Keep what a query found, and answer `key` from it.

        What the query could not say reads as "not there". A listed entry
        does not keep a `STAT` read that found nothing, as an `os.DirEntry`
        keeps no failed stat: the next question asks again.
        """
        if self.listed and view == STAT and found.get("exists") is False:
            return MISSING[STAT][key]
        self.learn(view, found)
        value = self.get(view, key)
        if value is UNKNOWN:
            value = self._views[view][key] = MISSING[view][key]
        return value

    def freeze(self) -> None:
        """Copy a lazy entry's answers in, and drop the entry.

        Run where the listing ran (a worker thread, for `AsyncPath`), so an
        async caller never touches the entry, which can stat the file.
        """
        entry = self._entry
        if entry is None:
            return
        known = self._views[LSTAT]
        for key in MISSING[LSTAT]:
            known.setdefault(key, _from_entry(entry, key))
        self._entry = None


def _from_entry(entry: tx.Any, key: str) -> tx.Any:
    """An `LSTAT` fact from a lazy listing entry, as pathlib 3.14 reads it.

    `os.DirEntry` and pathlib's `DirEntryInfo` take the same keywords. The
    listing saw the entry, so it exists (`DirEntryInfo.exists` with
    `follow_symlinks=False` says so without asking).
    """
    if key == "exists":
        return True
    try:
        if key == "symlink":
            return bool(entry.is_symlink())
        if key == "dir":
            return bool(entry.is_dir(follow_symlinks=False))
        return bool(entry.is_file(follow_symlinks=False))
    except OSError:
        return False


# -- facts from what a store reports ------------------------------------------
def info_view(
    info: tx.Mapping[str, tx.Any], view: str
) -> tx.Dict[str, tx.Any]:
    """One view from an fsspec info dict.

    fsspec reports a symbolic link with `islink`; its local `info()` gives
    the type of what the link leads to, its listing the type of the link
    itself, so a link's `LSTAT` view takes neither.
    """
    kind = info.get("type")
    if view == LSTAT:
        link = bool(info.get("islink"))
        return {
            "exists": True, "symlink": link,
            "dir": not link and kind == "directory",
            "file": not link and kind == "file",
        }
    is_file = kind == "file"
    size = info.get("size") if is_file else None
    return {
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
    return Facts(info_view(entry, LSTAT), listed=True)


def typed_facts(is_dir: bool) -> Facts:
    """The record for an entry a listing classified as a directory or a
    file (a store without symbolic links: a cloud listing)."""
    return Facts(
        {"exists": True, "symlink": False, "dir": is_dir,
         "file": not is_dir},
        listed=True,
    )


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


def stat_view(name: tx.Any, view: str) -> tx.Dict[str, tx.Any]:
    """One view of a local file name: `os.lstat` for `LSTAT`, `os.stat`
    for `STAT`. An error reads as "not there", as pathlib 3.14 reads it."""
    try:
        result = os.lstat(name) if view == LSTAT else os.stat(name)
    except (OSError, ValueError):
        return {"exists": False}
    mode = result.st_mode
    if view == LSTAT:
        return {
            "exists": True, "symlink": _stat.S_ISLNK(mode),
            "dir": _stat.S_ISDIR(mode), "file": _stat.S_ISREG(mode),
        }
    is_file = _stat.S_ISREG(mode)
    return {
        "exists": True,
        "dir": _stat.S_ISDIR(mode),
        "file": is_file,
        "size": result.st_size if is_file else None,
        "mtime": result.st_mtime,
    }


# -- facts asked through a path's own status members --------------------------
_STATUS = {"exists": "exists", "dir": "is_dir", "file": "is_file"}
_LINK = {"exists": True, "symlink": True, "dir": False, "file": False}
# The type questions of a view are asked together, the one asked first, so
# its answers describe one moment as a single stat would.
_ORDER = {
    "exists": ("exists", "file", "dir"),
    "file": ("file", "dir", "exists"),
    "dir": ("dir", "file", "exists"),
}


def _type_key(key: str) -> str:
    """The type question a view's read starts from."""
    return key if key in _STATUS else "exists"


def _settled(name: str, value: bool) -> bool:
    """Whether the type answers asked so far settle the rest."""
    return value if name != "exists" else not value


def _from_stat(result: tx.Any, is_file: bool) -> tx.Dict[str, tx.Any]:
    size = getattr(result, "st_size", None)
    mtime = getattr(result, "st_mtime", None)
    return {
        "size": int(size) if is_file and isinstance(size, int) else None,
        "mtime": float(mtime) if isinstance(mtime, (int, float)) else None,
    }


def ask(
    path: tx.Any, view: str, key: str, facts: Facts
) -> tx.Dict[str, tx.Any]:
    """One view, asked through a synchronous path's own members.

    For a driver with no faster way: its type answers together (up to
    three status calls, the one asked first), and one `stat()` for size
    and time. A store without symbolic links (no `is_symlink`) has none;
    a path that is not a link reads its `LSTAT` view as a `STAT` one, at
    the moment it is asked.
    """
    if view == LSTAT:
        link = facts.get(LSTAT, "symlink")
        if link is UNKNOWN:
            try:
                link = bool(path.is_symlink())
            except (UnsupportedPathOperation, OSError, ValueError):
                link = False
        if link:
            return dict(_LINK)
        found: tx.Dict[str, tx.Any] = {"symlink": False}
        found.update(ask(path, STAT, _type_key(key), facts))
        return found
    if key in _STATUS:
        found = {}
        for name in _ORDER[key]:
            try:
                value = bool(getattr(path, _STATUS[name])())
            except (OSError, ValueError):
                value = False
            found[name] = value
            if _settled(name, value):
                break
        return found
    found = {}
    is_file = facts.get(STAT, "file")
    if is_file is UNKNOWN:
        found.update(ask(path, STAT, "file", facts))
        is_file = found["file"]
    try:
        result = path.stat()
    except (UnsupportedPathOperation, OSError, ValueError):
        return dict(found, size=None, mtime=None)
    found.update(_from_stat(result, is_file))
    return found


async def ask_async(
    path: tx.Any, view: str, key: str, facts: Facts
) -> tx.Dict[str, tx.Any]:
    """`ask`, through an `AsyncPath`'s own (awaited) members."""
    if view == LSTAT:
        link = facts.get(LSTAT, "symlink")
        if link is UNKNOWN:
            try:
                link = bool(await path.is_symlink())
            except (UnsupportedPathOperation, OSError, ValueError):
                link = False
        if link:
            return dict(_LINK)
        found: tx.Dict[str, tx.Any] = {"symlink": False}
        found.update(await ask_async(path, STAT, _type_key(key), facts))
        return found
    if key in _STATUS:
        found = {}
        for name in _ORDER[key]:
            try:
                value = bool(await getattr(path, _STATUS[name])())
            except (OSError, ValueError):
                value = False
            found[name] = value
            if _settled(name, value):
                break
        return found
    found = {}
    is_file = facts.get(STAT, "file")
    if is_file is UNKNOWN:
        found.update(await ask_async(path, STAT, "file", facts))
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

    Read it from [`Path.pathinfo`][bagof.paths.Path.pathinfo]. It answers
    as `pathlib`'s `Path.info` does from Python 3.14, on every driver and
    Python version: each answer is obtained once and then kept. A path that
    came from listing its directory (`iterdir`, `glob`, `rglob`, `walk`)
    already knows what the listing said it is, at no extra cost;
    `exists()`, `size()` and `mtime()` are read together the first time
    one of them is asked. Ask the path itself (`Path.is_dir()`,
    `Path.exists()`) for a fresh answer.

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

    A path that cannot be reached answers False rather than raise, as
    `pathlib`'s status checks do from Python 3.14.
    """

    __slots__ = ("_path", "_facts")

    def __init__(self, path: tx.Any, facts: Facts) -> None:
        # `path` is a copy of the path the answers describe, holding none
        # itself, so the path and its info do not refer to each other.
        self._path = path
        self._facts = facts

    def _get(self, view: str, key: str) -> tx.Any:
        value = self._facts.get(view, key)
        if value is UNKNOWN:
            found = self._path._query_facts(view, key, self._facts)
            value = self._facts.keep(view, key, found)
        return value

    def _type_view(self, follow_symlinks: bool) -> str:
        # A listed entry that is not a link is typed by its listing, as
        # pathlib 3.14's DirEntryInfo is; any other follows the path.
        if not follow_symlinks:
            return LSTAT
        if self._facts.listed and not self._get(LSTAT, "symlink"):
            return LSTAT
        return STAT

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists.

        With `follow_symlinks=False`, a symbolic link exists even when what
        it leads to does not.
        """
        return self._get(STAT if follow_symlinks else LSTAT, "exists")

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a directory (or leads to one)."""
        return self._get(self._type_view(follow_symlinks), "dir")

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a regular file (or leads to one)."""
        return self._get(self._type_view(follow_symlinks), "file")

    def is_symlink(self) -> bool:
        """Whether the path is a symbolic link."""
        return self._get(LSTAT, "symlink")

    def size(self) -> tx.Optional[int]:
        """The size of the file in bytes.

        None for a directory, a path that is not there, or a store that
        does not report sizes.
        """
        return self._get(STAT, "size")

    def mtime(self) -> tx.Optional[float]:
        """When the file was last modified, as a POSIX timestamp.

        Seconds since the epoch, as `stat().st_mtime` gives them. None for
        a path that is not there or a store that does not report it (an
        object store reports none for a directory).
        """
        return self._get(STAT, "mtime")

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

    async def _get(self, view: str, key: str) -> tx.Any:
        value = self._facts.get(view, key)
        if value is UNKNOWN:
            found = await self._path._aquery_facts(view, key, self._facts)
            value = self._facts.keep(view, key, found)
        return value

    async def _type_view(self, follow_symlinks: bool) -> str:
        if not follow_symlinks:
            return LSTAT
        if self._facts.listed and not await self._get(LSTAT, "symlink"):
            return LSTAT
        return STAT

    async def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists (see `PathInfo.exists`)."""
        return await self._get(STAT if follow_symlinks else LSTAT, "exists")

    async def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a directory (or leads to one)."""
        return await self._get(await self._type_view(follow_symlinks), "dir")

    async def is_file(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the path is a regular file (or leads to one)."""
        return await self._get(
            await self._type_view(follow_symlinks), "file"
        )

    async def is_symlink(self) -> bool:
        """Whether the path is a symbolic link."""
        return await self._get(LSTAT, "symlink")

    async def size(self) -> tx.Optional[int]:
        """The size of the file in bytes (see `PathInfo.size`)."""
        return await self._get(STAT, "size")

    async def mtime(self) -> tx.Optional[float]:
        """When the file was last modified (see `PathInfo.mtime`)."""
        return await self._get(STAT, "mtime")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} of {self._path!r}>"


InfoView = (PathInfo, AsyncPathInfo)
