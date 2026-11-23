"""Quiz sessions repository (SRP).

Historical flashcard decks/cards stay in the database untouched; the feature
was removed from the product and the tables are kept for data preservation.
"""

from __future__ import annotations

from datetime import timedelta
import json
import sqlite3
from typing import Any

from .db_billing import BillingRepositoryMixin
from .db_core import _rows, iso
from .db_users import UsersRepositoryMixin
from .metrics import METRICS


class LearningRepositoryMixin(UsersRepositoryMixin, BillingRepositoryMixin):
    """Single-responsibility persistence slice for quizzes."""

    async def quiz_admission(
        self,
        user_id: int,
        *,
        max_active_hours: int,
        max_sessions_per_hour: int,
    ) -> str | None:
        now = self._now()
        cutoff = iso(now - timedelta(hours=max_active_hours))
        hour_ago = iso(now - timedelta(hours=1))
        async with self._transaction():
            await self.db.execute(
                "UPDATE quiz_sessions SET status = 'expired', updated_at = ? "
                "WHERE user_id = ? AND status = 'active' AND updated_at < ?",
                (iso(now), int(user_id), cutoff),
            )
            active = await _rows(
                self.db,
                "SELECT 1 FROM quiz_sessions WHERE user_id = ? AND status = 'active' "
                "LIMIT 1",
                (int(user_id),),
            )
            if active:
                return "active"
            # Quiz sessions are written with iso(), a fixed UTC ISO timestamp.
            recent = await _rows(
                self.db,
                "SELECT COUNT(*) FROM quiz_sessions WHERE user_id = ? "
                "AND created_at >= ?",
                (int(user_id), hour_ago),
            )
            if recent and int(recent[0][0]) >= int(max_sessions_per_hour):
                return "rate"
            return None

    async def quiz_create_session(
        self,
        user_id: int,
        item_id: int,
        questions: list[dict[str, Any]],
        *,
        status: str = "active",
        free_claim_id: str | None = None,
        paid_charge_id: str | None = None,
        refund_seconds_value: int = 0,
    ) -> int:
        if status not in ("active", "done"):
            raise ValueError(f"invalid quiz session status {status!r}")
        if not questions:
            raise ValueError("quiz session needs at least one question")
        for question in questions:
            if (not isinstance(question, dict) or not isinstance(question.get("question"), str)
                    or not isinstance(question.get("options"), list)
                    or len(question["options"]) != 4
                    or not all(isinstance(option, str) for option in question["options"])
                    or isinstance(question.get("correct_index"), bool)
                    or not isinstance(question.get("correct_index"), int)
                    or not 0 <= question["correct_index"] < 4):
                METRICS.record_learning_validation_failure()
                raise ValueError("invalid quiz question shape")
        now = iso(self._now())
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO quiz_sessions(user_id, item_id, question_count, "
                "current_index, score, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0, ?, ?, ?)",
                (int(user_id), int(item_id), len(questions), status, now, now),
            )
            session_id = int(cursor.lastrowid or 0)
            await self.db.executemany(
                "INSERT INTO quiz_questions(session_id, idx, question, "
                "options_json, correct_index, explanation, source, difficulty) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ((session_id, idx, str(question["question"]),
                  json.dumps(question["options"], ensure_ascii=False),
                  int(question["correct_index"]), str(question.get("explanation", "")),
                  str(question.get("source", ""))[:120], str(question.get("difficulty", ""))[:20])
                 for idx, question in enumerate(questions)),
            )
            await self._finalize_free_use_locked(user_id, "quiz", free_claim_id)
            await self._finalize_learning_charge_locked(user_id, paid_charge_id, refund_seconds_value)
            return session_id

    async def quiz_get_session(
        self, session_id: int, user_id: int
    ) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT session_id, user_id, item_id, question_count, current_index, "
            "score, status, created_at, updated_at FROM quiz_sessions "
            "WHERE session_id = ? AND user_id = ?",
            (int(session_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def quiz_current_question(self, session_id: int) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT q.question_id, q.idx, q.question, q.options_json, "
            "q.correct_index, q.explanation, q.source, q.difficulty, s.current_index, s.question_count "
            "FROM quiz_questions q JOIN quiz_sessions s ON s.session_id = q.session_id "
            "WHERE q.session_id = ? AND s.status = 'active' "
            "AND q.idx = s.current_index",
            (int(session_id),),
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["options"] = json.loads(row.pop("options_json"))
        return row

    async def quiz_record_answer(
        self, session_id: int, question_id: int, chosen_index: int
    ) -> dict[str, Any]:
        now = iso(self._now())
        async with self._transaction():
            session_rows = await _rows(
                self.db,
                "SELECT question_count, current_index, status FROM quiz_sessions "
                "WHERE session_id = ?",
                (int(session_id),),
            )
            if not session_rows or session_rows[0]["status"] != "active":
                return {"status": "stale"}
            question_count = int(session_rows[0]["question_count"])
            current_index = int(session_rows[0]["current_index"])
            question_rows = await _rows(
                self.db,
                "SELECT question_id, idx, options_json, correct_index, explanation "
                "FROM quiz_questions WHERE question_id = ? AND session_id = ?",
                (int(question_id), int(session_id)),
            )
            if not question_rows or not 0 <= int(chosen_index) <= 3:
                return {"status": "stale"}
            existing = await _rows(
                self.db,
                "SELECT 1 FROM quiz_answers WHERE question_id = ?",
                (int(question_id),),
            )
            if existing:
                return {"status": "already"}
            if int(question_rows[0]["idx"]) != current_index:
                return {"status": "stale"}
            question = question_rows[0]
            correct_index = int(question["correct_index"])
            is_correct = int(chosen_index) == correct_index
            try:
                await self.db.execute(
                    "INSERT INTO quiz_answers(session_id, question_id, "
                    "chosen_index, is_correct, answered_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        int(session_id),
                        int(question_id),
                        int(chosen_index),
                        int(is_correct),
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                METRICS.record_db_error()
                await self.db.rollback()
                return {"status": "already"}
            if is_correct:
                updated = await self.db.execute(
                    "UPDATE quiz_sessions SET score = score + 10, "
                    "current_index = current_index + 1, updated_at = ? "
                    "WHERE session_id = ? AND current_index = ?",
                    (now, int(session_id), current_index),
                )
                if updated.rowcount != 1:
                    raise sqlite3.IntegrityError("quiz cursor changed during answer")
            else:
                updated = await self.db.execute(
                    "UPDATE quiz_sessions SET current_index = current_index + 1, "
                    "updated_at = ? WHERE session_id = ? AND current_index = ?",
                    (now, int(session_id), current_index),
                )
                if updated.rowcount != 1:
                    raise sqlite3.IntegrityError("quiz cursor changed during answer")
            finished = current_index + 1 >= question_count
            if finished:
                await self.db.execute(
                    "UPDATE quiz_sessions SET status = 'done', updated_at = ? "
                    "WHERE session_id = ?",
                    (now, int(session_id)),
                )
            score_rows = await _rows(
                self.db,
                "SELECT score FROM quiz_sessions WHERE session_id = ?",
                (int(session_id),),
            )
            return {
                "status": "ok",
                "correct": is_correct,
                "chosen_index": int(chosen_index),
                "correct_index": correct_index,
                "options": json.loads(question["options_json"]),
                "explanation": str(question["explanation"] or ""),
                "finished": finished,
                "score": int(score_rows[0]["score"]) if score_rows else 0,
                "total": question_count,
            }

    async def quiz_finish(self, session_id: int, status: str) -> None:
        if status not in ("quit", "expired", "done"):
            raise ValueError(f"invalid terminal quiz status {status!r}")
        await self._write(
            "UPDATE quiz_sessions SET status = ?, updated_at = ? "
            "WHERE session_id = ? AND status = 'active'",
            (status, iso(self._now()), int(session_id)),
        )
