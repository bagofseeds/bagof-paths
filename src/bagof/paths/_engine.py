"""The policy engine: delegate, (later) fall back, or raise.

Every wrapper method routes through here so the delegate/fallback/raise
decision, the kwargs normalization, and the result re-wrapping live in one
place rather than in sixty method bodies. This is the sync engine; the async
wrapper bridges around a call into it.
"""

from __future__ import annotations

import inspect

import typing_extensions as tx

from ._args import path_arg
from ._base import BaseWrapper
from ._constants import PATH, PATH_ITER, PATH_TUPLE, SCALAR
from ._errors import UnsupportedPathOperation
from ._fallbacks import FALLBACKS
from ._spec import Member

_MISSING = object()

# (driver type, member, the class attribute, keywords) -> whether the
# driver's method takes those keywords; signatures are read once per type.
_ACCEPTS: tx.Dict[tx.Tuple[tx.Any, ...], bool] = {}


def _raise_unsupported(wrapper: BaseWrapper, name: str) -> tx.NoReturn:
    """Raise for a member that can be neither delegated nor synthesized.

    A driver may offer an `_unsupported_hint` string that explains the
    remedy (a backendless remote path uses this to point at installing a
    backend); it is added to the message when present.
    """
    hint = getattr(wrapper._wrapped, "_unsupported_hint", None)
    raise UnsupportedPathOperation(name, driver=wrapper._wrapped, hint=hint)


def _unwrap(value: tx.Any) -> tx.Any:
    """A wrapper passed as an argument is delegated as its wrapped path."""
    return value._wrapped if isinstance(value, BaseWrapper) else value


def coerce_args(
    member: Member, args: tx.Tuple[tx.Any, ...]
) -> tx.Tuple[tx.Any, ...]:
    """A member's positional arguments as the driver should receive them.

    Path arguments go through the shared path coercion (`bytes` refused, a
    plain `os.PathLike` read with `os.fspath`); any other argument (data,
    a mode, a name) is only unwrapped, so `write_bytes(b"...")` is untouched.
    """
    if member.paths:
        return tuple(path_arg(arg) for arg in args)
    return tuple(_unwrap(arg) for arg in args)


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
        if key in defaults:
            default = defaults[key]
            if value is default or value == default:
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
    value = getattr(wrapper._wrapped, member.name, _MISSING)
    if value is not _MISSING:
        return _finish(wrapper, value, member.result)
    _raise_unsupported(wrapper, member.name)


def invoke(
    wrapper: BaseWrapper,
    member: Member,
    args: tx.Tuple[tx.Any, ...] = (),
    kwargs: tx.Optional[tx.Mapping[str, tx.Any]] = None,
) -> tx.Any:
    """Resolve a delegated *method* member."""
    try:
        return _invoke(wrapper, member, args, kwargs)
    except (OSError, ValueError):
        if not quiet_check(wrapper, member):
            raise
        return False


def quiet_check(wrapper: BaseWrapper, member: Member) -> bool:
    """Whether an error from this member reads as False rather than raises.

    Python 3.14 made pathlib's status checks (`exists`, `is_file`, the
    other `is_*`) return False on any `OSError` or `ValueError`: a name
    too long, a parent that cannot be searched, an embedded NUL. Earlier
    versions raise for all but "not found". A local path follows 3.14 on
    every version, so a check answers the same everywhere; `stat()` still
    raises, for a caller that must see the error. On 3.14 this changes
    nothing. A remote driver's checks are its own and are left alone.
    """
    return member.check and wrapper.is_local()


def _invoke(
    wrapper: BaseWrapper,
    member: Member,
    args: tx.Tuple[tx.Any, ...],
    kwargs: tx.Optional[tx.Mapping[str, tx.Any]],
) -> tx.Any:
    kwargs = _drop_defaults(kwargs or {}, member.normalize)
    args = coerce_args(member, args)
    method = getattr(wrapper._wrapped, member.name, None)
    if callable(method):
        if member.keyword_fallback and not accepts(
            wrapper._wrapped, member.name, kwargs
        ):
            return _keyword_fallback(wrapper, member, args, kwargs)
        return _finish(wrapper, method(*args, **kwargs), member.result)
    if member.fallback and _has_needs(wrapper, member):
        synth = FALLBACKS[member.fallback]
        return _finish(wrapper, synth(wrapper, *args, **kwargs), member.result)
    _raise_unsupported(wrapper, member.name)


def _has_needs(wrapper: BaseWrapper, member: Member) -> bool:
    """Whether every primitive a fallback depends on is itself available."""
    return all(wrapper.supports(name) for name in member.needs)


def accepts(
    driver: tx.Any, name: str, kwargs: tx.Mapping[str, tx.Any]
) -> bool:
    """Whether the driver's `name` method takes every keyword in `kwargs`.

    Read from the signature, not by calling and catching `TypeError`, which
    would also swallow a real error raised inside the method. A signature
    that cannot be read (a builtin) counts as accepting: the call decides.
    """
    if not kwargs:
        return True
    keys = frozenset(kwargs)
    key = (type(driver), name, getattr(type(driver), name, None), keys)
    try:
        return _ACCEPTS[key]
    except KeyError:
        pass
    except TypeError:  # an unhashable class attribute: do not cache
        return _read_accepts(getattr(driver, name), keys)
    result = _ACCEPTS[key] = _read_accepts(getattr(driver, name), keys)
    return result


def _read_accepts(method: tx.Any, keys: tx.FrozenSet[str]) -> bool:
    try:
        params = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return True
    named = set()
    for param in params:
        if param.kind is param.VAR_KEYWORD:
            return True
        if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            named.add(param.name)
    return keys <= named


def keyword_call(member: Member, kwargs: tx.Mapping[str, tx.Any]) -> str:
    """How a call reads in an error: `is_file(follow_symlinks=False)`."""
    shown = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return f"{member.name}({shown})"


def _keyword_fallback(
    wrapper: BaseWrapper,
    member: Member,
    args: tx.Tuple[tx.Any, ...],
    kwargs: tx.Mapping[str, tx.Any],
) -> tx.Any:
    """A keyword the driver's method lacks: synthesize, else raise."""
    if member.keyword_fallback and _has_needs(wrapper, member):
        synth = FALLBACKS[member.keyword_fallback]
        return _finish(wrapper, synth(wrapper, *args, **kwargs), member.result)
    _raise_unsupported(wrapper, keyword_call(member, kwargs))
