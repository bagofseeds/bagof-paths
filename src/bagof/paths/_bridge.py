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

import typing_extensions as tx


async def run(
    func: tx.Callable[..., tx.Any], *args: tx.Any, **kwargs: tx.Any
) -> tx.Any:
    """Run a blocking ``func`` in the default thread pool and await it."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(func, *args, **kwargs)
    )
