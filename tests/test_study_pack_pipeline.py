"""Phase 1: the real pipelines create and own exactly one Study Pack per source.

Every test here runs a genuine pipeline (the job runner for media, the document
ingester for uploads) over a real SQLite database, and then asserts that the
pack owns the outputs that pipeline actually produced — by reference, without a
second copy of any bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot.db import Database
from gamas_bot.jobs import Job
from gamas_bot.progress import JobProgress
from gamas_bot.stt import STTError
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import (
    make_settings,
    sample_notes_json,
    wav_bytes,
)
from tests.test_bot import USER_ID, BotPipelineTestCase, _SttAndLlmSession
from tests.test_doc_extract import make_pdf_bytes
from tests.test_document_ingestion import OWNER, DocumentIngestionTests
from tests.test_webapp_api import BOT_TOKEN, _call_wsgi, _sign_init_data


class MediaStudyPackTests(BotPipelineTestCase):
    """Upload → STT → notes → booklet, through the real job runner."""

    def settings_overrides(self) -> dict:
        return {"avalai_api_key": "note-key"}

    async def admit(self, event, *, kind: str = "audio", filename: str = "a.wav"):
        """Admit a submission the way the intake path does, pack included."""
        await self.db.add_credit(USER_ID, 3600, note="test")
        submission = await self.db.create_submission(USER_ID, kind, filename)
        pack_id = await self.bot._open_study_pack(
            USER_ID, submission, kind=kind, filename=filename
        )
        assert pack_id is not None
        return submission, pack_id

    async def run_job(
        self, event, submission: int, *, transcribe=None, session=None, duration: float = 100.0
    ) -> Job:
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="audio",
            filename="a.wav",
            message=event.message,
            is_admin=False,
            duration_hint=duration,
            meta={"progress": progress},
        )
        audio = Path(self._tmp.name) / "in.wav"
        audio.write_bytes(wav_bytes(duration=0.2))

        async def fake_transcribe(path, settings, session=None):
            if isinstance(transcribe, BaseException):
                raise transcribe
            return transcribe

        if session is not None:
            # The scripted session serves both Speechmatics and the note model,
            # so the real ``transcribe`` driver runs unchanged.
            self.bot._http_session = lambda: session  # type: ignore[method-assign]
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch("gamas_bot.bot.probe_media", new=self._fake_probe(duration))
            )
            stack.enter_context(
                mock.patch("gamas_bot.bot.extract_audio_track", new=self._fake_extract(audio))
            )
            if transcribe is not None:
                stack.enter_context(
                    mock.patch("gamas_bot.bot.transcribe", new=fake_transcribe)
                )
            await self.bot._run_job(job)
        return job

    async def test_media_job_creates_one_ready_pack_that_owns_its_outputs(self) -> None:
        event = self.media_event()
        submission, pack_id = await self.admit(event)

        # The pack exists before processing starts, so a crash cannot lose the
        # record of the upload, and its status is not yet a success.
        pack = await self.db.study_packs.study_pack_get(USER_ID, pack_id)
        assert pack is not None
        self.assertEqual(pack["status"], "processing")
        self.assertEqual(pack["source_type"], "audio")
        self.assertEqual(pack["source_filename"], "a.wav")

        session = _SttAndLlmSession(
            {"choices": [{"message": {"content": sample_notes_json("جزوهٔ آزمون")}}]}
        )
        await self.run_job(event, submission, session=session)

        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")

        packs = await self.db.study_packs.study_pack_list(USER_ID)
        self.assertEqual(len(packs), 1, "one submission must own exactly one pack")
        pack = await self.db.study_packs.study_pack_get(USER_ID, pack_id)
        assert pack is not None
        self.assertEqual(int(pack["submission_id"]), submission)
        self.assertEqual(pack["status"], "ready")
        self.assertFalse(pack["error"])
        source = await self.db.library_submission_source_item(USER_ID, submission)
        assert source is not None
        self.assertEqual(int(pack["source_item_id"]), int(source["item_id"]))

        assets = await self.db.study_packs.study_pack_assets(USER_ID, pack_id)
        by_kind = {asset["kind"]: asset for asset in assets}
        self.assertIn("transcript", by_kind)
        self.assertIn("pdf", by_kind)
        self.assertIn("docx", by_kind)

        # An asset is a reference to the stored artifact, never a copy of it.
        stored_pdf = await self.db.binary_artifact_meta(submission, USER_ID, "booklet")
        assert stored_pdf is not None
        self.assertEqual(by_kind["pdf"]["source_kind"], "artifact")
        self.assertEqual(by_kind["pdf"]["source_ref"], f"{submission}:booklet")
        self.assertEqual(by_kind["pdf"]["storage_path"], "")
        self.assertEqual(int(by_kind["pdf"]["byte_size"]), int(stored_pdf["byte_size"]))
        self.assertEqual(by_kind["pdf"]["sha256"], stored_pdf["sha256"])

        transcript = await self.db.transcription(submission)
        assert transcript is not None
        self.assertEqual(by_kind["transcript"]["source_kind"], "transcription")
        self.assertEqual(by_kind["transcript"]["source_ref"], str(submission))
        self.assertEqual(
            int(by_kind["transcript"]["byte_size"]),
            len(str(transcript["text"]).encode("utf-8")),
        )

    async def test_repeated_writes_cannot_duplicate_the_pack_or_its_assets(self) -> None:
        event = self.media_event()
        submission, pack_id = await self.admit(event)
        session = _SttAndLlmSession(
            {"choices": [{"message": {"content": sample_notes_json("جزوهٔ آزمون")}}]}
        )
        await self.run_job(event, submission, session=session)
        before = await self.db.study_packs.study_pack_assets(USER_ID, pack_id)
        self.assertTrue(before)

        # Retried admission and a re-delivered submission (a retry after a lost
        # notification, for instance) must land on the same pack and assets.
        again = await self.bot._open_study_pack(
            USER_ID, submission, kind="audio", filename="a.wav"
        )
        self.assertEqual(again, pack_id)
        job = Job(
            submission_id=submission, user_id=USER_ID, kind="audio", filename="a.wav"
        )
        await self.bot._finalize_study_pack(job, kinds={"booklet", "docx"}, status="done")
        await self.bot._finalize_study_pack(job, kinds={"booklet", "docx"}, status="done")

        self.assertEqual(len(await self.db.study_packs.study_pack_list(USER_ID)), 1)
        after = await self.db.study_packs.study_pack_assets(USER_ID, pack_id)
        self.assertEqual(
            [asset["asset_id"] for asset in after],
            [asset["asset_id"] for asset in before],
        )
        pack = await self.db.study_packs.study_pack_get(USER_ID, pack_id)
        assert pack is not None
        self.assertEqual(pack["status"], "ready")

    async def test_failed_job_can_never_leave_a_ready_pack(self) -> None:
        event = self.media_event()
        submission, pack_id = await self.admit(event)
        job = await self.run_job(
            event, submission, transcribe=STTError("سرویس تبدیل گفتار در دسترس نیست")
        )

        self.assertEqual(job.state, "failed")
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")

        pack = await self.db.study_packs.study_pack_get(USER_ID, pack_id)
        assert pack is not None
        self.assertEqual(pack["status"], "failed")
        self.assertIn("سرویس تبدیل گفتار", str(pack["error"]))
        # Nothing was produced, so the pack cannot claim to own anything.
        self.assertEqual(
            await self.db.study_packs.study_pack_assets(USER_ID, pack_id), []
        )


class DocumentStudyPackTests(DocumentIngestionTests):
    """A PDF upload is indexed around the document row, not a submission one."""

    async def test_document_upload_creates_one_pack_with_the_extracted_text(self) -> None:
        _, documents = await self.ingest(
            "notes.pdf", make_pdf_bytes(), mime="application/pdf"
        )
        self.assertEqual(len(documents), 1)
        document = await self.db.document_get(OWNER, documents[0]["id"])
        assert document is not None

        pack = await self.db.study_packs.study_pack_get_for_document(
            OWNER, int(document["id"])
        )
        assert pack is not None
        self.assertEqual(pack["source_type"], "pdf")
        self.assertEqual(pack["status"], "ready")
        self.assertEqual(int(pack["source_item_id"]), int(document["library_item_id"]))

        assets = await self.db.study_packs.study_pack_assets(OWNER, pack["pack_id"])
        self.assertEqual([asset["kind"] for asset in assets], ["transcript"])
        self.assertEqual(assets[0]["source_kind"], "document_text")
        self.assertEqual(assets[0]["source_ref"], str(document["id"]))
        self.assertEqual(
            int(assets[0]["byte_size"]), len(str(document["text"]).encode("utf-8"))
        )

        # Indexing the same document again (a re-upload of an identical file)
        # adds neither a second pack nor a second asset.
        await self.bot._index_document_study_pack(
            OWNER, document, source_type="pdf", filename="notes.pdf"
        )
        self.assertEqual(len(await self.db.study_packs.study_pack_list(OWNER)), 1)
        self.assertEqual(
            len(await self.db.study_packs.study_pack_assets(OWNER, pack["pack_id"])), 1
        )


class QuizPackLinkTests(unittest.IsolatedAsyncioTestCase):
    """Questions carry the pack they were generated from; Review can group them."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = Database(root / "quiz.db")
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        await self.db.ensure_user(202, "Bob")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    @staticmethod
    def _questions() -> list[dict]:
        return [
            {
                "question": f"سؤال {index}",
                "options": ["الف", "ب", "ج", "د"],
                "correct_index": index % 4,
                "explanation": "چرا",
            }
            for index in range(3)
        ]

    async def test_quiz_session_records_the_pack_of_its_source(self) -> None:
        submission = await self.db.create_submission(101, "audio", "a.wav")
        pack_id = await self.db.study_packs.study_pack_ensure(
            101,
            submission_id=submission,
            title="درس شبکه",
            source_type="audio",
            source_filename="a.wav",
        )
        item_id = await self.db.library_add(
            101, "booklet", 0, 0, submission, title="درس شبکه", artifact_kind="submission_source"
        )
        await self.db.study_packs.study_pack_set_source_item(101, pack_id, item_id)

        session_id = await self.db.quiz_create_session(101, item_id, self._questions())
        session = await self.db.quiz_get_session(session_id, 101)
        assert session is not None
        self.assertEqual(int(session["pack_id"]), pack_id)

        # Delivered rows of the same submission resolve to the same pack.
        delivered = await self.db.library_add(
            101, "quiz", 0, 0, submission, title="آزمون", artifact_kind="quiz_html"
        )
        linked = await self.db.study_packs.study_pack_get_for_item(101, delivered)
        assert linked is not None
        self.assertEqual(int(linked["pack_id"]), pack_id)

    async def test_historical_source_without_a_pack_still_creates_a_session(self) -> None:
        item_id = await self.db.library_add(101, "booklet", 0, 0, title="قدیمی")
        session_id = await self.db.quiz_create_session(101, item_id, self._questions())
        session = await self.db.quiz_get_session(session_id, 101)
        assert session is not None
        self.assertIsNone(session["pack_id"])

    async def test_a_pack_of_another_user_is_never_linked(self) -> None:
        submission = await self.db.create_submission(202, "audio", "b.wav")
        await self.db.study_packs.study_pack_ensure(
            202, submission_id=submission, title="کاربر دیگر", source_type="audio"
        )
        # Alice's own row, the same submission number space, different owner.
        item_id = await self.db.library_add(101, "booklet", 0, 0, title="مال من")
        session_id = await self.db.quiz_create_session(101, item_id, self._questions())
        session = await self.db.quiz_get_session(session_id, 101)
        assert session is not None
        self.assertIsNone(session["pack_id"])


class ReferenceAssetApiTests(unittest.IsolatedAsyncioTestCase):
    """Reference assets are served from their owner's store, never copied."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN, db_path=root / "refs.db", temp_dir=root / "tmp"
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        await self.db.ensure_user(202, "Bob")
        await self.db.close()
        self.app = make_wsgi_app(
            self.settings, private_assets_dir=root / "private_assets"
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_transcript_and_artifact_references_are_served_to_the_owner_only(self) -> None:
        text = "سلام دوستان. امروز درس شبکه است."
        db = Database(self.settings.db_path)
        await db.connect()
        try:
            submission = await db.create_submission(101, "audio", "a.wav")
            await db.save_transcription(submission, "speechmatics", text, "")
            await db.binary_artifact_upsert(
                submission,
                101,
                "booklet",
                filename="جزوه.pdf",
                content_type="application/pdf",
                content=b"%PDF-1.4 booklet",
            )
            pack_id = await db.study_packs.study_pack_ensure(
                101, submission_id=submission, title="درس شبکه", source_type="audio"
            )
            transcript_asset = await db.study_packs.study_asset_add(
                pack_id,
                101,
                kind="transcript",
                title="متن خام",
                storage_path="",
                mime_type="text/plain; charset=utf-8",
                byte_size=len(text.encode("utf-8")),
                source_kind="transcription",
                source_ref=str(submission),
            )
            pdf_asset = await db.study_packs.study_asset_add(
                pack_id,
                101,
                kind="pdf",
                title="جزوه",
                storage_path="",
                mime_type="application/pdf",
                byte_size=len(b"%PDF-1.4 booklet"),
                source_kind="artifact",
                source_ref=f"{submission}:booklet",
            )
            assets = await db.study_packs.study_pack_assets(101, pack_id)
        finally:
            await db.close()

        self.assertEqual({asset["kind"] for asset in assets}, {"transcript", "pdf"})

        alice = {"X-Telegram-Init-Data": _sign_init_data(101)}
        bob = {"X-Telegram-Init-Data": _sign_init_data(202)}

        # The list endpoint describes references without leaking their target.
        status, body = await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "GET",
            f"/api/v1/library/{pack_id}/assets",
            headers=alice,
        )
        self.assertTrue(status.startswith("200"))
        listed = json.loads(body)["assets"]
        self.assertEqual({item["kind"] for item in listed}, {"transcript", "pdf"})
        for item in listed:
            self.assertNotIn("source_ref", item)
            self.assertNotIn("storage_path", item)

        status, body = await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "GET",
            f"/api/v1/library/{pack_id}/download/{transcript_asset}",
            headers=alice,
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body.decode("utf-8"), text)

        status, body = await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "GET",
            f"/api/v1/library/{pack_id}/download/{pdf_asset}",
            headers=alice,
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body, b"%PDF-1.4 booklet")

        # Another user can neither read the asset nor its pack.
        status, _ = await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "GET",
            f"/api/v1/library/{pack_id}/download/{pdf_asset}",
            headers=bob,
        )
        self.assertTrue(status.startswith("404"))

    async def test_a_dangling_reference_is_a_404_not_a_crash(self) -> None:
        db = Database(self.settings.db_path)
        await db.connect()
        try:
            pack_id = await db.study_packs.study_pack_create(101, "بدون فایل")
            asset_id = await db.study_packs.study_asset_add(
                pack_id,
                101,
                kind="transcript",
                title="متن",
                storage_path="",
                source_kind="transcription",
                source_ref="999999",
            )
        finally:
            await db.close()
        alice = {"X-Telegram-Init-Data": _sign_init_data(101)}
        status, _ = await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "GET",
            f"/api/assets/{asset_id}/download",
            headers=alice,
        )
        self.assertTrue(status.startswith("404"))


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    """Historical library rows map to packs once, and only once."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = Database(root / "backfill.db")
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_backfill_dry_run_then_live_is_idempotent(self) -> None:
        submission = await self.db.create_submission(101, "audio", "a.wav")
        first = await self.db.library_add(
            101,
            "booklet",
            0,
            0,
            submission,
            title="جزوهٔ اول",
            delivered=True,
            artifact_kind="submission_source",
        )
        await self.db.library_add(
            101,
            "booklet",
            0,
            0,
            submission,
            title="جزوهٔ اول",
            delivered=True,
            artifact_kind="booklet",
        )
        await self.db.library_add(101, "quiz", 0, 0, title="فایل تعاملی", delivered=True)

        dry = await self.db.study_packs.backfill_from_library(dry_run=True)
        self.assertEqual(dry["scanned"], 2)
        self.assertEqual(dry["created"], 0)
        self.assertEqual(dry["pending"], 2)
        self.assertEqual(await self.db.study_packs.study_pack_list(101), [])

        live = await self.db.study_packs.backfill_from_library(dry_run=False)
        self.assertEqual(live["created"], 1)
        self.assertEqual(live["skipped"], 1)

        again = await self.db.study_packs.backfill_from_library(dry_run=False)
        self.assertEqual(again["created"], 0)

        packs = await self.db.study_packs.study_pack_list(101)
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0]["status"], "ready")
        self.assertEqual(int(packs[0]["source_item_id"]), first)
        self.assertEqual(int(packs[0]["submission_id"]), submission)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
