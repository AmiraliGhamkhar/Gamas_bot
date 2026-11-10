"""Gamas Studio structured editing: the full save → render → export lifecycle.

These tests run against a real database built by the actual migrations (026
plus 028), so they prove the schema contract, not a hand-rolled copy of it.
The security property is the same one the rest of the Mini App carries: every
store call is owner-scoped, and a structured save can only ever touch the
caller's own booklet.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.docx_export import DocumentMeta
from gamas_bot.mini_app import (
    AppError,
    MiniAppData,
    booklet_export_payload,
    create_app,
    save_structured_booklet_payload,
)
from gamas_bot.mini_app_store import MiniAppStore
from gamas_bot.notes import (
    GlossaryEntry,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    StructuredNotes,
)
from gamas_bot.notes_pdf import build_notes_pdf

USER = 42
OTHER = 43


def sample_notes(title: str = "جزوهٔ کلاس فیزیولوژی") -> StructuredNotes:
    return StructuredNotes(
        title=title,
        summary="خلاصهٔ جلسهٔ اول.",
        sections=(
            NoteSection(
                heading="مبانی",
                paragraphs=("متن پاراگراف نخست.", "متن پاراگراف دوم."),
                definitions=(NoteDefinition("بافت", "Tissue", "گروهی از سلول‌های مشابه"),),
                bullets=("مورد یک", "مورد دو"),
                examples=("نمونهٔ بالینی.",),
                steps=("قدم اول", "قدم دوم"),
                formulas=("Q = m·c·ΔT",),
                key_points=("نکتهٔ مهم",),
                callouts=(NoteCallout("هشدار", "این عدد را حفظ کنید."),),
                tables=(
                    NoteTable(
                        headers=["پارامتر", "مقدار"],
                        rows=[["دمای بدن", "۳۷ درجه"], ["فشار خون", "۱۲۰/۸۰"]],
                        caption="جدول مقادیر نرمال",
                    ),
                ),
            ),
        ),
        glossary=(GlossaryEntry("متابولیسم", "مجموعهٔ واکنش‌های شیمیایی بدن", "Metabolism"),),
        key_points=("نکتهٔ کلیدی سند",),
        learning_objectives=("هدف یک",),
        review_questions=("پرسش یک؟",),
    )


def turns_json() -> str:
    return json.dumps(
        [
            {
                "speaker_id": "speaker_0",
                "speaker_label": "0",
                "display_name": "گویندهٔ ۱",
                "start": 0.0,
                "end": 12.5,
                "text": "سلام به همهٔ دانشجوها",
                "overlaps": False,
            },
            {
                "speaker_id": "speaker_1",
                "speaker_label": "1",
                "display_name": "گویندهٔ ۲",
                "start": 13.0,
                "end": 20.0,
                "text": "ممنون استاد",
                "overlaps": True,
            },
        ],
        ensure_ascii=False,
    )


class StudioStructuredTests(unittest.IsolatedAsyncioTestCase):
    """The store over a fully migrated database, exercised like the WSGI app."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "studio.sqlite3"
        self.db = Database(self.path)
        await self.db.connect()
        await self.db.ensure_user(USER)
        await self.db.ensure_user(OTHER)
        self.submission = await self.db.create_submission(
            USER, "audio", "lecture.mp3"
        )
        notes = sample_notes()
        await self.db.save_transcription(
            self.submission,
            "whisper",
            "متن خام گفتار",
            notes.to_json(),
            speaker_turns_json=turns_json(),
            diarization_attempted=True,
            overlap_count=1,
        )
        # The pipeline's completion path: persist a PDF and link a library item.
        meta = DocumentMeta(
            reference=f"GMS-{self.submission}",
            created_at=datetime(2026, 11, 10, 8, 0, 0),
        )
        pipeline_pdf = build_notes_pdf(notes, meta=meta)
        await self.db.binary_artifact_upsert(
            self.submission,
            USER,
            "booklet",
            filename="جزوه - GMS.pdf",
            content_type="application/pdf",
            content=pipeline_pdf,
        )
        self.pipeline_pdf = pipeline_pdf
        self.item = await self.db.library_add(
            USER, "booklet", 0, 0, self.submission, title="جزوهٔ کلاس فیزیولوژی"
        )
        await self.db.library_set_content(self.item, "متن یادگیری")
        await self.db.library_set_structured(self.item, notes.to_json())
        self.store = MiniAppStore(self.path)

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    def facade(self) -> MiniAppData:
        return MiniAppData(
            leaderboard=self.store.leaderboard,
            rank=self.store.rank,
            booklets=self.store.booklets,
            booklet=self.store.booklet,
            save_booklet=self.store.save_booklet,
            submissions=self.store.submissions,
            save_structured=self.store.save_structured,
            export_bytes=self.store.export_bytes,
            revisions=self.store.revisions,
            restore_revision=self.store.restore_revision,
            speaker_turns=self.store.speaker_turns,
            rename_speaker=self.store.rename_speaker,
        )

    # -- schema -------------------------------------------------------------

    async def test_migration_028_columns_exist(self):
        for table, column in (
            ("speaker_names", "role"),
            ("library_items", "structured_json"),
            ("booklet_revisions", "structured_json"),
            ("submission_binary_artifacts", "revision_id"),
        ):
            rows = await self.db.db.execute_fetchall(
                f"PRAGMA table_info({table})"
            )
            self.assertIn(
                column,
                [str(row[1]) for row in rows],
                f"{table}.{column} missing (migration 028 not applied)",
            )

    # -- overview -----------------------------------------------------------

    def test_submissions_list_the_studio_surface(self):
        rows = self.store.submissions(USER, 10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], self.submission)
        self.assertEqual(row["booklet_item_id"], self.item)
        self.assertTrue(row["has_pdf"])
        self.assertFalse(row["has_docx"])
        self.assertTrue(row["has_speakers"])

    def test_submissions_are_owner_scoped(self):
        self.assertEqual(self.store.submissions(OTHER, 10), [])

    # -- structured save ----------------------------------------------------

    def test_a_structured_save_creates_a_revision_and_renders_both_formats(self):
        edited = sample_notes("جزوهٔ ویرایش‌شده")
        result = self.store.save_structured(USER, self.item, edited)
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertGreater(result["revision_id"], 0)
        self.assertEqual(sorted(result["rendered"]), ["booklet", "docx"])
        revisions = self.store.revisions(USER, self.item)
        self.assertEqual([r["source"] for r in revisions], ["studio", "original"])
        self.assertTrue(all(r["structured"] for r in revisions))
        record = self.store.booklet(USER, self.item)
        self.assertEqual(json.loads(record["structured_json"])["title"], "جزوهٔ ویرایش‌شده")

    def test_the_first_edit_preserves_the_pipeline_original(self):
        original = json.loads(self.store.booklet(USER, self.item)["structured_json"])
        self.store.save_structured(USER, self.item, sample_notes("نسخهٔ دوم"))
        revisions = self.store.revisions(USER, self.item)
        oldest = revisions[-1]
        self.assertEqual(oldest["source"], "original")
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute(
                "SELECT structured_json FROM booklet_revisions WHERE revision_id = ?",
                (oldest["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(json.loads(row[0])["title"], original["title"])

    def test_a_structured_save_updates_the_learning_text_cache(self):
        edited = sample_notes("عنوان جدید")
        self.store.save_structured(USER, self.item, edited)
        record = self.store.booklet(USER, self.item)
        self.assertIn("مبانی", record["content_text"])
        self.assertIn("هدف یک", record["content_text"])

    def test_a_structured_save_never_touches_the_transcript_row(self):
        self.store.save_structured(USER, self.item, sample_notes("عنوان"))
        row = self._read_transcription()
        self.assertEqual(row["text"], "متن خام گفتار")
        # The pipeline's stored notes are the immutable original.
        self.assertEqual(
            json.loads(row["notes_json"])["title"], "جزوهٔ کلاس فیزیولوژی"
        )

    def _read_transcription(self) -> dict:
        """Read the transcription row on its own short-lived connection.

        The suite's aiosqlite handle belongs to the test-case loop, which is
        not running while a synchronous test method executes; a fresh plain
        sqlite3 read avoids cross-loop use entirely.
        """
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT text, notes_json FROM transcriptions WHERE submission_id = ?",
                (self.submission,),
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row else {}

    def test_a_save_by_another_user_is_refused(self):
        self.assertIsNone(self.store.save_structured(OTHER, self.item, sample_notes()))
        record = self.store.booklet(USER, self.item)
        self.assertEqual(
            json.loads(record["structured_json"])["title"], "جزوهٔ کلاس فیزیولوژی"
        )

    def test_renamed_speakers_flow_into_the_regenerated_booklet(self):
        self.assertTrue(
            self.store.rename_speaker(USER, self.submission, "0", "دکتر احمدی")
        )
        self.store.save_structured(USER, self.item, sample_notes("با نام گوینده"))
        payload = json.loads(self.store.booklet(USER, self.item)["structured_json"])
        self.assertTrue(
            any(speaker.startswith("دکتر احمدی") for speaker in payload["speakers"]),
            payload["speakers"],
        )
        # The audio-derived facts never change: the turns themselves are intact.
        turns = self.store.speaker_turns(USER, self.submission)
        self.assertEqual(turns[0]["start"], 0.0)
        self.assertEqual(turns[0]["end"], 12.5)
        self.assertEqual(turns[0]["text"], "سلام به همهٔ دانشجوها")

    def test_artifacts_are_associated_with_the_saving_revision(self):
        result = self.store.save_structured(USER, self.item, sample_notes("ویرایش"))
        revision_id = result["revision_id"]
        connection = sqlite3.connect(self.path)
        try:
            rows = dict(
                connection.execute(
                    "SELECT kind, revision_id FROM submission_binary_artifacts"
                ).fetchall()
            )
        finally:
            connection.close()
        self.assertEqual(rows, {"booklet": revision_id, "docx": revision_id})

    def test_revision_history_is_bounded_and_the_latest_is_identifiable(self):
        for index in range(25):
            self.assertTrue(
                self.store.save_structured(USER, self.item, sample_notes(f"نسخهٔ {index}"))
            )
        revisions = self.store.revisions(USER, self.item, limit=50)
        self.assertLessEqual(len(revisions), 21)  # bounded retention
        newest_id = revisions[0]["id"]
        connection = sqlite3.connect(self.path)
        try:
            stale = connection.execute(
                "SELECT COUNT(*) FROM submission_binary_artifacts "
                "WHERE revision_id IS NOT NULL AND revision_id NOT IN "
                "(SELECT revision_id FROM booklet_revisions)"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(stale, 0)
        current = json.loads(self.store.booklet(USER, self.item)["structured_json"])
        connection = sqlite3.connect(self.path)
        try:
            newest_json = connection.execute(
                "SELECT structured_json FROM booklet_revisions WHERE revision_id = ?",
                (newest_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(json.loads(newest_json), current)

    # -- export ---------------------------------------------------------------

    def test_exports_serve_the_persisted_bytes_and_are_idempotent(self):
        self.store.save_structured(USER, self.item, sample_notes("خروجی"))
        first = self.store.export_bytes(USER, self.item, "pdf")
        second = self.store.export_bytes(USER, self.item, "pdf")
        self.assertIsNotNone(first)
        self.assertTrue(first[0].startswith(b"%PDF-"))
        self.assertEqual(first, second)
        docx = self.store.export_bytes(USER, self.item, "docx")
        self.assertIsNotNone(docx)
        self.assertTrue(docx[0].startswith(b"PK"))
        self.assertIn("wordprocessingml", docx[2])

    def test_an_export_by_another_user_finds_nothing(self):
        self.store.save_structured(USER, self.item, sample_notes("خروجی"))
        self.assertIsNone(self.store.export_bytes(OTHER, self.item, "pdf"))
        self.assertIsNone(self.store.export_bytes(OTHER, self.item, "docx"))

    def test_a_missing_artifact_is_rendered_on_demand_and_persisted(self):
        # A booklet with structure but no stored files (a save whose render
        # failed, or a pre-persistence item): the export renders once, stores,
        # and the next call is a pure retrieval.
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "DELETE FROM submission_binary_artifacts WHERE submission_id = ?",
                (self.submission,),
            )
            connection.commit()
        finally:
            connection.close()
        first = self.store.export_bytes(USER, self.item, "pdf")
        self.assertIsNotNone(first)
        connection = sqlite3.connect(self.path)
        try:
            stored = connection.execute(
                "SELECT content FROM submission_binary_artifacts WHERE kind = 'booklet'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(stored[0], first[0])
        self.assertEqual(self.store.export_bytes(USER, self.item, "pdf"), first)

    def test_a_legacy_booklet_without_structure_has_no_pdf_export(self):
        """A pre-structured-editing booklet: text cache only, no PDF render."""
        connection = sqlite3.connect(self.path)
        try:
            submission = connection.execute(
                "INSERT INTO submissions(user_id, kind, filename, status, created_at) "
                "VALUES (?, 'audio', 'old.mp3', 'done', '2026-01-01')",
                (USER,),
            ).lastrowid
            legacy = connection.execute(
                "INSERT INTO library_items(user_id, submission_id, kind, chat_id, "
                "message_id, title, created_at, content_text) "
                "VALUES (?, ?, 'booklet', 0, 0, 'جزوهٔ قدیمی', '2026-01-01', 'متن قدیمی')",
                (USER, submission),
            ).lastrowid
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(self.store.export_bytes(USER, legacy, "pdf"))

    # -- restore ---------------------------------------------------------------

    def test_a_structured_revision_restores_and_re_renders(self):
        self.store.save_structured(USER, self.item, sample_notes("نسخهٔ دو"))
        original_revision = self.store.revisions(USER, self.item)[-1]
        self.assertTrue(
            self.store.restore_revision(USER, self.item, original_revision["id"])
        )
        payload = json.loads(self.store.booklet(USER, self.item)["structured_json"])
        self.assertEqual(payload["title"], "جزوهٔ کلاس فیزیولوژی")
        revisions = self.store.revisions(USER, self.item)
        self.assertEqual(revisions[0]["source"], "restore")
        pdf = self.store.export_bytes(USER, self.item, "pdf")
        self.assertTrue(pdf[0].startswith(b"%PDF-"))

    def test_a_legacy_text_revision_still_restores_by_text(self):
        # The legacy text path appends the *previous* state before each
        # overwrite, so the revision created by the second save below holds
        # the first edit's text.
        self.assertTrue(
            self.store.save_booklet(USER, self.item, "متن ویرایش‌شدهٔ متنی")
        )
        self.assertTrue(self.store.save_booklet(USER, self.item, "متن سوم"))
        revisions = self.store.revisions(USER, self.item)
        text_only = next(r for r in revisions if not r["structured"])
        self.assertTrue(
            self.store.restore_revision(USER, self.item, text_only["id"])
        )
        self.assertEqual(
            self.store.booklet(USER, self.item)["content_text"], "متن ویرایش‌شدهٔ متنی"
        )

    # -- speakers ---------------------------------------------------------------

    def test_speaker_turns_carry_names_roles_and_defaults(self):
        self.store.rename_speaker(USER, self.submission, "0", "استاد", role="مدرس")
        turns = self.store.speaker_turns(USER, self.submission)
        self.assertEqual(turns[0]["custom_name"], "استاد")
        self.assertEqual(turns[0]["role"], "مدرس")
        self.assertEqual(turns[0]["default_name"], "گویندهٔ ۱")
        self.assertEqual(turns[0]["speaker_id"], "speaker_0")
        self.assertTrue(turns[1]["overlaps"])

    def test_a_rename_does_not_change_timings_or_labels(self):
        self.store.rename_speaker(USER, self.submission, "0", "نام تازه")
        turns = self.store.speaker_turns(USER, self.submission)
        self.assertEqual([t["speaker_label"] for t in turns], ["0", "1"])
        self.assertEqual([t["start"] for t in turns], [0.0, 13.0])
        self.assertEqual([t["end"] for t in turns], [12.5, 20.0])

    def test_a_rename_is_scoped_to_the_owner(self):
        self.assertFalse(
            self.store.rename_speaker(OTHER, self.submission, "0", "دستکاری")
        )
        turns = self.store.speaker_turns(USER, self.submission)
        self.assertIsNone(turns[0]["custom_name"])

    def test_a_role_can_be_cleared_and_left_untouched(self):
        self.store.rename_speaker(USER, self.submission, "0", "استاد", role="مدرس")
        self.store.rename_speaker(USER, self.submission, "0", "استاد", role="")
        turns = self.store.speaker_turns(USER, self.submission)
        self.assertEqual(turns[0]["role"], "")
        self.store.rename_speaker(USER, self.submission, "0", "استاد دیگر")
        turns = self.store.speaker_turns(USER, self.submission)
        self.assertEqual(turns[0]["role"], "")  # None keeps the stored role
        self.assertEqual(turns[0]["custom_name"], "استاد دیگر")

    def test_an_unknown_label_is_refused(self):
        self.assertFalse(
            self.store.rename_speaker(USER, self.submission, "UU", "نام")
        )
        self.assertFalse(
            self.store.rename_speaker(USER, self.submission, "", "نام")
        )




class BotSideDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The bot's own DB surface for the same features (pipeline writes)."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "bot.sqlite3")
        await self.db.connect()
        await self.db.ensure_user(USER)
        self.submission = await self.db.create_submission(USER, "audio", "l.mp3")

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_the_pipeline_backfills_the_structured_cache(self):
        notes = sample_notes("جزوهٔ خط لوله")
        item = await self.db.library_add(
            USER, "booklet", 0, 0, self.submission, title="جزوهٔ خط لوله"
        )
        await self.db.library_set_structured(item, notes.to_json())
        record = await self.db.read_db.execute_fetchall(
            "SELECT structured_json FROM library_items WHERE item_id = ?", (item,)
        )
        self.assertEqual(json.loads(record[0][0])["title"], "جزوهٔ خط لوله")

    async def test_the_bot_can_store_a_speaker_role(self):
        await self.db.set_speaker_name(self.submission, "0", "استاد")
        await self.db.set_speaker_name(self.submission, "0", "استاد", role="مدرس")
        names = await self.db.speaker_names(self.submission)
        self.assertEqual(names, {"0": "استاد"})
        row = await self.db.read_db.execute_fetchall(
            "SELECT custom_name, role FROM speaker_names WHERE speaker_label = '0'"
        )
        self.assertEqual((row[0][0], row[0][1]), ("استاد", "مدرس"))

    async def test_artifacts_can_carry_a_revision_pointer(self):
        await self.db.binary_artifact_upsert(
            self.submission,
            USER,
            "docx",
            filename="a.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=b"PK-first",
        )
        await self.db.binary_artifact_upsert(
            self.submission,
            USER,
            "docx",
            filename="a.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=b"PK-second",
            revision_id=11,
        )
        artifact = await self.db.binary_artifact(self.submission, USER, "docx")
        self.assertEqual(artifact["content"], b"PK-second")
        self.assertEqual(artifact["revision_id"], 11)
        # The pipeline path (no revision id) keeps the pointer untouched.
        await self.db.binary_artifact_upsert(
            self.submission,
            USER,
            "docx",
            filename="a.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=b"PK-third",
        )
        artifact = await self.db.binary_artifact(self.submission, USER, "docx")
        self.assertEqual(artifact["content"], b"PK-third")
        self.assertEqual(artifact["revision_id"], 11)


class StudioWsgiTests(unittest.IsolatedAsyncioTestCase):
    """The structured endpoints through the real WSGI callable."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "wsgi.sqlite3"
        self.db = Database(self.path)
        await self.db.connect()
        await self.db.ensure_user(USER)
        self.submission = await self.db.create_submission(USER, "audio", "l.mp3")
        notes = sample_notes()
        await self.db.save_transcription(
            self.submission,
            "whisper",
            "متن خام",
            notes.to_json(),
            speaker_turns_json=turns_json(),
        )
        self.item = await self.db.library_add(
            USER, "booklet", 0, 0, self.submission, title="جزوه"
        )
        await self.db.library_set_structured(self.item, notes.to_json())
        self.store = MiniAppStore(self.path)
        store = self.store
        self.data = MiniAppData(
            leaderboard=store.leaderboard,
            rank=store.rank,
            booklets=store.booklets,
            booklet=store.booklet,
            save_booklet=store.save_booklet,
            submissions=store.submissions,
            save_structured=store.save_structured,
            export_bytes=store.export_bytes,
            revisions=store.revisions,
            restore_revision=store.restore_revision,
            speaker_turns=store.speaker_turns,
            rename_speaker=store.rename_speaker,
        )
        self.app = create_app(data=self.data, bot_token=BOT_TOKEN, now=lambda: NOW)

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    def request(self, path, body, method="POST"):
        raw = json.dumps(body).encode()
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        chunks = self.app(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": method,
                "CONTENT_LENGTH": str(len(raw)),
                "wsgi.input": io.BytesIO(raw),
            },
            start_response,
        )
        return captured, b"".join(chunks)

    def test_the_structured_round_trip_through_http(self):
        captured, body = self.request("/api/booklet", credentials({"id": self.item}))
        payload = json.loads(body)
        self.assertTrue(captured["status"].startswith("200"))
        self.assertIsNotNone(payload["structured"])
        edited = payload["structured"]
        edited["title"] = "عنوان ویرایش‌شده"
        captured, body = self.request(
            "/api/booklet",
            credentials({"id": self.item, "document": edited}),
            "PUT",
        )
        result = json.loads(body)
        self.assertTrue(captured["status"].startswith("200"))
        self.assertTrue(result["ok"])
        self.assertGreater(result["revision_id"], 0)
        self.assertEqual(sorted(result["rendered"]), ["booklet", "docx"])

    def test_a_malformed_document_is_rejected_before_any_write(self):
        for bad in (
            {"document": []},
            {"document": {"sections": "not-a-list"}},
            {"document": {"title": "x"}},  # no content at all
        ):
            captured, body = self.request(
                "/api/booklet", credentials({"id": self.item, **bad}), "PUT"
            )
            self.assertTrue(captured["status"].startswith("400"), bad)
            self.assertIn("error", json.loads(body))
        self.assertEqual(self.store.revisions(USER, self.item), [])

    def test_control_characters_are_stripped_not_stored(self):
        captured, body = self.request("/api/booklet", credentials({"id": self.item}))
        edited = json.loads(body)["structured"]
        edited["sections"][0]["paragraphs"] = ["متن\x00با\x01کاراکتر"]
        captured, body = self.request(
            "/api/booklet", credentials({"id": self.item, "document": edited}), "PUT"
        )
        self.assertTrue(captured["status"].startswith("200"))
        stored = json.loads(self.store.booklet(USER, self.item)["structured_json"])
        self.assertNotIn("\x00", stored["sections"][0]["paragraphs"][0])
        self.assertIn("متنبا", stored["sections"][0]["paragraphs"][0].replace("\x00", ""))

    def test_an_oversized_document_is_rejected(self):
        captured, body = self.request("/api/booklet", credentials({"id": self.item}))
        edited = json.loads(body)["structured"]
        edited["sections"] = [
            {"heading": f"بخش {i}", "paragraphs": ["متن طولانی " * 50]}
            for i in range(400)
        ]
        captured, _body = self.request(
            "/api/booklet", credentials({"id": self.item, "document": edited}), "PUT"
        )
        # Either the 1 MB request cap or the section-count bound refuses it;
        # both fire before anything is written.
        self.assertIn(captured["status"][:3], ("400", "413"))
        self.assertEqual(self.store.revisions(USER, self.item), [])

    def test_too_many_sections_are_rejected_by_validation(self):
        captured, body = self.request("/api/booklet", credentials({"id": self.item}))
        edited = json.loads(body)["structured"]
        edited["sections"] = [
            {"heading": f"بخش {i}", "paragraphs": ["متن کوتاه"]}
            for i in range(250)
        ]
        captured, _body = self.request(
            "/api/booklet", credentials({"id": self.item, "document": edited}), "PUT"
        )
        self.assertTrue(captured["status"].startswith("400"))
        self.assertEqual(self.store.revisions(USER, self.item), [])

    def test_exports_require_a_known_format_and_ownership(self):
        captured, _body = self.request(
            "/api/booklet/export", credentials({"id": self.item, "format": "exe"})
        )
        self.assertTrue(captured["status"].startswith("400"))
        captured, _body = self.request(
            "/api/booklet/export", credentials({"id": 999, "format": "pdf"})
        )
        self.assertTrue(captured["status"].startswith("404"))
        captured, body = self.request(
            "/api/booklet/export", credentials({"id": self.item, "format": "pdf"})
        )
        self.assertTrue(captured["status"].startswith("200"))
        self.assertTrue(body.startswith(b"%PDF-"))
        self.assertEqual(captured["headers"]["Content-Type"], "application/pdf")

    def test_the_legacy_text_save_still_works(self):
        captured, body = self.request(
            "/api/booklet", credentials({"id": self.item, "text": "متن ساده"}), "PUT"
        )
        self.assertTrue(captured["status"].startswith("200"))
        self.assertEqual(self.store.booklet(USER, self.item)["content_text"], "متن ساده")

    def test_speaker_endpoints_group_and_rename_with_roles(self):
        captured, body = self.request(
            "/api/speakers", credentials({"submission_id": self.submission})
        )
        payload = json.loads(body)
        self.assertEqual(len(payload["speakers"]), 2)
        first = payload["speakers"][0]
        self.assertEqual(first["default_name"], "گویندهٔ ۱")
        self.assertEqual(first["segments"], 1)
        self.assertTrue(payload["unknown_attribution"] is False)
        captured, body = self.request(
            "/api/speakers",
            credentials(
                {
                    "submission_id": self.submission,
                    "label": "0",
                    "name": "استاد",
                    "role": "مدرس",
                }
            ),
            "PUT",
        )
        self.assertEqual(json.loads(body)["role"], "مدرس")
        captured, body = self.request(
            "/api/speakers", credentials({"submission_id": self.submission})
        )
        payload = json.loads(body)
        self.assertEqual(payload["speakers"][0]["custom_name"], "استاد")

    def test_the_export_payload_function_maps_missing_booklets(self):
        with self.assertRaises(AppError) as caught:
            booklet_export_payload(self.data, USER, 999, "pdf")
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(AppError) as caught:
            booklet_export_payload(self.data, USER, self.item, "rtf")
        self.assertEqual(caught.exception.status, 400)

    def test_the_payload_function_rejects_non_objects(self):
        for bad in (None, [], "text", 5):
            with self.assertRaises(AppError) as caught:
                save_structured_booklet_payload(
                    self.data, USER, self.item, {"document": bad}
                )
            self.assertEqual(caught.exception.status, 400)


# ---------------------------------------------------------------- helpers --

import hashlib  # noqa: E402
import hmac  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

BOT_TOKEN = "123456:TEST-token-for-signatures"
NOW = 1_800_000_000.0


def credentials(body: dict) -> dict:
    fields = {
        "auth_date": str(int(NOW)),
        "user": json.dumps({"id": USER, "first_name": "آزمون"}, ensure_ascii=False),
    }
    check_string = "\n".join(f"{name}={fields[name]}" for name in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return {**body, "initData": urlencode({**fields, "hash": signature})}


if __name__ == "__main__":
    unittest.main()
