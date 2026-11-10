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


def _utc_now() -> str:
    """UTC ISO-8601 without importing the bot's asyncio database layer."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None

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

    def _read_optional(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[sqlite3.Row]:
        """Read a table added by a newer migration, degrading to no rows.

        The Mini App may be deployed a few minutes before the bot applies a
        migration. A missing optional table must never turn the Home screen
        into a 500; the core (all-time board, booklets) still works.
        """
        try:
            return self._read(sql, params)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "no such table" not in message and "no such column" not in message:
                raise
            return []

    # -- ranking -----------------------------------------------------------
    def leaderboard(self, limit: int) -> list[dict[str, Any]]:
        return self.leaderboard_window(limit, "all")

    def leaderboard_window(
        self, limit: int, window: str = "all"
    ) -> list[dict[str, Any]]:
        """All-time, weekly or monthly top scorers.

        Weekly/monthly are computed from the append-only ``quiz_point_events``
        table (migration 026). The all-time board still reads the authoritative
        ``users.quiz_points`` total, so an old deployment or a database without
        events keeps the exact ranking it had before.
        """
        window = (window or "all").strip().lower()
        if window not in ("weekly", "monthly"):
            rows = self._read(
                "SELECT first_name, quiz_points FROM users "
                "WHERE banned = 0 AND quiz_points > 0 "
                "ORDER BY quiz_points DESC, telegram_id ASC LIMIT ?",
                (int(limit),),
            )
            return [
                {
                    "first_name": str(row["first_name"] or ""),
                    "quiz_points": int(row["quiz_points"]),
                }
                for row in rows
            ]
        days = 7 if window == "weekly" else 30
        rows = self._read_optional(
            "SELECT u.first_name, SUM(e.points) AS points "
            "FROM quiz_point_events e JOIN users u ON u.telegram_id = e.user_id "
            "WHERE u.banned = 0 AND e.created_at >= datetime('now', ?) "
            "GROUP BY e.user_id HAVING points > 0 "
            "ORDER BY points DESC, e.user_id ASC LIMIT ?",
            (f"-{days} days", int(limit)),
        )
        return [
            {
                "first_name": str(row["first_name"] or ""),
                "quiz_points": int(row["points"] or 0),
            }
            for row in rows
        ]

    def rank_window(self, user_id: int, window: str = "all") -> dict[str, int]:
        """The caller's place in a time window (all-time uses the cached total)."""
        window = (window or "all").strip().lower()
        if window not in ("weekly", "monthly"):
            return self.rank(user_id)
        days = 7 if window == "weekly" else 30
        rows = self._read_optional(
            "SELECT COALESCE(SUM(points), 0) FROM quiz_point_events "
            "WHERE user_id = ? AND created_at >= datetime('now', ?)",
            (int(user_id), f"-{days} days"),
        )
        points = int(rows[0][0] or 0) if rows else 0
        higher = self._read_optional(
            "SELECT COUNT(*) AS n FROM (SELECT e.user_id FROM quiz_point_events e "
            "JOIN users u ON u.telegram_id = e.user_id "
            "WHERE u.banned = 0 AND e.created_at >= datetime('now', ?) "
            "GROUP BY e.user_id HAVING SUM(e.points) > ?)",
            (f"-{days} days", points),
        )
        return {
            "rank": (int(higher[0]["n"]) if higher else 0) + 1,
            "points": points,
        }

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

    # -- speaker turns -----------------------------------------------------
    def speaker_turns(self, user_id: int, submission_id: int) -> list[dict[str, Any]]:
        """Anonymous turns of one submission the caller owns."""
        import json as _json

        rows = self._read_optional(
            "SELECT t.speaker_turns_json FROM transcriptions t "
            "JOIN submissions s ON s.submission_id = t.submission_id "
            "WHERE t.submission_id = ? AND s.user_id = ?",
            (int(submission_id), int(user_id)),
        )
        if not rows or not rows[0][0]:
            return []
        try:
            raw = _json.loads(str(rows[0][0]))
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        names = self._read_optional(
            "SELECT speaker_label, custom_name FROM speaker_names WHERE submission_id = ?",
            (int(submission_id),),
        )
        custom = {str(row[0]): str(row[1]) for row in names}
        turns: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            label = item.get("speaker_label")
            label_text = str(label) if label is not None else None
            turns.append(
                {
                    "speaker_id": str(item.get("speaker_id") or "speaker_unknown"),
                    "speaker_label": label_text,
                    "display_name": (
                        custom.get(label_text, str(item.get("display_name") or ""))
                        if label_text is not None
                        else None
                    ),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "text": str(item.get("text") or ""),
                    "overlaps": bool(item.get("overlaps", False)),
                }
            )
        return turns

    def rename_speaker(
        self, user_id: int, submission_id: int, label: str, name: str
    ) -> bool:
        """Rename one anonymous label, only for the owner of the submission."""
        clean_name = " ".join(str(name).split())[:64]
        clean_label = str(label).strip()[:16]
        if not clean_name or not clean_label or not clean_label.isdigit():
            return False
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT 1 FROM submissions WHERE submission_id = ? AND user_id = ?",
                (int(submission_id), int(user_id)),
            ).fetchone()
            if owner is None or not _has_table(connection, "speaker_names"):
                connection.execute("COMMIT")
                return False
            connection.execute(
                "INSERT INTO speaker_names(submission_id, speaker_label, custom_name, named_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(submission_id, speaker_label) "
                "DO UPDATE SET custom_name = excluded.custom_name, "
                "named_at = excluded.named_at",
                (int(submission_id), clean_label, clean_name, _utc_now()),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise
        finally:
            connection.close()

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

    def profile(self, user_id: int) -> dict[str, Any]:
        """The Home screen: only the caller's own, non-private summary."""
        from gamas_bot.billing import seconds_to_credits

        rows = self._read_optional(
            "SELECT first_name, quiz_points FROM users WHERE telegram_id = ?",
            (int(user_id),),
        )
        if not rows:
            return {}
        row = rows[0]
        entitlement = self._read_optional(
            "SELECT is_unlimited FROM users WHERE telegram_id = ?",
            (int(user_id),),
        )
        unlimited = bool(entitlement[0][0]) if entitlement else False
        balance = self._read_optional(
            "SELECT available_seconds FROM user_balances WHERE user_id = ?",
            (int(user_id),),
        )
        seconds = int(balance[0][0]) if balance else 0
        counts = self._read_optional(
            "SELECT (SELECT COUNT(*) FROM library_items WHERE user_id = ?), "
            "(SELECT COUNT(*) FROM submissions WHERE user_id = ?)",
            (int(user_id), int(user_id)),
        )
        artifacts = self._read_optional(
            "SELECT COUNT(*) AS n FROM submission_artifacts WHERE user_id = ? AND available = 1",
            (int(user_id),),
        )
        return {
            "first_name": str(row["first_name"] or ""),
            "credits": seconds_to_credits(seconds),
            "unlimited": unlimited,
            "quiz_points": int(row["quiz_points"] or 0),
            "library_count": int(counts[0][0]) if counts else 0,
            "submission_count": int(counts[0][1]) if counts else 0,
            "artifact_count": int(artifacts[0]["n"]) if artifacts else 0,
        }

    def revisions(self, user_id: int, item_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Revision history of one booklet the caller owns (newest first)."""
        rows = self._read_optional(
            "SELECT r.revision_id, r.created_at, r.source, "
            "LENGTH(r.content_text) AS chars FROM booklet_revisions r "
            "JOIN library_items l ON l.item_id = r.item_id "
            "WHERE r.item_id = ? AND r.user_id = ? AND l.user_id = ? "
            "ORDER BY r.revision_id DESC LIMIT ?",
            (int(item_id), int(user_id), int(user_id), max(1, min(50, int(limit)))),
        )
        return [
            {
                "id": int(row["revision_id"]),
                "created_at": str(row["created_at"] or ""),
                "source": str(row["source"] or ""),
                "chars": int(row["chars"] or 0),
            }
            for row in rows
        ]

    def revision_text(self, user_id: int, revision_id: int) -> str | None:
        rows = self._read_optional(
            "SELECT content_text FROM booklet_revisions "
            "WHERE revision_id = ? AND user_id = ?",
            (int(revision_id), int(user_id)),
        )
        return str(rows[0]["content_text"]) if rows else None

    def restore_revision(
        self, user_id: int, item_id: int, revision_id: int
    ) -> bool:
        """Restore a revision as a *new* save (history is append-only)."""
        text = self.revision_text(user_id, revision_id)
        if text is None:
            return False
        return self.save_booklet(user_id, item_id, text, source="restore")

    def save_booklet(
        self, user_id: int, item_id: int, text: str, *, source: str = "mini_app"
    ) -> bool:
        """Overwrite one booklet's text cache, appending a revision first."""
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT COALESCE(content_text, '') FROM library_items "
                "WHERE item_id = ? AND user_id = ? AND kind = 'booklet' "
                "AND unavailable = 0",
                (int(item_id), int(user_id)),
            ).fetchone()
            if previous is None:
                connection.execute("COMMIT")
                return False
            has_revisions = _has_table(connection, "booklet_revisions")
            if has_revisions:
                connection.execute(
                    "INSERT INTO booklet_revisions(item_id, user_id, content_text, "
                    "source, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        int(item_id),
                        int(user_id),
                        str(previous[0] or ""),
                        str(source)[:32],
                        _utc_now(),
                    ),
                )
            cursor = connection.execute(
                "UPDATE library_items SET content_text = ? "
                "WHERE item_id = ? AND user_id = ? AND kind = 'booklet' "
                "AND unavailable = 0",
                (text, int(item_id), int(user_id)),
            )
            updated = cursor.rowcount == 1
            # Bounded history: keep the 20 newest revisions per booklet.
            if has_revisions:
                connection.execute(
                    "DELETE FROM booklet_revisions WHERE item_id = ? AND revision_id NOT IN "
                    "(SELECT revision_id FROM booklet_revisions WHERE item_id = ? "
                    "ORDER BY revision_id DESC LIMIT 20)",
                    (int(item_id), int(item_id)),
                )
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
