"""Process-wide shared :class:`~gamas_bot.db.Database` connections for the API.

Opening a fresh SQLite connection pair per HTTP request costs ~17 ms (two
aiosqlite threads, ~13 PRAGMA statements, and a full migration pass) while the
queries themselves take ~0.2 ms. This broker keeps one ``Database`` per data
file open for the lifetime of the process and funnels every request's database
work onto a single dedicated event loop.

Why a dedicated loop: an aiosqlite connection binds each pending operation to
the event loop that submitted it, so *sharing one connection between several
concurrent request loops deadlocks*. Running all coroutines that touch a given
``Database`` on one owner loop (via ``asyncio.run_coroutine_threadsafe``) is
the safe shape; a per-connection ``asyncio.Lock`` additionally serialises each
request's whole callback, which also makes raw ``db.db.execute`` writes in the
WSGI handlers safe against interleaved multi-statement transactions.

Staleness: a cached connection whose database file was deleted or replaced
(different ``st_ino``) is closed and reopened, so test fixtures that rebuild a
database at the same path are always seen fresh. In-memory databases and the
``GAMAS_DB_REUSE=0`` escape hatch bypass the broker entirely and use the
historical connect/close-per-request path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from .db import Database

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = ["DatabaseBroker", "db_reuse_enabled", "get_broker", "reset_broker"]


def db_reuse_enabled() -> bool:
    """Connection reuse is on unless explicitly disabled (``GAMAS_DB_REUSE=0``)."""
    return os.environ.get("GAMAS_DB_REUSE", "1").strip() not in {"0", "false", "off"}


def _inode_of(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


class _Entry:
    __slots__ = ("db", "inode", "guard")

    def __init__(self, db: Database, inode: tuple[int, int] | None) -> None:
        self.db = db
        self.inode = inode
        self.guard = asyncio.Lock()


class DatabaseBroker:
    """One owner loop + one shared ``Database`` per path, per process."""

    def __init__(self) -> None:
        self._thread_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._entries: dict[str, _Entry] = {}
        # Both locks below are only ever touched on the owner loop.
        self._checkout_lock = asyncio.Lock()

    # ------------------------------------------------------------- owner loop
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever,
                    name="gamas-db-broker",
                    daemon=True,
                )
                thread.start()
                self._loop = loop
                self._thread = thread
            return self._loop

    # ------------------------------------------------------------- public API
    async def run(
        self, db_path: Path, fn: Callable[[Database], Awaitable[T]]
    ) -> T:
        """Execute ``fn(db)`` for ``db_path`` on the owner loop and return its result."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._run_guarded(db_path, fn), loop)
        return await asyncio.wrap_future(future)

    def close(self) -> None:
        """Close every cached connection and stop the owner loop (tests/shutdown)."""
        with self._thread_lock:
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_all(), loop).result(timeout=15)
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            logger.warning("db broker close failed", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        with self._thread_lock:
            self._entries.clear()
        self._checkout_lock = asyncio.Lock()

    # ------------------------------------------------------------- internals
    async def _close_all(self) -> None:
        for entry in list(self._entries.values()):
            try:
                await entry.db.close()
            except Exception:  # noqa: BLE001
                logger.warning("db broker connection close failed", exc_info=True)
        self._entries.clear()

    async def _run_guarded(
        self, db_path: Path, fn: Callable[[Database], Awaitable[T]]
    ) -> T:
        entry = await self._checkout(db_path)
        async with entry.guard:
            return await fn(entry.db)

    async def _checkout(self, db_path: Path) -> _Entry:
        async with self._checkout_lock:
            key = str(db_path)
            inode = _inode_of(db_path)
            entry = self._entries.get(key)
            if entry is not None and entry.inode != inode:
                # The file behind this key was deleted or replaced (a test
                # rebuilt its fixture); drop the stale handle and reopen.
                self._entries.pop(key, None)
                try:
                    await entry.db.close()
                except Exception:  # noqa: BLE001
                    logger.warning("stale db close failed", exc_info=True)
                entry = None
            if entry is None:
                db = Database(db_path)
                await db.connect()
                # Record the inode *after* connect() creates the file: a
                # pre-connect stat would be None and make the first checkout
                # look stale, closing the connection it just opened.
                entry = _Entry(db, _inode_of(db_path))
                self._entries[key] = entry
                logger.debug("db broker opened %s", db_path)
            return entry


_broker: DatabaseBroker | None = None
_broker_guard = threading.Lock()


def get_broker() -> DatabaseBroker:
    global _broker
    with _broker_guard:
        if _broker is None:
            _broker = DatabaseBroker()
        return _broker


def reset_broker() -> None:
    """Close and forget the shared broker (used by tests)."""
    global _broker
    with _broker_guard:
        broker, _broker = _broker, None
    if broker is not None:
        broker.close()
