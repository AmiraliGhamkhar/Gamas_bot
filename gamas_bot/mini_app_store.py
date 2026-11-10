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

Deliberately absent from this module: the language model and the booklet
builder. An edit here changes the *editable caches* that the Studio reads —
``library_items.content_text`` for learning and ``library_items.structured_json``
for the canonical structured model. Since Gamas 3.0 a structured save also
re-renders the polished PDF and the editable DOCX from that same model and
persists them as the submission's binary artifacts, so the Telegram booklet
buttons keep serving the *edited* booklet with no renderer and no model call at
download time.
"""

from __future__ import annotations

import json as _json
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


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether a migration-added column exists in *this* database.

    The Mini App may be deployed minutes before the bot applies migration 028;
    a missing column must degrade the structured editor, never crash the app.
    """
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row[1]) == column for row in rows)

#: How long a write waits for the bot's transaction before giving up. Long
#: enough to ride out a settlement, short enough that a stuck job cannot make the
#: Mini App hang.
BUSY_TIMEOUT_SECONDS = 3.0

#: Kinds of library row the editor may change. Office exports and transcripts are
#: intentionally excluded: their text is generated from a file, not authored.


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
    def _speaker_names_rows(
        self, connection: sqlite3.Connection, submission_id: int
    ) -> list[sqlite3.Row]:
        """Owner-scoped custom names/roles, tolerating a pre-028 schema."""
        if not _has_table(connection, "speaker_names"):
            return []
        if _has_column(connection, "speaker_names", "role"):
            return list(
                connection.execute(
                    "SELECT speaker_label, custom_name, role FROM speaker_names "
                    "WHERE submission_id = ?",
                    (int(submission_id),),
                ).fetchall()
            )
        return list(
            connection.execute(
                "SELECT speaker_label, custom_name, '' AS role FROM speaker_names "
                "WHERE submission_id = ?",
                (int(submission_id),),
            ).fetchall()
        )

    def speaker_turns(self, user_id: int, submission_id: int) -> list[dict[str, Any]]:
        """Anonymous turns of one submission the caller owns."""
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
        connection = self._connect()
        try:
            names = self._speaker_names_rows(connection, submission_id)
        except sqlite3.OperationalError:
            names = []
        finally:
            connection.close()
        custom = {str(row[0]): str(row[1]) for row in names}
        roles = {str(row[0]): str(row[2] or "") for row in names}
        from .transcript import speaker_name_from_label

        turns: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            label = item.get("speaker_label")
            label_text = str(label) if label is not None else None
            default_name = (
                speaker_name_from_label(label_text) if label_text is not None else None
            )
            turns.append(
                {
                    "speaker_id": str(item.get("speaker_id") or "speaker_unknown"),
                    "speaker_label": label_text,
                    "display_name": (
                        custom.get(label_text)
                        or str(item.get("display_name") or "")
                        or default_name
                        if label_text is not None
                        else None
                    ),
                    "default_name": default_name,
                    "custom_name": custom.get(label_text) if label_text is not None else None,
                    "role": roles.get(label_text, "") if label_text is not None else "",
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "text": str(item.get("text") or ""),
                    "overlaps": bool(item.get("overlaps", False)),
                }
            )
        return turns

    def rename_speaker(
        self,
        user_id: int,
        submission_id: int,
        label: str,
        name: str,
        role: str | None = None,
    ) -> bool:
        """Rename one anonymous label (and optionally set a role), owner-only.

        ``role`` is free text the owner typed (``مدرس`` …) or empty to clear it;
        ``None`` means "not provided" and keeps the stored role untouched.
        Nothing about the audio, the turns or their timestamps is modified.
        """
        clean_name = " ".join(str(name).split())[:64]
        clean_label = str(label).strip()[:16]
        if not clean_name or not clean_label or not clean_label.isdigit():
            return False
        clean_role = None if role is None else " ".join(str(role).split())[:64]
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
            if clean_role is None or not _has_column(connection, "speaker_names", "role"):
                connection.execute(
                    "INSERT INTO speaker_names(submission_id, speaker_label, custom_name, named_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(submission_id, speaker_label) "
                    "DO UPDATE SET custom_name = excluded.custom_name, "
                    "named_at = excluded.named_at",
                    (int(submission_id), clean_label, clean_name, _utc_now()),
                )
            else:
                connection.execute(
                    "INSERT INTO speaker_names(submission_id, speaker_label, custom_name, "
                    "role, named_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(submission_id, speaker_label) DO UPDATE SET "
                    "custom_name = excluded.custom_name, role = excluded.role, "
                    "named_at = excluded.named_at",
                    (
                        int(submission_id),
                        clean_label,
                        clean_name,
                        clean_role,
                        _utc_now(),
                    ),
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

    # -- submissions (Studio overview) --------------------------------------
    def submissions(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        """The caller's submissions with what the Studio can do with each.

        One lightweight query per screen: speaker counts are *not* computed here
        (the turns JSON of a three-hour class is large); the Speakers view loads
        them lazily per submission.
        """
        rows = self._read_optional(
            "SELECT s.submission_id, s.kind, s.filename, s.status, s.created_at, "
            "s.finished_at, s.duration_seconds, "
            "(SELECT li.title FROM library_items li WHERE li.user_id = s.user_id "
            " AND li.submission_id = s.submission_id AND li.kind = 'booklet' "
            " AND li.unavailable = 0 ORDER BY li.item_id DESC LIMIT 1) AS title, "
            "(SELECT li.item_id FROM library_items li WHERE li.user_id = s.user_id "
            " AND li.submission_id = s.submission_id AND li.kind = 'booklet' "
            " AND li.unavailable = 0 ORDER BY li.item_id DESC LIMIT 1) AS booklet_item_id, "
            "EXISTS(SELECT 1 FROM submission_binary_artifacts b "
            " WHERE b.submission_id = s.submission_id AND b.user_id = s.user_id "
            " AND b.kind = 'booklet') AS has_pdf, "
            "EXISTS(SELECT 1 FROM submission_binary_artifacts b "
            " WHERE b.submission_id = s.submission_id AND b.user_id = s.user_id "
            " AND b.kind = 'docx') AS has_docx, "
            "EXISTS(SELECT 1 FROM transcriptions t WHERE t.submission_id = s.submission_id "
            " AND t.speaker_turns_json IS NOT NULL AND t.speaker_turns_json != '') "
            " AS has_speakers "
            "FROM submissions s WHERE s.user_id = ? "
            "ORDER BY s.submission_id DESC LIMIT ?",
            (int(user_id), max(1, min(100, int(limit)))),
        )
        return [
            {
                "id": int(row["submission_id"]),
                "kind": str(row["kind"] or ""),
                "filename": str(row["filename"] or ""),
                "status": str(row["status"] or ""),
                "created_at": str(row["created_at"] or ""),
                "finished_at": str(row["finished_at"] or ""),
                "duration_seconds": (
                    int(row["duration_seconds"]) if row["duration_seconds"] else None
                ),
                "title": str(row["title"] or row["filename"] or ""),
                "booklet_item_id": (
                    int(row["booklet_item_id"]) if row["booklet_item_id"] else None
                ),
                "has_pdf": bool(row["has_pdf"]),
                "has_docx": bool(row["has_docx"]),
                "has_speakers": bool(row["has_speakers"]),
            }
            for row in rows
        ]

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
        connection = self._connect()
        try:
            has_structured = _has_column(connection, "library_items", "structured_json")
            structured_column = ", structured_json" if has_structured else ", NULL AS structured_json"
            rows = connection.execute(
                "SELECT item_id, title, content_text, unavailable, submission_id, created_at"
                + structured_column
                + " FROM library_items WHERE item_id = ? AND user_id = ? AND kind = 'booklet'",
                (int(item_id), int(user_id)),
            ).fetchall()
            if not rows:
                return None
            row = rows[0]
            submission_id = row["submission_id"]
            artifacts: set[str] = set()
            if submission_id:
                artifacts = {
                    str(kind_row[0])
                    for kind_row in connection.execute(
                        "SELECT kind FROM submission_binary_artifacts "
                        "WHERE submission_id = ? AND user_id = ?",
                        (int(submission_id), int(user_id)),
                    ).fetchall()
                }
            return {
                "id": int(row["item_id"]),
                "title": str(row["title"] or ""),
                # An empty cache is an empty editor, not a missing booklet: the user
                # can write the text they want their quiz built from.
                "content_text": str(row["content_text"] or ""),
                "editable": not bool(row["unavailable"]),
                "submission_id": int(submission_id) if submission_id else None,
                "created_at": str(row["created_at"] or ""),
                "structured_json": (
                    str(row["structured_json"] or "") or None
                ),
                "available": sorted(artifacts),
            }
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            if not _has_table(connection, "booklet_revisions"):
                return []
            has_structured = _has_column(connection, "booklet_revisions", "structured_json")
            structured_expr = (
                "(r.structured_json IS NOT NULL AND r.structured_json != '')"
                if has_structured
                else "0"
            )
            rows = connection.execute(
                "SELECT r.revision_id, r.created_at, r.source, "
                f"LENGTH(r.content_text) AS chars, {structured_expr} AS structured "
                "FROM booklet_revisions r "
                "JOIN library_items l ON l.item_id = r.item_id "
                "WHERE r.item_id = ? AND r.user_id = ? AND l.user_id = ? "
                "ORDER BY r.revision_id DESC LIMIT ?",
                (int(item_id), int(user_id), int(user_id), max(1, min(50, int(limit)))),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "id": int(row["revision_id"]),
                "created_at": str(row["created_at"] or ""),
                "source": str(row["source"] or ""),
                "chars": int(row["chars"] or 0),
                "structured": bool(row["structured"]),
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

    def _revision_structured(self, user_id: int, revision_id: int) -> str | None:
        """The stored canonical payload of one revision, owner-scoped."""
        rows = self._read_optional(
            "SELECT structured_json FROM booklet_revisions "
            "WHERE revision_id = ? AND user_id = ? AND structured_json IS NOT NULL",
            (int(revision_id), int(user_id)),
        )
        if not rows:
            return None
        raw = rows[0][0]
        return str(raw) if raw else None

    def restore_revision(
        self, user_id: int, item_id: int, revision_id: int
    ) -> bool:
        """Restore a revision as a *new* save (history is append-only).

        A structured revision is restored through the structured path, so the
        PDF and DOCX artifacts are re-rendered from the restored model. A
        legacy text-only revision keeps the historical text-only behaviour.
        """
        raw = self._revision_structured(user_id, revision_id)
        if raw is not None:
            from .notes import StructuredNotes, StructuringError

            try:
                notes = StructuredNotes.from_payload(_json.loads(raw))
            except (StructuringError, ValueError, TypeError):
                notes = None
            if notes is not None:
                result = self.save_structured(user_id, item_id, notes, source="restore")
                return bool(result and result.get("ok"))
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

    # -- structured booklet editing ----------------------------------------
    def _live_speakers(
        self, connection: sqlite3.Connection, submission_id: int | None
    ) -> tuple[str, ...] | None:
        """The booklet's speaker list from stored turns and the owner's names.

        Mirrors the pipeline's ``_booklet_context``: only real turns are used,
        the owner's chosen names win over the Persian defaults, and unknown
        attribution is skipped. ``None`` means "no turns for this submission";
        the caller then keeps the payload's existing list untouched.
        """
        if not submission_id:
            return None
        try:
            row = connection.execute(
                "SELECT speaker_turns_json FROM transcriptions WHERE submission_id = ?",
                (int(submission_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or not row[0]:
            return None
        try:
            raw = _json.loads(str(row[0]))
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, list):
            return None
        names = {
            str(item[0]): str(item[1])
            for item in self._speaker_names_rows(connection, int(submission_id))
        }
        from .progress import to_persian_digits
        from .transcript import speaker_name_from_label

        words_by_speaker: dict[str, int] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            label = item.get("speaker_label")
            if label is None:
                continue
            name = names.get(str(label)) or speaker_name_from_label(str(label))
            if not name:
                continue
            words = len(str(item.get("text") or "").split())
            if not words:
                continue
            words_by_speaker[name] = words_by_speaker.get(name, 0) + words
        return tuple(
            f"{name} — {to_persian_digits(count)} واژه"
            for name, count in sorted(
                words_by_speaker.items(), key=lambda pair: (-pair[1], pair[0])
            )[:12]
        )

    def _prune_revisions(self, connection: sqlite3.Connection, item_id: int) -> None:
        """Keep the 20 newest revisions; clear artifact pointers to pruned ones."""
        keep = connection.execute(
            "SELECT revision_id FROM booklet_revisions WHERE item_id = ? "
            "ORDER BY revision_id DESC LIMIT 20",
            (int(item_id),),
        ).fetchall()
        if not keep:
            return
        keep_ids = tuple(int(row[0]) for row in keep)
        placeholders = ",".join("?" for _ in keep_ids)
        pruned = connection.execute(
            "SELECT revision_id FROM booklet_revisions WHERE item_id = ? "
            f"AND revision_id NOT IN ({placeholders})",
            (int(item_id), *keep_ids),
        ).fetchall()
        if not pruned:
            return
        pruned_ids = tuple(int(row[0]) for row in pruned)
        pruned_placeholders = ",".join("?" for _ in pruned_ids)
        connection.execute(
            f"DELETE FROM booklet_revisions WHERE revision_id IN ({pruned_placeholders})",
            pruned_ids,
        )
        if _has_table(connection, "submission_binary_artifacts") and _has_column(
            connection, "submission_binary_artifacts", "revision_id"
        ):
            connection.execute(
                "UPDATE submission_binary_artifacts SET revision_id = NULL "
                f"WHERE revision_id IN ({pruned_placeholders})",
                pruned_ids,
            )

    def save_structured(
        self,
        user_id: int,
        item_id: int,
        notes: Any,
        *,
        source: str = "studio",
    ) -> dict[str, Any] | None:
        """Persist one validated structured booklet edit, then re-render files.

        Order matters and is the contract the revision lifecycle is built on:

        1. one ``BEGIN IMMEDIATE`` transaction: ownership check, append the
           *previous* state once (the pipeline original is never lost), update
           the editable caches, append the *new* state as the newest revision;
        2. after the commit, render the PDF and DOCX from the same model and
           persist them as the submission's binary artifacts, tagged with the
           new revision id.

        A render failure therefore cannot corrupt the revision (step 1 already
        committed) and cannot destroy the previously delivered files (the old
        artifact row is only replaced by a successful render). The response
        reports which formats were actually regenerated.
        """
        from dataclasses import replace

        connection = self._connect(write=True)
        try:
            if not _has_column(connection, "library_items", "structured_json"):
                return {"ok": False, "reason": "migration"}
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT submission_id, COALESCE(content_text, ''), "
                "COALESCE(structured_json, '') FROM library_items "
                "WHERE item_id = ? AND user_id = ? AND kind = 'booklet' "
                "AND unavailable = 0",
                (int(item_id), int(user_id)),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            submission_id = row[0]
            previous_text = str(row[1] or "")
            previous_structured = str(row[2] or "")

            # The owner's current speaker names are source-derived (their own
            # renames), so the regenerated booklet shows them — never an
            # inferred identity.
            speakers = self._live_speakers(connection, submission_id)
            if speakers is not None:
                notes = replace(notes, speakers=speakers)
            structured_json = notes.to_json()
            markdown = notes.to_markdown()

            has_revisions = _has_table(connection, "booklet_revisions")
            revision_columns = has_revisions and _has_column(
                connection, "booklet_revisions", "structured_json"
            )
            new_revision_id = 0
            if revision_columns:
                existing = connection.execute(
                    "SELECT COUNT(*) FROM booklet_revisions WHERE item_id = ?",
                    (int(item_id),),
                ).fetchone()
                if not existing or int(existing[0]) == 0:
                    # The first edit preserves the pipeline original as
                    # revision 1; every state stays recoverable afterwards.
                    connection.execute(
                        "INSERT INTO booklet_revisions(item_id, user_id, content_text, "
                        "structured_json, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            int(item_id),
                            int(user_id),
                            previous_text,
                            previous_structured or None,
                            "original",
                            _utc_now(),
                        ),
                    )
                cursor = connection.execute(
                    "INSERT INTO booklet_revisions(item_id, user_id, content_text, "
                    "structured_json, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        int(item_id),
                        int(user_id),
                        markdown,
                        structured_json,
                        str(source)[:32],
                        _utc_now(),
                    ),
                )
                new_revision_id = int(cursor.lastrowid or 0)
            connection.execute(
                "UPDATE library_items SET content_text = ?, structured_json = ? "
                "WHERE item_id = ? AND user_id = ? AND kind = 'booklet' "
                "AND unavailable = 0",
                (markdown, structured_json, int(item_id), int(user_id)),
            )
            if revision_columns:
                self._prune_revisions(connection, item_id)
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - nothing left to roll back
                pass
            raise
        finally:
            connection.close()

        rendered = self._persist_rendered_artifacts(
            user_id, item_id, submission_id, notes, new_revision_id
        )
        logger.info(
            "mini app structured edit item=%s user=%s revision=%s rendered=%s",
            item_id, user_id, new_revision_id, ",".join(sorted(rendered)) or "none",
        )
        return {
            "ok": True,
            "revision_id": new_revision_id,
            "submission_id": int(submission_id) if submission_id else None,
            "rendered": sorted(rendered),
        }

    def _persist_rendered_artifacts(
        self,
        user_id: int,
        item_id: int,
        submission_id: int | None,
        notes: Any,
        revision_id: int | None,
    ) -> set[str]:
        """Render PDF+DOCX from one model and upsert the persisted artifacts.

        Each format is independent: a DOCX failure must not block the PDF, and
        neither may take the revision down (it is already committed). The
        artifacts table predates revision tagging; when the 028 column is
        missing the bytes are still stored, just without the revision pointer.
        A ``None`` revision id (pipeline originals, on-demand renders) leaves
        any existing pointer untouched.
        """
        if not submission_id:
            return set()
        rendered: set[str] = set()
        import hashlib
        from datetime import datetime, timezone

        from .docx_export import (
            DocumentMeta,
            build_notes_docx,
            notes_docx_filename,
            resolve_design,
            resolve_fonts,
        )
        from .notes_pdf import build_notes_pdf, notes_pdf_filename

        reference = f"GMS-{int(submission_id)}"
        meta = DocumentMeta(
            reference=reference,
            created_at=datetime.now(timezone.utc),
        )
        outputs: list[tuple[str, bytes, str, str]] = []
        try:
            outputs.append(
                (
                    "booklet",
                    build_notes_pdf(notes, meta=meta),
                    "application/pdf",
                    notes_pdf_filename(notes, reference),
                )
            )
        except Exception:  # noqa: BLE001 - one format failing must not kill the save
            logger.exception("studio PDF render failed item=%s", item_id)
        try:
            outputs.append(
                (
                    "docx",
                    build_notes_docx(
                        notes, fonts=resolve_fonts(), design=resolve_design(), meta=meta
                    ),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    notes_docx_filename(notes, reference),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("studio DOCX render failed item=%s", item_id)

        connection = self._connect(write=True)
        try:
            has_revision_column = _has_column(
                connection, "submission_binary_artifacts", "revision_id"
            )
            connection.execute("BEGIN IMMEDIATE")
            for kind, content, content_type, filename in outputs:
                if not content:
                    continue
                digest = hashlib.sha256(content).hexdigest()
                now = _utc_now()
                if has_revision_column and revision_id:
                    connection.execute(
                        "INSERT INTO submission_binary_artifacts(submission_id, user_id, "
                        "kind, filename, content_type, content, sha256, created_at, "
                        "updated_at, revision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(submission_id, kind) DO UPDATE SET "
                        "user_id = excluded.user_id, filename = excluded.filename, "
                        "content_type = excluded.content_type, content = excluded.content, "
                        "sha256 = excluded.sha256, updated_at = excluded.updated_at, "
                        "revision_id = excluded.revision_id",
                        (
                            int(submission_id), int(user_id), kind, filename,
                            content_type, content, digest, now, now, int(revision_id),
                        ),
                    )
                else:
                    connection.execute(
                        "INSERT INTO submission_binary_artifacts(submission_id, user_id, "
                        "kind, filename, content_type, content, sha256, created_at, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(submission_id, kind) DO UPDATE SET "
                        "user_id = excluded.user_id, filename = excluded.filename, "
                        "content_type = excluded.content_type, content = excluded.content, "
                        "sha256 = excluded.sha256, updated_at = excluded.updated_at",
                        (
                            int(submission_id), int(user_id), kind, filename,
                            content_type, content, digest, now, now,
                        ),
                    )
                rendered.add(kind)
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            logger.exception(
                "studio artifact persistence failed item=%s submission=%s",
                item_id, submission_id,
            )
        finally:
            connection.close()
        return rendered

    def export_bytes(
        self, user_id: int, item_id: int, kind: str
    ) -> tuple[bytes, str, str] | None:
        """The booklet's current PDF/DOCX bytes: stored first, rendered never twice.

        The persisted artifact (written by the pipeline or the latest Studio
        save) is served as-is, so repeated downloads are free and idempotent.
        Only when it is missing — a save whose render failed, or a legacy
        booklet — is a render attempted, and a successful one is persisted so
        the next download is again a pure retrieval.
        """
        # ``pdf`` is the API-facing name; the artifact table calls it ``booklet``.
        artifact_kind = "booklet" if kind in {"pdf", "booklet"} else kind
        if artifact_kind not in {"booklet", "docx"}:
            return None
        record = self.booklet(user_id, item_id)
        if record is None:
            return None
        raw_submission_id = record.get("submission_id")
        submission_id = (
            raw_submission_id
            if isinstance(raw_submission_id, int) and raw_submission_id > 0
            else None
        )
        if submission_id is not None:
            rows = self._read_optional(
                "SELECT filename, content_type, content FROM submission_binary_artifacts "
                "WHERE submission_id = ? AND user_id = ? AND kind = ?",
                (submission_id, int(user_id), artifact_kind),
            )
            if rows and rows[0][2]:
                content = bytes(rows[0][2])
                if content:
                    return content, str(rows[0][0]), str(rows[0][1])
        raw = record.get("structured_json")
        if not raw or submission_id is None:
            return None
        from .notes import StructuredNotes, StructuringError

        try:
            notes = StructuredNotes.from_payload(_json.loads(raw))
        except (StructuringError, ValueError, TypeError):
            return None
        rendered = self._persist_rendered_artifacts(
            user_id, item_id, submission_id, notes, revision_id=None
        )
        if artifact_kind not in rendered:
            return None
        # Read back what was just persisted (one level, no recursion): the
        # upsert already stored the exact bytes to serve.
        rows = self._read_optional(
            "SELECT filename, content_type, content FROM submission_binary_artifacts "
            "WHERE submission_id = ? AND user_id = ? AND kind = ?",
            (submission_id, int(user_id), artifact_kind),
        )
        if rows and rows[0][2]:
            content = bytes(rows[0][2])
            if content:
                return content, str(rows[0][0]), str(rows[0][1])
        return None
