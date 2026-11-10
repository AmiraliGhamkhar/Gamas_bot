"""Read/write the Mini App's data straight from SQLite, without the event loop.

The bot owns the database through ``aiosqlite`` inside its own process. The Mini
App runs in a *different* process — a Passenger worker or a Cloudflare-fronted
container — so it cannot borrow that connection. What it can do is open the same
file read-only-most-of-the-time:

* WAL mode (which the bot already uses, see :mod:`gamas_bot.db_core`) lets any
  number of readers work while the bot writes;
* writes are short and synchronous, and SQLite's own busy timeout handles the
  rare overlap with the bot's ``BEGIN IMMEDIATE`` transactions;
* every statement is scoped to one ``telegram_id``, so a Mini App session can
  only ever read or edit its own rows.

Deliberately absent from this module: the language model, the booklet builder,
Telegram calls. An edit here changes the *text cache* that the leaderboard,
quiz and flashcard builders read; it never re-renders a Word file, which would
mean loading python-docx in a web worker for no user-visible gain.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: How long a write waits for the bot's transaction before giving up. Long
#: enough to ride out a settlement, short enough that a stuck job cannot make the
#: Mini App hang.
BUSY_TIMEOUT_SECONDS = 3.0

#: Kinds of library row the editor may change. Office exports and transcripts are
#: intentionally excluded: their text is generated from a file, not authored.
EDITABLE_KINDS = ("booklet",)


class MiniAppStore:
    """The :class:`~gamas_bot.mini_app.MiniAppData` implementation over SQLite.

    One connection is opened per request and closed immediately. Passenger runs
    a small, pre-forked worker pool, and a long-lived connection per worker would
    hold a snapshot open across requests — the classic way a WAL file grows
    without bound on a shared host.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    # -- connection --------------------------------------------------------
    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # explicit transactions only
        )
        connection.row_factory = sqlite3.Row
        # The bot's process owns the schema; the app never migrates.
        connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        if not write:
            connection.execute("PRAGMA query_only = ON")
        return connection

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            return list(connection.execute(sql, params).fetchall())
        finally:
            connection.close()

    # -- ranking -----------------------------------------------------------
    def leaderboard(self, limit: int) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT first_name, quiz_points FROM users "
            "WHERE banned = 0 AND quiz_points > 0 "
            "ORDER BY quiz_points DESC, telegram_id ASC LIMIT ?",
            (int(limit),),
        )
        return [
            {"first_name": str(row["first_name"] or ""), "quiz_points": int(row["quiz_points"])}
            for row in rows
        ]

    def rank(self, user_id: int) -> dict[str, int]:
        rows = self._read(
            "SELECT quiz_points FROM users WHERE telegram_id = ?", (int(user_id),)
        )
        points = int(rows[0]["quiz_points"]) if rows else 0
        higher = self._read(
            "SELECT COUNT(*) AS n FROM users WHERE banned = 0 AND quiz_points > ?",
            (points,),
        )
        return {"rank": (int(higher[0]["n"]) if higher else 0) + 1, "points": points}

    # -- booklets ----------------------------------------------------------
    def booklets(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT item_id, title, created_at, unavailable, "
            "       LENGTH(COALESCE(content_text, '')) AS chars "
            "FROM library_items WHERE user_id = ? AND kind IN ('booklet') "
            "ORDER BY item_id DESC LIMIT ?",
            (int(user_id), int(limit)),
        )
        return [
            {
                "id": int(row["item_id"]),
                "title": str(row["title"] or ""),
                "created_at": str(row["created_at"] or ""),
                "chars": int(row["chars"] or 0),
                "editable": not bool(row["unavailable"]),
            }
            for row in rows
        ]

    def booklet(self, user_id: int, item_id: int) -> dict[str, Any] | None:
        rows = self._read(
            "SELECT item_id, title, content_text, unavailable "
            "FROM library_items WHERE item_id = ? AND user_id = ? AND kind = 'booklet'",
            (int(item_id), int(user_id)),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "id": int(row["item_id"]),
            "title": str(row["title"] or ""),
            # An empty cache is an empty editor, not a missing booklet: the user
            # can write the text they want their quiz built from.
            "content_text": str(row["content_text"] or ""),
            "editable": not bool(row["unavailable"]),
        }

    def save_booklet(self, user_id: int, item_id: int, text: str) -> bool:
        """Overwrite one booklet's text cache, if it belongs to this user."""
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE library_items SET content_text = ? "
                "WHERE item_id = ? AND user_id = ? AND kind = 'booklet' "
                "AND unavailable = 0",
                (text, int(item_id), int(user_id)),
            )
            updated = cursor.rowcount == 1
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - nothing left to roll back
                pass
            raise
        finally:
            connection.close()
        if updated:
            logger.info("mini app edited booklet item=%s user=%s", item_id, user_id)
        return updated
