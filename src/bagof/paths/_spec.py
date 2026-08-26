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
    Member("with_stem", result=PATH),
    Member("with_suffix", result=PATH),
    Member("as_posix"),
    Member("as_uri"),
    Member("is_absolute"),
    Member("is_relative_to"),
    Member("relative_to", result=PATH, normalize=(("walk_up", False),)),
    # matching: delegate-or-raise for now; a vendored glob.translate fallback
    # gives these consistent semantics across drivers in a later phase.
    Member("match", normalize=(("case_sensitive", None),)),
    Member("full_match", normalize=(("case_sensitive", None),)),
)

BY_NAME = {member.name: member for member in PURE_PATH_MEMBERS}
