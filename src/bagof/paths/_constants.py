"""Shared sentinels and vocabulary for the wrapper machinery."""

from __future__ import annotations

import re

# Matches a leading URL scheme, e.g. the "s3" of "s3://bucket/key". Used to
# recover the protocol and the scheme-less path from a driver whose str() is
# a URL but which exposes no protocol/path attribute.
SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://")

# -- member kinds -----------------------------------------------------------
# A pure member is lexical: it never touches a filesystem and is never async.
# An io member may reach a filesystem and gets an async counterpart.
PURE = "pure"
IO = "io"

# -- result policies --------------------------------------------------------
# How the engine treats what a delegated member returns.
SCALAR = "scalar"          # return the value unchanged
PATH = "path"              # a driver path -> re-wrap via with_wrapped
PATH_TUPLE = "path_tuple"  # a sequence of driver paths -> tuple of wrappers
PATH_ITER = "path_iter"    # an iterable of driver paths -> wrapper generator

# -- protocols treated as the local filesystem ------------------------------
# Used for __fspath__ and (later) default-driver selection.
LOCAL_PROTOCOLS = frozenset({"", "file", "local"})

# Members handled by the driver-adapter layer rather than the engine.
ADAPTER_MEMBERS = frozenset(
    {"rmdir", "copy", "copy_into", "move", "move_into", "walk"}
)

# Members computed lexically (see _match.py), delegated to neither.
COMPUTED_MEMBERS = frozenset({"match", "full_match"})
