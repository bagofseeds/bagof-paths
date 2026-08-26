"""The policy engine: delegate, (later) fall back, or raise.

Every wrapper method routes through here so the delegate/fallback/raise
decision, the kwargs normalization, and the result re-wrapping live in one
place rather than in sixty method bodies. This is the sync engine; the async
wrapper bridges around a call into it.
"""

from __future__ import annotations

import typing_extensions as tx

from ._base import BaseWrapper
from ._constants import PATH, PATH_ITER, PATH_TUPLE, SCALAR
from ._errors import UnsupportedPathOperation
from ._spec import Member


def _unwrap(value: tx.Any) -> tx.Any:
    """A wrapper passed as an argument is delegated as its wrapped path."""
    return value._wrapped if isinstance(value, BaseWrapper) else value


def _drop_defaults(
    kwargs: tx.Mapping[str, tx.Any],
    normalize: tx.Tuple[tx.Tuple[str, tx.Any], ...],
) -> tx.Dict[str, tx.Any]:
    """Forward a normalized keyword only when it differs from its default."""
    if not normalize:
        return dict(kwargs)
    defaults = dict(normalize)
    out = {}
    for key, value in kwargs.items():
        if key in defaults and value == defaults[key]:
            continue
        out[key] = value
    return out


def _finish(wrapper: BaseWrapper, result: tx.Any, policy: str) -> tx.Any:
    """Apply a member's result policy to what the driver returned."""
    if policy == SCALAR:
        return result
    if policy == PATH:
        return wrapper.with_wrapped(result)
    if policy == PATH_TUPLE:
        return tuple(wrapper.with_wrapped(item) for item in result)
    if policy == PATH_ITER:
        return (wrapper.with_wrapped(item) for item in result)
    return result


def get(wrapper: BaseWrapper, member: Member) -> tx.Any:
    """Resolve a delegated *property* member."""
    wrapped = wrapper._wrapped
    if hasattr(wrapped, member.name):
        return _finish(wrapper, getattr(wrapped, member.name), member.result)
    raise UnsupportedPathOperation(member.name, driver=wrapped)


def invoke(
    wrapper: BaseWrapper,
    member: Member,
    args: tx.Tuple[tx.Any, ...] = (),
    kwargs: tx.Optional[tx.Mapping[str, tx.Any]] = None,
) -> tx.Any:
    """Resolve a delegated *method* member."""
    kwargs = _drop_defaults(kwargs or {}, member.normalize)
    args = tuple(_unwrap(arg) for arg in args)
    method = getattr(wrapper._wrapped, member.name, None)
    if callable(method):
        return _finish(wrapper, method(*args, **kwargs), member.result)
    # A synthesized fallback is wired in for io members in a later phase.
    raise UnsupportedPathOperation(member.name, driver=wrapper._wrapped)
