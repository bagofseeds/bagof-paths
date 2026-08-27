"""Per-protocol traits: the data that varies by URL scheme.

A scheme's *behaviour* is data, not a class (see the design's construction
section): whether its first path component is a bucket, which spellings name
the same store, whether it is always absolute, and which driver to prefer when
building one. The base wrapper and the driver selector read this registry; a
downstream adds a protocol with one :func:`register_protocol` call rather than
a subclass.

``ProtocolTraits`` takes keyword arguments only, so a new trait can be added
later without shifting a positional and breaking a caller. Register protocols
at import time: traits participate in a path's identity (alias folding in the
canonical key), so registering after paths are already hashed into a dict
would change their hashes.
"""

from __future__ import annotations

import typing_extensions as tx


class ProtocolTraits:
    """What varies about a URL scheme, as data.

    Parameters
    ----------
    bucketed:
        The first path component is a bucket/container -- drives ``bucket``
        and the drive/root split.
    absolute:
        Paths of this scheme are always absolute (most remote stores).
    aliases:
        Other schemes that name the same store (``s3a`` for ``s3``); folded
        onto this scheme for identity so the spellings compare equal.
    driver:
        A preferred factory (a driver class, or a ``str -> path`` callable)
        used to build a path of this scheme before the availability order.
    """

    __slots__ = ("bucketed", "absolute", "aliases", "driver")

    def __init__(
        self,
        *,
        bucketed: bool = False,
        absolute: bool = False,
        aliases: tx.Iterable[str] = (),
        driver: tx.Optional[tx.Callable[[str], tx.Any]] = None,
    ) -> None:
        self.bucketed = bucketed
        self.absolute = absolute
        self.aliases = tuple(aliases)
        self.driver = driver

    def __repr__(self) -> str:
        return (
            "ProtocolTraits("
            f"bucketed={self.bucketed!r}, absolute={self.absolute!r}, "
            f"aliases={self.aliases!r}, driver={self.driver!r})"
        )


_DEFAULT = ProtocolTraits()

# canonical scheme -> traits, and alias scheme -> canonical scheme. Both keyed
# by lower-case scheme (URL schemes are case-insensitive).
_PROTOCOLS: tx.Dict[str, ProtocolTraits] = {}
_ALIASES: tx.Dict[str, str] = {}


def register_protocol(
    scheme: str,
    *,
    bucketed: bool = False,
    absolute: bool = False,
    aliases: tx.Iterable[str] = (),
    driver: tx.Optional[tx.Callable[[str], tx.Any]] = None,
) -> None:
    """Register (or replace) the traits for a URL ``scheme``.

    A later registration replaces an earlier one for the same scheme
    wholesale. Registering a scheme that was another scheme's alias detaches
    it: the explicit registration wins.

    Register at import time -- see the module note on identity.
    """
    scheme = scheme.lower()
    new_aliases = tuple(alias.lower() for alias in aliases)
    # An alias must not hijack a scheme that is registered in its own right;
    # that would silently flip the identity of every path of that scheme.
    for alias in new_aliases:
        if alias in _PROTOCOLS and alias != scheme:
            raise ValueError(
                f"cannot use {alias!r} as an alias: it is already a "
                "registered protocol"
            )
    traits = ProtocolTraits(
        bucketed=bucketed,
        absolute=absolute,
        aliases=new_aliases,
        driver=driver,
    )
    _PROTOCOLS[scheme] = traits
    # Replace wholesale: this scheme is canonical now (not an alias), and any
    # aliases a previous registration of it left behind are dropped.
    _ALIASES.pop(scheme, None)
    for alias in [a for a, canon in _ALIASES.items() if canon == scheme]:
        del _ALIASES[alias]
    for alias in new_aliases:
        _ALIASES[alias] = scheme


def canonical_scheme(scheme: str) -> str:
    """The scheme an alias stands for; the scheme itself when not an alias."""
    scheme = scheme.lower()
    return _ALIASES.get(scheme, scheme)


def traits_for(scheme: str) -> ProtocolTraits:
    """The traits for ``scheme`` (following aliases); a default when unknown.

    An unregistered scheme still wraps -- it gets the default traits
    (not bucketed, not always-absolute) and degrades per member.
    """
    return _PROTOCOLS.get(canonical_scheme(scheme), _DEFAULT)


def _register_builtin_protocols() -> None:
    # The object stores whose first component is a bucket; each carries the
    # spellings that name the same store.
    register_protocol("s3", bucketed=True, absolute=True, aliases=("s3a",))
    register_protocol("gs", bucketed=True, absolute=True, aliases=("gcs",))
    register_protocol(
        "az", bucketed=True, absolute=True,
        aliases=("abfs", "abfss", "azure", "adl"),
    )
    # memory is a flat absolute namespace, but not bucketed.
    register_protocol("memory", absolute=True)


_register_builtin_protocols()
