"""The member table: the pathlib surface described once.

Each :class:`Member` says how one pathlib member is delegated, what its
result should become, and (in later phases) how it is synthesized when the
wrapped object lacks it. Both the sync and the async wrapper read this table,
so the surface stays defined in exactly one place.
"""

from __future__ import annotations

import typing_extensions as tx

from ._constants import IO, PATH, PATH_ITER, PATH_TUPLE, PURE, SCALAR


class Member(tx.NamedTuple):
    """One entry of the wrapped pathlib surface."""

    name: str
    # PURE (lexical) or IO (touches a filesystem).
    kind: str = PURE
    # True: accessed as a property; False: called as a method.
    prop: bool = False
    # How the engine treats the returned value (see _constants).
    result: str = SCALAR
    # (keyword, default) pairs forwarded to the driver only when the caller's
    # value differs from the default -- so drivers with older signatures that
    # lack the keyword still work in the common case.
    normalize: tx.Tuple[tx.Tuple[str, tx.Any], ...] = ()
    # Name of a synthesis function used when the driver lacks the member
    # (wired in a later phase); None means delegate-or-raise.
    fallback: tx.Optional[str] = None
    # Primitives the fallback needs; if any is missing the member raises.
    needs: tx.Tuple[str, ...] = ()


# The pure-path surface: lexical members that every driver either implements
# directly or (later) has synthesized. Location members that need
# canonicalization across drivers -- protocol, path, drive, root, anchor --
# live on the base wrapper, not here.
PURE_PATH_MEMBERS = (
    # properties
    Member("name", prop=True),
    Member("stem", prop=True),
    Member("suffix", prop=True),
    Member("suffixes", prop=True),
    Member("parts", prop=True),
    Member("parent", prop=True, result=PATH),
    Member("parents", prop=True, result=PATH_TUPLE),
    # methods
    Member("joinpath", result=PATH),
    Member("with_name", result=PATH),
    # with_stem / is_relative_to were added to pathlib in 3.9; synthesize them
    # so the surface is uniform on the 3.8 floor.
    Member(
        "with_stem", result=PATH, fallback="with_stem", needs=("with_name",)
    ),
    Member("with_suffix", result=PATH),
    Member("as_posix"),
    Member("as_uri"),
    Member("is_absolute"),
    Member(
        "is_relative_to", fallback="is_relative_to", needs=("relative_to",)
    ),
    Member("relative_to", result=PATH, normalize=(("walk_up", False),)),
    # with_segments builds a sibling path from string parts; pathlib gained it
    # in 3.12, so synthesize it from the wrapped type on older interpreters.
    Member(
        "with_segments", result=PATH, fallback="with_segments"
    ),
    # is_reserved is lexical (Windows device names); delegate-or-raise.
    Member("is_reserved"),
    # joinuri joins a URI onto the path (universal-pathlib); delegate-or-raise.
    Member("joinuri", result=PATH),
    # match / full_match are not delegated: they are computed lexically on the
    # canonical path (see _match.py) so their semantics are identical across
    # drivers and Python versions.
)

# The concrete surface: members that reach a filesystem. Copy/move, recursive
# rmdir, walk, and the classmethods (home/cwd) arrive with the driver adapter
# layer, where their cross-driver divergence is handled in one place.
IO_MEMBERS = (
    # status queries -- follow_symlinks is forwarded only when False, so a
    # driver whose signature predates the keyword still works by default.
    Member("exists", IO, normalize=(("follow_symlinks", True),)),
    Member("is_file", IO, normalize=(("follow_symlinks", True),)),
    Member("is_dir", IO, normalize=(("follow_symlinks", True),)),
    Member("is_symlink", IO),
    Member("stat", IO, normalize=(("follow_symlinks", True),)),
    Member("lstat", IO),
    Member("samefile", IO),
    # the read/write primitive; buffering/encoding/errors/newline forwarded
    # only when set, so a driver whose open() takes just a mode still works.
    Member(
        "open", IO,
        normalize=(
            ("buffering", -1), ("encoding", None),
            ("errors", None), ("newline", None),
        ),
    ),
    # read/write: delegate, else synthesize from open / read_bytes
    Member("read_bytes", IO, fallback="read_bytes", needs=("open",)),
    Member(
        "read_text", IO, fallback="read_text", needs=("read_bytes",),
        normalize=(("encoding", None), ("errors", None), ("newline", None)),
    ),
    Member("write_bytes", IO, fallback="write_bytes", needs=("open",)),
    Member(
        "write_text", IO, fallback="write_text", needs=("write_bytes",),
        normalize=(("encoding", None), ("errors", None), ("newline", None)),
    ),
    # directory iteration
    Member("iterdir", IO, result=PATH_ITER),
    Member(
        "glob", IO, result=PATH_ITER,
        normalize=(("case_sensitive", None), ("recurse_symlinks", False)),
    ),
    Member(
        "rglob", IO, result=PATH_ITER,
        normalize=(("case_sensitive", None), ("recurse_symlinks", False)),
    ),
    # creation (return None, like pathlib -- no fluent self)
    Member(
        "mkdir", IO,
        normalize=(("mode", 0o777), ("parents", False), ("exist_ok", False)),
    ),
    Member("touch", IO, normalize=(("mode", 0o666), ("exist_ok", True))),
    # removal -- missing_ok is always forwarded (not normalized away),
    # because cloudpathlib's own default is True: dropping our False default
    # would silently adopt it and make unlink() succeed on a missing file.
    Member("unlink", IO),
    # resolving / expanding (return a path)
    Member("resolve", IO, result=PATH, normalize=(("strict", False),)),
    Member("absolute", IO, result=PATH),
    Member("expanduser", IO, result=PATH),
    Member("readlink", IO, result=PATH),
    Member("rename", IO, result=PATH),
    Member("replace", IO, result=PATH),
    # -- extended status queries (pathlib/universal-pathlib) ----------------
    # Special-file and mount tests; delegate-or-raise (a driver without the
    # concept -- most cloud backends -- reports it as unsupported).
    Member("is_mount", IO),
    Member("is_socket", IO),
    Member("is_fifo", IO),
    Member("is_block_device", IO),
    Member("is_char_device", IO),
    # is_junction is 3.12+/cloud; synthesize False where the concept is absent
    # so the answer is uniform across drivers and interpreter versions.
    Member("is_junction", IO, fallback="is_junction"),
    # -- permissions and ownership ------------------------------------------
    # follow_symlinks reached pathlib.chmod/owner/group in 3.13; forward it
    # only when False/non-default so older signatures still accept the call.
    Member("chmod", IO, normalize=(("follow_symlinks", True),)),
    # lchmod delegate-or-raise: it cannot be synthesized portably (many
    # platforms have no lchmod, and chmod's follow_symlinks keyword is 3.10+).
    Member("lchmod", IO),
    Member("owner", IO, normalize=(("follow_symlinks", True),)),
    Member("group", IO, normalize=(("follow_symlinks", True),)),
    # -- links --------------------------------------------------------------
    Member(
        "symlink_to", IO, normalize=(("target_is_directory", False),)
    ),
    # hardlink_to synthesizes from os.link for a local driver that lacks it
    # (stdlib pathlib gained it in 3.10).
    Member("hardlink_to", IO, fallback="hardlink_to"),
    # link_to was deprecated in 3.10 and removed in 3.12; kept for backward
    # compatibility and synthesized from hardlink_to where pathlib dropped it.
    Member("link_to", IO, fallback="link_to", needs=("hardlink_to",)),
    # -- cloud transfer and cache (cloudpathlib) ----------------------------
    # as_url synthesizes from as_uri (a plain URI) where the driver lacks it.
    # download_to/upload_from/clear_cache are cache operations with no generic
    # synthesis, so they delegate-or-raise. as_url returns a URL string and
    # download_to returns a local path (a different driver family), so neither
    # is re-wrapped; upload_from returns the uploaded path in this driver's
    # family, so it is re-wrapped like any other path result.
    Member("as_url", IO, fallback="as_url", needs=("as_uri",)),
    Member("download_to", IO),
    Member("upload_from", IO, result=PATH),
    Member("clear_cache", IO),
)

MEMBERS = PURE_PATH_MEMBERS + IO_MEMBERS
BY_NAME = {member.name: member for member in MEMBERS}
