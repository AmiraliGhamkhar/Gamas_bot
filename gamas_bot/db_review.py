"""Review persistence slice: Study Pack-owned questions, answers, and publication state.

Review is a *view* over content the pipelines already produced, never a second
copy of it. The source relationship is explicit:

* a question belongs to a ``quiz_sessions`` row (its generation/execution run),
* the session belongs to a Study Pack through ``quiz_sessions.pack_id``, which
  migration 039 adds and the generation path fills from the session's source
  ``library_items`` row,
* anything the pack link cannot be established for is returned as a **legacy**
  record grouped by its real library item — never attached to an unrelated pack.

Ownership is enforced twice on every read: the session must belong to the user
*and* the pack it points at must belong to the same user, which is why a pack
that has been deleted (soft-deleted) silently drops out of Review instead of
keeping stale titles alive.
"""

from __future__ import annotations

import json
from typing import Any

from .db_core import _rows, iso

#: Publication states of a question that was offered to Explore.
PUBLICATION_STATES: frozenset[str] = frozenset({"pending", "approved", "rejected"})

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


def _limit(value: int, default: int = _DEFAULT_LIMIT) -> int:
    return max(1, min(_MAX_LIMIT, int(value or default)))


def _offset(value: int) -> int:
    return max(0, int(value or 0))


def _group_key(pack_id: Any, item_id: Any) -> str:
    """Stable identifier of one review group (a pack, or a legacy library row)."""
    if pack_id is not None:
        return f"pack:{int(pack_id)}"
    return f"legacy:{int(item_id)}"


class ReviewRepository:
    """Single-responsibility persistence slice for the Review screen."""

    def __init__(self, core: Any) -> None:
        self._core = core

    # ---------------------------------------------------------------- queries
    #: One question row, joined to its owner-scoped pack and its answer state.
    #: ``correct_index``/``explanation`` are selected but only published to the
    #: caller for questions the user has already answered (see ``_public``).
    _QUESTION_SELECT = """
        SELECT q.question_id, q.session_id, q.idx, q.question, q.options_json,
               q.correct_index, q.explanation,
               s.pack_id AS session_pack_id, s.item_id, s.created_at AS session_at,
               p.pack_id AS pack_id, p.title AS pack_title,
               p.source_type AS pack_source_type, p.status AS pack_status,
               i.title AS item_title, i.kind AS item_kind,
               a.chosen_index, a.is_correct, a.answered_at,
               e.status AS publication_status
        FROM quiz_questions q
        JOIN quiz_sessions s ON s.session_id = q.session_id
        LEFT JOIN study_packs p
               ON p.pack_id = s.pack_id AND p.user_id = s.user_id
              AND p.deleted_at IS NULL
        LEFT JOIN library_items i
               ON i.item_id = s.item_id AND i.user_id = s.user_id
        LEFT JOIN quiz_answers a ON a.question_id = q.question_id
        LEFT JOIN explore_questions e
               ON e.source_question_id = q.question_id AND e.created_by = s.user_id
    """

    #: A question is visible when its pack resolves for this user, or when it has
    #: no pack at all and its own source row does (a legacy record). A session
    #: pointing at a deleted pack therefore disappears instead of dangling.
    _QUESTION_VISIBLE = (
        " AND (p.pack_id IS NOT NULL "
        "OR (s.pack_id IS NULL AND i.item_id IS NOT NULL))"
    )

    async def review_question_count(self, user_id: int, pack_id: int) -> int:
        """How many reviewable questions one owned pack holds (for its card).

        Same visibility rule as :meth:`review_questions`: the session must belong
        to the user and point at the pack that belongs to the same user.
        """
        rows = await _rows(
            self._core.read_db,
            """
            SELECT COUNT(*) AS cnt
            FROM quiz_questions q
            JOIN quiz_sessions s ON s.session_id = q.session_id
            JOIN study_packs p
              ON p.pack_id = s.pack_id AND p.user_id = s.user_id
             AND p.deleted_at IS NULL
            WHERE s.user_id = ? AND s.pack_id = ?
            """,
            (int(user_id), int(pack_id)),
        )
        return int(rows[0]["cnt"]) if rows else 0

    async def review_groups(
        self, user_id: int, *, limit: int = _DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Study Packs that own questions, plus legacy groups, newest first."""
        limit = _limit(limit)
        offset = _offset(offset)
        # Both halves are bounded by the same window so the merge below can slice
        # deterministically without loading a user's whole history.
        window = limit + offset
        pack_rows = await _rows(
            self._core.read_db,
            """
            SELECT p.pack_id, p.title, p.source_type, p.status AS processing_status,
                   COUNT(q.question_id) AS question_count,
                   SUM(CASE WHEN a.answered_at IS NOT NULL THEN 1 ELSE 0 END)
                       AS answered_count,
                   SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END) AS correct_count,
                   MAX(s.created_at) AS latest_at
            FROM study_packs p
            JOIN quiz_sessions s ON s.pack_id = p.pack_id AND s.user_id = p.user_id
            JOIN quiz_questions q ON q.session_id = s.session_id
            LEFT JOIN quiz_answers a ON a.question_id = q.question_id
            WHERE p.user_id = ? AND p.deleted_at IS NULL
            GROUP BY p.pack_id
            ORDER BY latest_at DESC, p.pack_id DESC
            LIMIT ?
            """,
            (int(user_id), int(window)),
        )
        legacy_rows = await _rows(
            self._core.read_db,
            """
            SELECT s.item_id AS item_id, i.title AS title, i.kind AS item_kind,
                   COUNT(q.question_id) AS question_count,
                   SUM(CASE WHEN a.answered_at IS NOT NULL THEN 1 ELSE 0 END)
                       AS answered_count,
                   SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END) AS correct_count,
                   MAX(s.created_at) AS latest_at
            FROM quiz_sessions s
            JOIN library_items i ON i.item_id = s.item_id AND i.user_id = s.user_id
            JOIN quiz_questions q ON q.session_id = s.session_id
            LEFT JOIN quiz_answers a ON a.question_id = q.question_id
            WHERE s.user_id = ? AND s.pack_id IS NULL
            GROUP BY s.item_id
            ORDER BY latest_at DESC, s.item_id DESC
            LIMIT ?
            """,
            (int(user_id), int(window)),
        )
        groups: list[dict[str, Any]] = []
        for row in pack_rows:
            record = dict(row)
            groups.append(
                {
                    "group_key": _group_key(record["pack_id"], None),
                    "pack_id": int(record["pack_id"]),
                    "title": str(record["title"] or ""),
                    "source_type": str(record["source_type"] or ""),
                    "processing_status": str(record["processing_status"] or ""),
                    "legacy": False,
                    "question_count": int(record["question_count"] or 0),
                    "answered_count": int(record["answered_count"] or 0),
                    "correct_count": int(record["correct_count"] or 0),
                    "latest_at": str(record["latest_at"] or ""),
                }
            )
        for row in legacy_rows:
            record = dict(row)
            groups.append(
                {
                    "group_key": _group_key(None, record["item_id"]),
                    "pack_id": None,
                    "title": str(record["title"] or ""),
                    "source_type": str(record["item_kind"] or ""),
                    "processing_status": "",
                    "legacy": True,
                    "question_count": int(record["question_count"] or 0),
                    "answered_count": int(record["answered_count"] or 0),
                    "correct_count": int(record["correct_count"] or 0),
                    "latest_at": str(record["latest_at"] or ""),
                }
            )
        groups.sort(key=lambda g: (g["latest_at"], g["group_key"]), reverse=True)
        return groups[offset : offset + limit], len(groups)

    async def review_questions(
        self,
        user_id: int,
        *,
        pack_id: int | None = None,
        legacy_item_id: int | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Questions of one group (or all of them), ready for the Review screen."""
        limit = _limit(limit)
        offset = _offset(offset)
        where = " WHERE s.user_id = ?" + self._QUESTION_VISIBLE
        params: list[Any] = [int(user_id)]
        if pack_id is not None:
            where += " AND p.pack_id = ?"
            params.append(int(pack_id))
        elif legacy_item_id is not None:
            where += " AND s.pack_id IS NULL AND s.item_id = ?"
            params.append(int(legacy_item_id))
        total_rows = await _rows(
            self._core.read_db,
            "SELECT COUNT(*) AS total FROM ("
            "SELECT q.question_id FROM quiz_questions q "
            "JOIN quiz_sessions s ON s.session_id = q.session_id "
            "LEFT JOIN study_packs p ON p.pack_id = s.pack_id AND p.user_id = s.user_id "
            "AND p.deleted_at IS NULL "
            "LEFT JOIN library_items i ON i.item_id = s.item_id AND i.user_id = s.user_id"
            + where
            + " GROUP BY q.question_id)",
            tuple(params),
        )
        rows = await _rows(
            self._core.read_db,
            self._QUESTION_SELECT
            + where
            + " ORDER BY s.created_at ASC, s.session_id ASC, q.idx ASC"
            " LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
        questions = [self._public(dict(row)) for row in rows]
        total = int(total_rows[0]["total"]) if total_rows else 0
        return questions, total

    async def review_question(
        self, user_id: int, question_id: int
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self._core.read_db,
            self._QUESTION_SELECT
            + " WHERE s.user_id = ? AND q.question_id = ?"
            + self._QUESTION_VISIBLE,
            (int(user_id), int(question_id)),
        )
        return self._public(dict(rows[0])) if rows else None

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        """Shape one question for the client without leaking unsolved answers."""
        pack_id = row.get("pack_id")
        item_id = row.get("item_id")
        answered = row.get("answered_at") is not None
        question: dict[str, Any] = {
            "question_id": int(row["question_id"]),
            "session_id": int(row["session_id"]),
            "group_key": _group_key(pack_id, item_id),
            "pack_id": int(pack_id) if pack_id is not None else None,
            "pack_title": str(row.get("pack_title") or ""),
            "legacy": pack_id is None,
            "idx": int(row["idx"]),
            "question": str(row["question"]),
            "options": _options(row.get("options_json")),
            "answered": None,
            "publication_status": (
                str(row["publication_status"])
                if row.get("publication_status") in PUBLICATION_STATES
                else None
            ),
        }
        if answered:
            # The correct answer and its explanation are only ever revealed for a
            # question the user already answered; the list endpoint must not be a
            # way to read the answer key of an unanswered quiz.
            question["answered"] = {
                "chosen_index": int(row["chosen_index"]),
                "is_correct": bool(row["is_correct"]),
                "answered_at": str(row["answered_at"]),
                "correct_index": int(row["correct_index"]),
                "explanation": str(row["explanation"] or ""),
            }
        return question

    # ----------------------------------------------------------------- answers
    async def review_answer(
        self, user_id: int, question_id: int, chosen_index: int
    ) -> dict[str, Any] | None:
        """Record one answer once, and always return the stored state.

        ``quiz_answers`` is unique per question (the schema's once-only answering
        rule), so a repeated tap re-reads the existing answer instead of raising
        an ``IntegrityError``: Review is a study surface, not a scoring surface.
        """
        if not 0 <= int(chosen_index) <= 3:
            raise ValueError("invalid choice")
        async with self._core._transaction():
            rows = await _rows(
                self._core.db,
                self._QUESTION_SELECT
                + " WHERE s.user_id = ? AND q.question_id = ?"
                + self._QUESTION_VISIBLE,
                (int(user_id), int(question_id)),
            )
            if not rows:
                return None
            row = dict(rows[0])
            if row.get("answered_at") is None:
                correct_index = int(row["correct_index"])
                await self._core.db.execute(
                    "INSERT INTO quiz_answers(session_id, question_id, chosen_index, "
                    "is_correct, answered_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        int(row["session_id"]),
                        int(question_id),
                        int(chosen_index),
                        int(int(chosen_index) == correct_index),
                        iso(self._core._now()),
                    ),
                )
                row["chosen_index"] = int(chosen_index)
                row["is_correct"] = int(int(chosen_index) == correct_index)
            return {
                "is_correct": bool(row["is_correct"]),
                "correct_index": int(row["correct_index"]),
                "explanation": str(row["explanation"] or ""),
                "already_answered": row.get("answered_at") is not None,
            }

    # ------------------------------------------------------------ publication
    async def review_share_candidate(
        self, user_id: int, question_id: int
    ) -> dict[str, Any] | None:
        """The fields needed to offer one owned question to Explore."""
        rows = await _rows(
            self._core.read_db,
            self._QUESTION_SELECT
            + " WHERE s.user_id = ? AND q.question_id = ?"
            + self._QUESTION_VISIBLE,
            (int(user_id), int(question_id)),
        )
        if not rows:
            return None
        row = dict(rows[0])
        return {
            "question_id": int(row["question_id"]),
            "question": str(row["question"]),
            "options_json": str(row["options_json"]),
            "correct_index": int(row["correct_index"]),
            "explanation": str(row["explanation"] or ""),
            "publication_status": (
                str(row["publication_status"])
                if row.get("publication_status") in PUBLICATION_STATES
                else None
            ),
        }

    async def review_share(
        self, user_id: int, question_id: int
    ) -> dict[str, Any] | None:
        """Offer one owned question to Explore exactly once.

        The ``(created_by, source_question_id)`` unique index makes a repeated
        request return the existing row (with its current moderation status), so
        multi-select publication cannot pile up duplicates. Nothing about the
        Explore feed's ranking changes here.
        """
        async with self._core._transaction():
            candidate = await self._share_candidate_locked(int(user_id), int(question_id))
            if candidate is None:
                return None
            existing = await _rows(
                self._core.db,
                "SELECT question_id, status FROM explore_questions "
                "WHERE created_by = ? AND source_question_id = ?",
                (int(user_id), int(question_id)),
            )
            if existing:
                return {
                    "shared": True,
                    "explore_question_id": int(existing[0]["question_id"]),
                    "status": str(existing[0]["status"]),
                    "already_shared": True,
                }
            source_title = str(candidate.get("source_title") or "").strip()
            topic = source_title[:48] if source_title else "عمومی"
            cursor = await self._core.db.execute(
                """
                INSERT INTO explore_questions(
                    topic, difficulty, question_text, options_json,
                    correct_index, explanation, status, created_by, created_at,
                    source_question_id
                ) VALUES (?, 'medium', ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    topic,
                    candidate["question"],
                    candidate["options_json"],
                    candidate["correct_index"],
                    candidate["explanation"],
                    int(user_id),
                    iso(self._core._now()),
                    int(question_id),
                ),
            )
            return {
                "shared": True,
                "explore_question_id": int(cursor.lastrowid or 0),
                "status": "pending",
                "already_shared": False,
            }

    async def _share_candidate_locked(
        self, user_id: int, question_id: int
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self._core.db,
            self._QUESTION_SELECT
            + " WHERE s.user_id = ? AND q.question_id = ?"
            + self._QUESTION_VISIBLE,
            (int(user_id), int(question_id)),
        )
        if not rows:
            return None
        row = dict(rows[0])
        source_title = str(row.get("pack_title") or row.get("item_title") or "").strip()
        source_kind = str(row.get("pack_source_type") or row.get("item_kind") or "").strip()
        return {
            "question": str(row["question"]),
            "options_json": str(row["options_json"]),
            "correct_index": int(row["correct_index"]),
            "explanation": str(row["explanation"] or ""),
            "source_title": source_title,
            "source_kind": source_kind,
        }


def _options(options_json: Any) -> list[str]:
    try:
        parsed = json.loads(str(options_json or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(option) for option in parsed]
