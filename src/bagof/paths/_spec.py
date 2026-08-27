"""The member table: the pathlib surface described once.

Each :class:`Member` says how one pathlib member is delegated, what its
result should become, and (in later phases) how it is synthesized when the
wrapped object lacks it. Both the sync and the async wrapper read this table,
so the surface stays defined in exactly one place.
"""

from __future__ import annotations

import typing_extensions as tx

from ._constants import PATH, PATH_TUPLE, PURE, SCALAR


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
    # match / full_match are not delegated: they are computed lexically on the
    # canonical path (see _match.py) so their semantics are identical across
    # drivers and Python versions.
)

MEMBERS = PURE_PATH_MEMBERS
BY_NAME = {member.name: member for member in MEMBERS}
