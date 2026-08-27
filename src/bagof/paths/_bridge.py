"""The async bridge.

The async wrapper reuses the whole synchronous implementation -- engine,
fallbacks, adapters -- by running it on a sync view of the same driver in a
worker thread, so the blocking work never stalls the event loop and no policy
is written twice. This is stdlib only: ``run_in_executor`` with a
``functools.partial`` (``asyncio.to_thread`` is 3.9+, and we support 3.8),
which also lets keyword arguments through.
"""

from __future__ import annotations

import asyncio
import functools
import inspect

import typing_extensions as tx

from ._errors import UnsupportedPathOperation


async def run(
    func: tx.Callable[..., tx.Any], *args: tx.Any, **kwargs: tx.Any
) -> tx.Any:
    """Run a blocking ``func`` in the default thread pool and await it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as error:  # pragma: no cover
        raise RuntimeError(
            "AsyncPath needs a running asyncio event loop"
        ) from error
    result = await loop.run_in_executor(
        None, functools.partial(func, *args, **kwargs)
    )
    if inspect.isawaitable(result):
        # The wrapped driver's method is itself a coroutine: we ran it in a
        # thread and got an un-awaited awaitable back. Fail cleanly rather
        # than leak a truthy coroutine object (and a "never awaited" warning).
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise UnsupportedPathOperation(
            getattr(func, "__name__", "call"),
            hint=(
                "this driver's methods are coroutines; natively-async "
                "drivers are not yet supported by AsyncPath"
            ),
        )
    return result
