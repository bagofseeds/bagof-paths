"""Shared sentinels and vocabulary for the wrapper machinery."""

from __future__ import annotations

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
