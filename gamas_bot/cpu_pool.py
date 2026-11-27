"""Off-event-loop, off-GIL execution for CPU-bound booklet builds.

``booklet PDF/Word builds are CPU-heavy (Typst compilation or ReportLab
layout). :func:`gamas_bot.fileio.run_file_io` / ``asyncio.to_thread`` keep the
event loop unblocked, but the GIL still serialises ReportLab work against the
bot's Telegram updates. This module routes the *build* step through a small
``spawn``-based process pool when the deployment opts in, so the parent's GIL
is released for the whole compile.

The pool is strictly opt-in (``GAMAS_BUILD_POOL=1``) because child processes
cannot see test monkey-patches; the default path is the exact same
thread-based call used before. Every pool failure — unpicklable arguments,
worker crash, executor shutdown — degrades to the thread path, never to a job
failure.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: One pool per process; two workers bound GIL-free builds to two cores so a
#: burst of builds cannot starve the bot or the host.
DEFAULT_POOL_SIZE = 2

_pool: ProcessPoolExecutor | None = None
_pool_lock = asyncio.Lock()


def pool_enabled() -> bool:
    """True when the deployment explicitly enabled process-pool builds."""
    return os.environ.get("GAMAS_BUILD_POOL", "").strip() == "1"


def _pool_size() -> int:
    try:
        return max(1, min(8, int(os.environ.get("GAMAS_BUILD_POOL_SIZE", DEFAULT_POOL_SIZE))))
    except (TypeError, ValueError):
        return DEFAULT_POOL_SIZE


def _invoke(fn: Callable[..., T], args: tuple[Any, ...], kwargs: dict[str, Any]) -> T:
    """Run in the worker process (module-level so it pickles)."""
    return fn(*args, **kwargs)


def _get_pool() -> ProcessPoolExecutor:
    """Create the shared pool once per process, using the ``spawn`` method.

    ``fork`` from a multi-threaded bot (aiosqlite workers, Telethon) can
    deadlock the child on inherited locks; ``spawn`` starts clean
    interpreters and inherits only what ``pickle`` carries plus the
    environment.
    """
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=_pool_size(),
            mp_context=multiprocessing.get_context("spawn"),
        )
    return _pool


def shutdown_pool() -> None:
    """Release worker processes (called from tests and clean exits)."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


async def run_cpu_bound(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``fn(*args, **kwargs)`` off the event loop; process pool when enabled.

    * Default (``GAMAS_BUILD_POOL`` unset or anything but ``1``): the exact
      previous behaviour — ``asyncio.to_thread``.
    * Enabled: submit to the spawn pool; on *any* submission or execution
      problem fall back to the thread path so the caller sees the same result
      or the same ``fn`` error it always would.
    """
    if not pool_enabled():
        return await asyncio.to_thread(fn, *args, **kwargs)

    payload = partial(_invoke, fn, args, kwargs)
    try:
        pickle.dumps(payload)
    except Exception:
        logger.debug("cpu_pool: unpicklable callable %r; using thread path", fn)
        return await asyncio.to_thread(fn, *args, **kwargs)

    loop = asyncio.get_running_loop()
    try:
        pool = _get_pool()
        return await loop.run_in_executor(pool, payload)
    except Exception:
        logger.warning("cpu_pool: falling back to thread path", exc_info=True)
        return await asyncio.to_thread(fn, *args, **kwargs)


__all__ = [
    "DEFAULT_POOL_SIZE",
    "pool_enabled",
    "run_cpu_bound",
    "shutdown_pool",
]
