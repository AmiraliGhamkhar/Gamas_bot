"""Phase 2: Review is a Study Pack view, not a list of quiz session ids.

The suite covers the relationship (question → session → pack), the grouping and
titles the Mini App renders, ownership isolation, legacy rows, answer state and
the publication hand-off to Explore — against a real database and the real WSGI
app, with the same question shape the bot's 20-question generator produces.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings
from tests.test_webapp_api import BOT_TOKEN, _call_wsgi, _sign_init_data

ALICE = 101
BOB = 202
MIGRATION_040 = (
    Path(__file__).resolve().parents[1] / "migrations" / "v3" / "040_review_pack_links.sql"
)


def questions(count: int, *, prefix: str = "سؤال") -> list[dict]:
    """The exact question shape ``quiz_create_session`` validates and stores."""
    return [
        {
            "question": f"{prefix} {index}؟",
            "options": [
                f"گزینهٔ درست {index}",
                f"انحرافی الف {index}",
                f"انحرافی ب {index}",
                f"انحرافی ج {index}",
            ],
            "correct_index": index % 4,
            "explanation": f"زیرا نکتهٔ {index}.",
            "source": "صفحهٔ ۱",
            "difficulty": "medium",
        }
        for index in range(count)
    ]


async def make_pack(
    db: Database,
    user_id: int,
    *,
    title: str,
    count: int = 20,
    source_type: str = "audio",
    status: str = "done",
) -> tuple[int, int, int]:
    """One accepted upload, its pack, its source row and one generation run."""
    submission = await db.create_submission(user_id, source_type, "lecture.wav")
    pack_id = await db.study_packs.study_pack_ensure(
        user_id,
        submission_id=submission,
        title=title,
        source_type=source_type,
        source_filename="lecture.wav",
    )
    item_id = await db.library_add(
        user_id,
        "booklet",
        0,
        0,
        submission,
        title=title,
        delivered=False,
        artifact_kind="submission_source",
    )
    await db.study_packs.study_pack_set_source_item(user_id, pack_id, item_id)
    session_id = await db.quiz_create_session(
        user_id, item_id, questions(count), status=status
    )
    return pack_id, item_id, session_id


class ReviewRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = Database(root / "review.db")
        await self.db.connect()
        await self.db.ensure_user(ALICE, "Alice")
        await self.db.ensure_user(BOB, "Bob")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------- grouping
    async def test_questions_are_grouped_by_pack_and_titled_with_its_own_name(self) -> None:
        anatomy, _, _ = await make_pack(
            self.db, ALICE, title="آناتومی انسان جلسهٔ یک", count=20
        )
        health, _, _ = await make_pack(
            self.db, ALICE, title="شاخص‌های سلامت جلسهٔ دو", count=5
        )

        groups, total = await self.db.review.review_groups(ALICE)

        self.assertEqual(total, 2)
        self.assertEqual(
            {group["group_key"] for group in groups},
            {f"pack:{anatomy}", f"pack:{health}"},
        )
        by_key = {group["group_key"]: group for group in groups}
        self.assertEqual(by_key[f"pack:{anatomy}"]["title"], "آناتومی انسان جلسهٔ یک")
        self.assertEqual(by_key[f"pack:{anatomy}"]["question_count"], 20)
        self.assertEqual(by_key[f"pack:{health}"]["title"], "شاخص‌های سلامت جلسهٔ دو")
        self.assertEqual(by_key[f"pack:{health}"]["question_count"], 5)
        for group in groups:
            # A session id must never be the primary identity of a source.
            self.assertNotIn("session_id", group)
            self.assertEqual(group["processing_status"], "processing")

        pack_questions, pack_total = await self.db.review.review_questions(
            ALICE, pack_id=anatomy
        )
        self.assertEqual(pack_total, 20)
        self.assertEqual(len(pack_questions), 20)
        self.assertEqual(
            [question["idx"] for question in pack_questions], list(range(20))
        )
        for question in pack_questions:
            self.assertEqual(question["pack_id"], anatomy)
            self.assertEqual(question["pack_title"], "آناتومی انسان جلسهٔ یک")
            self.assertEqual(question["group_key"], f"pack:{anatomy}")
            self.assertFalse(question["legacy"])

    async def test_a_second_generation_run_joins_the_same_pack_group(self) -> None:
        pack_id, item_id, first = await make_pack(
            self.db, ALICE, title="فصل اول", count=20
        )
        second = await self.db.quiz_create_session(
            ALICE, item_id, questions(5, prefix="سؤال دوم"), status="done"
        )
        self.assertNotEqual(first, second)

        groups, total = await self.db.review.review_groups(ALICE)
        self.assertEqual(total, 1)
        self.assertEqual(groups[0]["group_key"], f"pack:{pack_id}")
        # Both runs are the same source: one group, all 25 questions, no copies.
        self.assertEqual(groups[0]["question_count"], 25)
        listed, listed_total = await self.db.review.review_questions(
            ALICE, pack_id=pack_id
        )
        self.assertEqual(listed_total, 25)
        self.assertEqual(len({question["question_id"] for question in listed}), 25)
        self.assertEqual(len({question["session_id"] for question in listed}), 2)

    async def test_legacy_questions_keep_their_real_source_and_get_no_pack(self) -> None:
        item_id = await self.db.library_add(
            ALICE, "booklet", 0, 0, title="جزوهٔ قدیمی", delivered=True
        )
        session_id = await self.db.quiz_create_session(
            ALICE, item_id, questions(3), status="done"
        )
        session = await self.db.quiz_get_session(session_id, ALICE)
        assert session is not None
        self.assertIsNone(session["pack_id"])
        self.assertEqual(await self.db.study_packs.study_pack_list(ALICE), [])

        groups, total = await self.db.review.review_groups(ALICE)
        self.assertEqual(total, 1)
        self.assertEqual(groups[0]["group_key"], f"legacy:{item_id}")
        self.assertTrue(groups[0]["legacy"])
        self.assertIsNone(groups[0]["pack_id"])
        self.assertEqual(groups[0]["title"], "جزوهٔ قدیمی")
        self.assertEqual(groups[0]["question_count"], 3)

        listed, listed_total = await self.db.review.review_questions(
            ALICE, legacy_item_id=item_id
        )
        self.assertEqual(listed_total, 3)
        for question in listed:
            self.assertTrue(question["legacy"])
            self.assertIsNone(question["pack_id"])
            self.assertEqual(question["group_key"], f"legacy:{item_id}")
        # Nothing was invented: no pack was created to fill the missing link.
        self.assertEqual(await self.db.study_packs.study_pack_list(ALICE), [])

    async def test_empty_review_state(self) -> None:
        self.assertEqual(await self.db.review.review_groups(BOB), ([], 0))
        self.assertEqual(await self.db.review.review_questions(BOB), ([], 0))
        self.assertIsNone(await self.db.review.review_question(BOB, 1))

    async def test_pagination_is_bounded_and_stable(self) -> None:
        pack_id, _, _ = await make_pack(self.db, ALICE, title="فصل", count=20)

        first_page, total = await self.db.review.review_questions(
            ALICE, pack_id=pack_id, limit=12, offset=0
        )
        second_page, _ = await self.db.review.review_questions(
            ALICE, pack_id=pack_id, limit=12, offset=12
        )
        self.assertEqual(total, 20)
        self.assertEqual(len(first_page), 12)
        self.assertEqual(len(second_page), 8)
        self.assertEqual(
            [question["idx"] for question in first_page], list(range(12))
        )
        self.assertEqual(
            [question["idx"] for question in second_page], list(range(12, 20))
        )
        huge, _ = await self.db.review.review_questions(
            ALICE, pack_id=pack_id, limit=9999
        )
        self.assertEqual(len(huge), 20)  # clamped, never unbounded

    # ----------------------------------------------------------- ownership
    async def test_another_user_sees_nothing_and_cannot_act_on_it(self) -> None:
        pack_id, _, _ = await make_pack(self.db, ALICE, title="فصل خصوصی", count=3)
        mine, _ = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        question_id = mine[0]["question_id"]

        self.assertEqual(await self.db.review.review_groups(BOB), ([], 0))
        self.assertEqual(await self.db.review.review_questions(BOB), ([], 0))
        self.assertIsNone(await self.db.review.review_question(BOB, question_id))
        self.assertIsNone(await self.db.review.review_answer(BOB, question_id, 0))
        self.assertIsNone(await self.db.review.review_share(BOB, question_id))
        # No answer row and no publication row were created for the intruder.
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM quiz_answers WHERE question_id = ?",
            (question_id,),
        )
        self.assertEqual(int(rows[0]["c"]), 0)
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM explore_questions WHERE created_by = ?",
            (BOB,),
        )
        self.assertEqual(int(rows[0]["c"]), 0)

    async def test_a_deleted_pack_hides_its_questions(self) -> None:
        pack_id, _, _ = await make_pack(self.db, ALICE, title="موقت", count=3)
        mine, _ = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        question_id = mine[0]["question_id"]

        self.assertTrue(await self.db.study_packs.study_pack_delete(ALICE, pack_id))

        self.assertEqual(await self.db.review.review_groups(ALICE), ([], 0))
        self.assertEqual(await self.db.review.review_questions(ALICE), ([], 0))
        self.assertIsNone(await self.db.review.review_question(ALICE, question_id))

    # -------------------------------------------------------------- answers
    async def test_answer_is_recorded_once_and_state_is_persisted(self) -> None:
        pack_id, _, _ = await make_pack(self.db, ALICE, title="فصل", count=3)
        mine, _ = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        question = mine[0]
        self.assertIsNone(question["answered"])
        # The list must not be a way to read the answer key of an open question.
        self.assertNotIn("correct_index", question)
        self.assertNotIn("explanation", question)

        first = await self.db.review.review_answer(
            ALICE, question["question_id"], question["idx"] % 4
        )
        assert first is not None
        self.assertTrue(first["is_correct"])
        self.assertFalse(first["already_answered"])
        self.assertEqual(first["correct_index"], question["idx"] % 4)
        self.assertTrue(first["explanation"])

        # A second tap is idempotent: same stored answer, no IntegrityError.
        again = await self.db.review.review_answer(ALICE, question["question_id"], 3)
        assert again is not None
        self.assertTrue(again["already_answered"])
        self.assertEqual(again["correct_index"], question["idx"] % 4)
        self.assertEqual(again["is_correct"], first["is_correct"])
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM quiz_answers WHERE question_id = ?",
            (question["question_id"],),
        )
        self.assertEqual(int(rows[0]["c"]), 1)

        refreshed, _ = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        answered = refreshed[0]["answered"]
        assert answered is not None
        self.assertEqual(answered["chosen_index"], question["idx"] % 4)
        self.assertTrue(answered["is_correct"])
        groups, _ = await self.db.review.review_groups(ALICE)
        self.assertEqual(groups[0]["answered_count"], 1)
        self.assertEqual(groups[0]["correct_count"], 1)

    async def test_review_never_creates_a_session_or_copies_questions(self) -> None:
        pack_id, _, session_id = await make_pack(
            self.db, ALICE, title="فصل", count=20
        )
        for _ in range(3):
            await self.db.review.review_groups(ALICE)
            await self.db.review.review_questions(ALICE, pack_id=pack_id)
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM quiz_sessions WHERE user_id = ?", (ALICE,)
        )
        self.assertEqual(int(rows[0]["c"]), 1)
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM quiz_questions q "
            "JOIN quiz_sessions s ON s.session_id = q.session_id WHERE s.user_id = ?",
            (ALICE,),
        )
        self.assertEqual(int(rows[0]["c"]), 20)
        session = await self.db.quiz_get_session(session_id, ALICE)
        assert session is not None
        self.assertEqual(session["question_count"], 20)

    # ---------------------------------------------------------- publication
    async def test_publication_is_idempotent_and_its_state_is_visible(self) -> None:
        pack_id, _, _ = await make_pack(self.db, ALICE, title="فصل", count=3)
        mine, _ = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        question_id = mine[0]["question_id"]
        self.assertIsNone(mine[0]["publication_status"])

        first = await self.db.review.review_share(ALICE, question_id)
        assert first is not None
        self.assertTrue(first["shared"])
        self.assertFalse(first["already_shared"])
        self.assertEqual(first["status"], "pending")

        second = await self.db.review.review_share(ALICE, question_id)
        assert second is not None
        self.assertTrue(second["already_shared"])
        self.assertEqual(second["explore_question_id"], first["explore_question_id"])
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM explore_questions WHERE created_by = ?", (ALICE,)
        )
        self.assertEqual(int(rows[0]["c"]), 1)

        listed, _ = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        self.assertEqual(listed[0]["publication_status"], "pending")
        self.assertIsNone(listed[1]["publication_status"])

        await self.db.db.execute(
            "UPDATE explore_questions SET status = 'approved' WHERE question_id = ?",
            (first["explore_question_id"],),
        )
        await self.db.db.commit()
        listed, _ = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        self.assertEqual(listed[0]["publication_status"], "approved")

    # --------------------------------------------------- migration backfill
    async def test_legacy_sessions_are_linked_to_their_real_pack(self) -> None:
        pack_id, item_id, session_id = await make_pack(
            self.db, ALICE, title="فصل", count=2
        )
        orphan_item = await self.db.library_add(
            ALICE, "booklet", 0, 0, title="بدون بسته", delivered=True
        )
        orphan_session = await self.db.quiz_create_session(
            ALICE, orphan_item, questions(2), status="done"
        )
        # Simulate rows generated before migration 039/040 filled the link.
        await self.db.db.execute(
            "UPDATE quiz_sessions SET pack_id = NULL WHERE user_id = ?", (ALICE,)
        )
        await self.db.db.commit()

        statement = next(
            part.strip()
            for part in MIGRATION_040.read_text().split(";")
            if part.strip().upper().startswith("UPDATE QUIZ_SESSIONS")
        )
        await self.db.db.execute(statement)
        await self.db.db.commit()

        linked = await self.db.quiz_get_session(session_id, ALICE)
        assert linked is not None
        self.assertEqual(linked["pack_id"], pack_id)
        # A source with no pack is left alone rather than guessed at.
        untouched = await self.db.quiz_get_session(orphan_session, ALICE)
        assert untouched is not None
        self.assertIsNone(untouched["pack_id"])

        groups, total = await self.db.review.review_groups(ALICE)
        self.assertEqual(total, 2)
        keys = {group["group_key"] for group in groups}
        self.assertIn(f"pack:{pack_id}", keys)
        self.assertIn(f"legacy:{orphan_item}", keys)


class ReviewApiTests(unittest.IsolatedAsyncioTestCase):
    """The same relationship, over the real WSGI app and signed initData."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN, db_path=root / "api.db", temp_dir=root / "tmp"
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(ALICE, "Alice")
        await self.db.ensure_user(BOB, "Bob")
        self.pack_id, _, self.session_id = await make_pack(
            self.db, ALICE, title="آناتومی انسان جلسهٔ یک", count=20
        )
        self.questions, _ = await self.db.review.review_questions(
            ALICE, pack_id=self.pack_id
        )
        await self.db.close()
        self.app = make_wsgi_app(self.settings, private_assets_dir=root / "assets")
        self.alice = {"X-Telegram-Init-Data": _sign_init_data(ALICE)}
        self.bob = {"X-Telegram-Init-Data": _sign_init_data(BOB)}

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def call(self, method: str, path: str, *, headers=None, body: bytes = b""):
        return await asyncio.to_thread(
            _call_wsgi, self.app, method, path, headers=headers, body=body
        )

    async def test_groups_and_questions_are_served_with_pack_titles(self) -> None:
        status, body = await self.call("GET", "/api/v1/review/groups", headers=self.alice)
        self.assertTrue(status.startswith("200"))
        payload = json.loads(body)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["groups"][0]["group_key"], f"pack:{self.pack_id}")
        self.assertEqual(
            payload["groups"][0]["title"], "آناتومی انسان جلسهٔ یک"
        )
        self.assertEqual(payload["groups"][0]["question_count"], 20)

        status, body = await self.call(
            "GET",
            f"/api/v1/review/questions?group_key=pack:{self.pack_id}&limit=5",
            headers=self.alice,
        )
        self.assertTrue(status.startswith("200"))
        payload = json.loads(body)
        self.assertEqual(payload["total"], 20)
        self.assertEqual(payload["limit"], 5)
        self.assertEqual(len(payload["questions"]), 5)
        self.assertEqual(
            payload["questions"][0]["pack_title"], "آناتومی انسان جلسهٔ یک"
        )

        status, body = await self.call(
            "GET",
            f"/api/v1/review/questions/{self.questions[0]['question_id']}",
            headers=self.alice,
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(
            json.loads(body)["question"]["group_key"], f"pack:{self.pack_id}"
        )

    async def test_the_api_isolates_users_and_validates_input(self) -> None:
        question_id = self.questions[0]["question_id"]

        status, body = await self.call("GET", "/api/v1/review/groups", headers=self.bob)
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["groups"], [])

        status, _ = await self.call(
            "GET", f"/api/v1/review/questions/{question_id}", headers=self.bob
        )
        self.assertTrue(status.startswith("404"))
        status, _ = await self.call(
            "POST",
            "/api/v1/review/answers",
            headers=self.bob,
            body=json.dumps({"question_id": question_id, "chosen_index": 0}).encode(),
        )
        self.assertTrue(status.startswith("404"))
        status, _ = await self.call(
            "POST",
            "/api/v1/review/share",
            headers=self.bob,
            body=json.dumps({"question_id": question_id}).encode(),
        )
        self.assertTrue(status.startswith("404"))

        # No initData at all: unauthorized, never a partial answer.
        status, _ = await self.call("GET", "/api/v1/review/groups")
        self.assertTrue(status.startswith("401"))

        status, _ = await self.call(
            "GET", "/api/v1/review/questions?group_key=bogus", headers=self.alice
        )
        self.assertTrue(status.startswith("400"))
        status, _ = await self.call(
            "GET", "/api/v1/review/questions?group_key=pack:abc", headers=self.alice
        )
        self.assertTrue(status.startswith("400"))
        status, _ = await self.call(
            "POST",
            "/api/v1/review/answers",
            headers=self.alice,
            body=json.dumps({"question_id": question_id, "chosen_index": 9}).encode(),
        )
        self.assertTrue(status.startswith("400"))
        status, _ = await self.call(
            "POST",
            "/api/v1/review/answers",
            headers=self.alice,
            body=json.dumps({"question_id": "1", "chosen_index": 0}).encode(),
        )
        self.assertTrue(status.startswith("400"))

    async def test_answering_twice_and_sharing_twice_stay_idempotent(self) -> None:
        question_id = self.questions[0]["question_id"]
        body = json.dumps({"question_id": question_id, "chosen_index": 1}).encode()

        status, first = await self.call(
            "POST", "/api/v1/review/answers", headers=self.alice, body=body
        )
        self.assertTrue(status.startswith("200"))
        first_payload = json.loads(first)
        self.assertFalse(first_payload["already_answered"])
        self.assertEqual(set(first_payload), {
            "is_correct", "correct_index", "explanation", "already_answered",
        })

        status, second = await self.call(
            "POST", "/api/v1/review/answers", headers=self.alice, body=body
        )
        self.assertTrue(status.startswith("200"))
        self.assertTrue(json.loads(second)["already_answered"])

        share_body = json.dumps({"question_id": question_id}).encode()
        status, first_share = await self.call(
            "POST", "/api/v1/review/share", headers=self.alice, body=share_body
        )
        self.assertTrue(status.startswith("201"))
        status, second_share = await self.call(
            "POST", "/api/v1/review/share", headers=self.alice, body=share_body
        )
        self.assertTrue(status.startswith("200"))
        self.assertTrue(json.loads(second_share)["already_shared"])
        self.assertEqual(
            json.loads(first_share)["explore_question_id"],
            json.loads(second_share)["explore_question_id"],
        )

        status, body = await self.call(
            "GET",
            f"/api/v1/review/questions?group_key=pack:{self.pack_id}",
            headers=self.alice,
        )
        listed = json.loads(body)["questions"][0]
        self.assertEqual(listed["publication_status"], "pending")
        # The stored answer is the first one (index 1), and the correct option of
        # question 0 is a different index, so this attempt is a miss.
        self.assertEqual(listed["answered"]["chosen_index"], 1)
        self.assertFalse(listed["answered"]["is_correct"])


class ExistingQuizCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """The bot's own quiz flow keeps its cursor, score and 20-question format."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "compat.db")
        await self.db.connect()
        await self.db.ensure_user(ALICE, "Alice")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_quiz_answering_and_review_reads_share_the_same_rows(self) -> None:
        pack_id, item_id, session_id = await make_pack(
            self.db, ALICE, title="فصل", count=2, status="active"
        )
        current = await self.db.quiz_current_question(session_id)
        assert current is not None
        self.assertEqual(current["idx"], 0)

        result = await self.db.quiz_record_answer(
            session_id, current["question_id"], int(current["correct_index"])
        )
        self.assertEqual(result["status"], "ok")
        replay = await self.db.quiz_record_answer(
            session_id, current["question_id"], 0
        )
        self.assertEqual(replay["status"], "already")

        listed, total = await self.db.review.review_questions(ALICE, pack_id=pack_id)
        self.assertEqual(total, 2)
        answered = [question for question in listed if question["answered"]]
        self.assertEqual(len(answered), 1)
        self.assertTrue(answered[0]["answered"]["is_correct"])
        # The bot's own answer is the one Review shows; there is no second copy.
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM quiz_answers WHERE session_id = ?",
            (session_id,),
        )
        self.assertEqual(int(rows[0]["c"]), 1)
        self.assertEqual(item_id > 0, True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
