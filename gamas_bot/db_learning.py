"""Quiz sessions, flashcard decks, and leaderboard repository (SRP)."""

from __future__ import annotations

from datetime import timedelta
import json
import sqlite3
from typing import Any

from .db_billing import BillingRepositoryMixin
from .db_core import _rows, iso
from .db_users import UsersRepositoryMixin


class LearningRepositoryMixin(UsersRepositoryMixin, BillingRepositoryMixin):
    """Single-responsibility persistence slice for quizzes, decks, and leaderboards."""

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
        free_claim_id: str | None = None,
        paid_charge_id: str | None = None,
        refund_seconds_value: int = 0,
    ) -> int:
        if not questions:
            raise ValueError("quiz session needs at least one question")
        now = iso(self._now())
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO quiz_sessions(user_id, item_id, question_count, "
                "current_index, score, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0, 'active', ?, ?)",
                (int(user_id), int(item_id), len(questions), now, now),
            )
            session_id = int(cursor.lastrowid or 0)
            for idx, question in enumerate(questions):
                await self.db.execute(
                    "INSERT INTO quiz_questions(session_id, idx, question, "
                    "options_json, correct_index, explanation, source, difficulty) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        idx,
                        str(question["question"]),
                        json.dumps(question["options"], ensure_ascii=False),
                        int(question["correct_index"]),
                        str(question.get("explanation", "")),
                        str(question.get("source", ""))[:120],
                        str(question.get("difficulty", ""))[:20],
                    ),
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
                return {"status": "already"}
            if is_correct:
                await self.db.execute(
                    "UPDATE quiz_sessions SET score = score + 10, "
                    "current_index = current_index + 1, updated_at = ? "
                    "WHERE session_id = ?",
                    (now, int(session_id)),
                )
                await self.db.execute(
                    "UPDATE users SET quiz_points = quiz_points + 10 "
                    "WHERE telegram_id = (SELECT user_id FROM quiz_sessions "
                    "WHERE session_id = ?)",
                    (int(session_id),),
                )
            else:
                await self.db.execute(
                    "UPDATE quiz_sessions SET current_index = current_index + 1, "
                    "updated_at = ? WHERE session_id = ?",
                    (now, int(session_id)),
                )
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

    async def leaderboard_top(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT first_name, quiz_points FROM users "
            "WHERE banned = 0 AND quiz_points > 0 "
            "ORDER BY quiz_points DESC, telegram_id ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [
            {"first_name": str(row[0] or ""), "quiz_points": int(row[1])}
            for row in rows
        ]

    async def leaderboard_rank(self, user_id: int) -> dict[str, int]:
        rows = await _rows(
            self.read_db,
            "SELECT quiz_points FROM users WHERE telegram_id = ?",
            (int(user_id),),
        )
        points = int(rows[0][0]) if rows else 0
        higher = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM users WHERE banned = 0 AND quiz_points > ?",
            (points,),
        )
        return {"rank": (int(higher[0][0]) if higher else 0) + 1, "points": points}

    async def leaderboard_admin_top(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT telegram_id, first_name, quiz_points FROM users "
            "WHERE banned = 0 AND quiz_points > 0 "
            "ORDER BY quiz_points DESC, telegram_id ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [
            {
                "telegram_id": int(row[0]),
                "first_name": str(row[1] or ""),
                "quiz_points": int(row[2]),
            }
            for row in rows
        ]

    async def deck_count(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT COUNT(*) FROM flashcard_decks WHERE user_id = ?",
            (int(user_id),),
        )
        return int(rows[0][0]) if rows else 0

    async def deck_create(
        self,
        user_id: int,
        item_id: int | None,
        title: str,
        source: str,
    ) -> int:
        if source not in ("ai", "manual"):
            raise ValueError(f"invalid deck source {source!r}")
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO flashcard_decks(user_id, item_id, title, source, "
                "study_cursor, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (int(user_id), item_id, str(title), source, iso(self._now())),
            )
            return int(cursor.lastrowid or 0)

    async def deck_create_with_cards(
        self,
        user_id: int,
        item_id: int,
        title: str,
        cards: list[dict[str, str]],
        *,
        free_claim_id: str | None = None,
        paid_charge_id: str | None = None,
        refund_seconds_value: int = 0,
    ) -> int:
        if not cards:
            raise ValueError("a deck needs at least one card")
        now = iso(self._now())
        async with self._transaction():
            cursor = await self.db.execute(
                "INSERT INTO flashcard_decks(user_id, item_id, title, source, "
                "study_cursor, created_at) VALUES (?, ?, ?, 'ai', 0, ?)",
                (int(user_id), int(item_id), str(title), now),
            )
            deck_id = int(cursor.lastrowid or 0)
            for idx, card in enumerate(cards):
                await self.db.execute(
                    "INSERT INTO flashcards(deck_id, idx, front, back, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        deck_id,
                        idx,
                        str(card["front"]),
                        str(card["back"]),
                        str(card.get("source", ""))[:120],
                    ),
                )
            await self._finalize_free_use_locked(user_id, "deck", free_claim_id)
            await self._finalize_learning_charge_locked(user_id, paid_charge_id, refund_seconds_value)
            return deck_id

    async def manual_deck_get_or_create(self, user_id: int) -> int:
        rows = await _rows(
            self.read_db,
            "SELECT deck_id FROM flashcard_decks WHERE user_id = ? "
            "AND source = 'manual' ORDER BY deck_id LIMIT 1",
            (int(user_id),),
        )
        if rows:
            return int(rows[0][0])
        return await self.deck_create(
            user_id, None, "فلش‌کارت‌های دستی من", "manual"
        )

    async def deck_add_card(self, deck_id: int, front: str, back: str) -> int:
        async with self._transaction():
            count_rows = await _rows(
                self.db,
                "SELECT COUNT(*) FROM flashcards WHERE deck_id = ?",
                (int(deck_id),),
            )
            idx = int(count_rows[0][0]) if count_rows else 0
            await self.db.execute(
                "INSERT INTO flashcards(deck_id, idx, front, back) "
                "VALUES (?, ?, ?, ?)",
                (int(deck_id), idx, str(front), str(back)),
            )
            return idx + 1

    async def deck_get(self, deck_id: int, user_id: int) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT deck_id, user_id, item_id, title, source, study_cursor, "
            "created_at FROM flashcard_decks WHERE deck_id = ? AND user_id = ?",
            (int(deck_id), int(user_id)),
        )
        return dict(rows[0]) if rows else None

    async def deck_cards(self, deck_id: int) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT card_id, deck_id, idx, front, back, source FROM flashcards "
            "WHERE deck_id = ? ORDER BY idx",
            (int(deck_id),),
        )
        return [dict(row) for row in rows]

    async def deck_card(self, deck_id: int, card_id: int) -> dict[str, Any] | None:
        rows = await _rows(
            self.read_db,
            "SELECT card_id, idx, front, back, source FROM flashcards "
            "WHERE deck_id = ? AND card_id = ?",
            (int(deck_id), int(card_id)),
        )
        return dict(rows[0]) if rows else None

    async def deck_set_cursor(self, deck_id: int, cursor: int) -> None:
        await self._write(
            "UPDATE flashcard_decks SET study_cursor = ? WHERE deck_id = ?",
            (max(0, int(cursor)), int(deck_id)),
        )

    async def deck_delete(self, deck_id: int, user_id: int) -> bool:
        async with self._transaction():
            cursor = await self.db.execute(
                "DELETE FROM flashcard_decks WHERE deck_id = ? AND user_id = ?",
                (int(deck_id), int(user_id)),
            )
            return cursor.rowcount == 1

    async def decks_list(
        self, user_id: int, *, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await _rows(
            self.read_db,
            "SELECT d.deck_id, d.item_id, d.title, d.source, d.study_cursor, "
            "d.created_at, (SELECT COUNT(*) FROM flashcards c WHERE c.deck_id = d.deck_id) "
            "AS card_count FROM flashcard_decks d WHERE d.user_id = ? "
            "ORDER BY d.deck_id DESC LIMIT ? OFFSET ?",
            (int(user_id), max(1, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in rows]
