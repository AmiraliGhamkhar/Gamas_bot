"""Regressions for the large-audio hardening pass (audit findings F-1 … F-10).

Each test here pins one failure mode of a long lecture (hours of audio, a
booklet far larger than one note request, a multi-gigabyte upload) that the
default-size test suite never reaches:

* F-1 — the compile and repair passes silently skipped a booklet larger than
  one request, so a 3-hour lecture lost the editorial pass entirely;
* F-2 — the text fallback needed ≤ 40 pages, so a 3-hour lecture could not be
  delivered as text at all.

The provider is a scripted session: map answers are fixtures, and a compile or
repair answer echoes the booklet it was sent, so the acceptance gates are
exercised on honest content instead of on hand-written shapes.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot import notes as notes_module
from gamas_bot.bot import GamasBot, raw_parts
from gamas_bot.db import Database
from gamas_bot.jobs import Job
from gamas_bot.notes import (
    MAX_REPAIR_SECTIONS,
    LectureContext,
    NoteSection,
    StructuredNotes,
    _repair_once,
    build_compile_document,
    gate_notes,
    structure_transcript,
)
from tests.support import FakeJobEvent, FakeTelegramClient, make_settings

#: The booklet block of a compile or repair request (both embed it verbatim).
_BOOKLET_RE = re.compile(r"### جزوهٔ فعلی[^\n]*\n(\{.*?\})\n\n### ", re.DOTALL)


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.headers = {}
        self._payload = payload

    async def read(self):
        return json.dumps(self._payload).encode()

    async def json(self, content_type=None):
        return self._payload


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class EchoSession:
    """Scripted map answers; compile/repair answers echo the booklet sent.

    ``edit`` is applied to the echoed booklet payload before it is returned, so
    a test can simulate a model that restores a fact (or invents a section).
    """

    def __init__(self, map_answers, *, edit=None):
        self.map_answers = list(map_answers)
        self.calls: list[str] = []
        self.edit = edit

    def post(self, url, **kwargs):
        user = kwargs["json"]["messages"][1]["content"]
        self.calls.append(user)
        match = _BOOKLET_RE.search(user)
        if match is None:
            content = self.map_answers.pop(0)
        else:
            payload = json.loads(match.group(1))
            if self.edit is not None:
                payload = self.edit(payload)
            content = json.dumps(payload, ensure_ascii=False)
        return _AsyncCM(_FakeResponse(200, {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}]
        }))


def compile_calls(session: EchoSession) -> list[str]:
    return [call for call in session.calls if _BOOKLET_RE.search(call)]


def big_notes_json(title: str) -> str:
    """A valid map answer with several sections, large enough to matter."""
    payload = {
        "title": title,
        "summary": f"خلاصهٔ نسبتاً طولانی برای بخش {title} از درس.",
        "sections": [
            {
                "heading": f"بخش {number} از {title}",
                "paragraphs": [
                    f"پاراگراف {paragraph} از بخش {number} با توضیح کامل و محتوای آموزشی یکتای {title}."
                    for paragraph in range(2)
                ],
                "bullets": [f"نکتهٔ مهم بخش {number} از {title}"],
            }
            for number in range(3)
        ],
        "key_points": ["نکتهٔ کلیدی"],
    }
    return json.dumps(payload, ensure_ascii=False)


def long_lecture_document(topics: int) -> str:
    return "\n\n".join(
        f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
        for index in range(topics)
    )


class GroupedCompileTests(unittest.IsolatedAsyncioTestCase):
    """F-1a: a merged booklet over the compile budget is compiled in groups."""

    async def merged_sections(self, document: str, chunks: int) -> int:
        """The deterministic merge's section count (no compile pass)."""
        settings = make_settings(note_chunk_chars=400, note_compile=False)
        session = EchoSession([big_notes_json(f"جزوهٔ {i}") for i in range(chunks)])
        baseline = await structure_transcript(document, settings, session)
        self.assertEqual(compile_calls(session), [])
        return len(baseline.sections)

    async def test_a_booklet_above_the_budget_is_compiled_in_whole_section_groups(self):
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=True)
        document = long_lecture_document(6)
        chunks = split_transcript(document, 400)
        self.assertGreater(len(chunks), 1)
        expected = await self.merged_sections(document, len(chunks))
        base = len(build_compile_document(LectureContext(), StructuredNotes()))
        budget = base + 2200
        session = EchoSession([big_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))])

        with mock.patch.object(notes_module, "COMPILE_HARD_MAX_CHARS", budget), \
             self.assertLogs("gamas_bot.notes", level="WARNING") as logs:
            notes = await structure_transcript(document, settings, session)

        requests = compile_calls(session)
        # More than one compile request: the booklet was grouped, not skipped.
        self.assertGreater(len(requests), 1)
        for request in requests:
            self.assertLessEqual(len(request), budget)
        # The size-driven path says so instead of looking like a normal merge.
        self.assertTrue(
            any("compile pass grouped" in line for line in logs.output), logs.output
        )
        # Every merged section survives: grouping never drops or splits one.
        self.assertEqual(len(notes.sections), expected)

    async def test_a_section_that_cannot_fit_alone_is_delivered_verbatim_with_a_warning(self):
        from gamas_bot.transcript import split_transcript

        settings = make_settings(note_chunk_chars=400, note_compile=True)
        document = long_lecture_document(4)
        chunks = split_transcript(document, 400)
        expected = await self.merged_sections(document, len(chunks))
        base = len(build_compile_document(LectureContext(), StructuredNotes()))
        session = EchoSession([big_notes_json(f"جزوهٔ {i}") for i in range(len(chunks))])

        with mock.patch.object(notes_module, "COMPILE_HARD_MAX_CHARS", base + 20):
            with self.assertLogs("gamas_bot.notes", level="WARNING") as logs:
                notes = await structure_transcript(document, settings, session)

        self.assertEqual(compile_calls(session), [])
        self.assertEqual(len(notes.sections), expected)
        self.assertTrue(
            any("compile" in line and "skipped" in line for line in logs.output),
            logs.output,
        )


REPAIR_SOURCE_SECTIONS = [
    f"بخش {index}: مراقبت از بیمار شمارهٔ {index} با دوز مشخص انجام می‌شود. "
    + "توضیح بیشتر دربارهٔ این موضوع در کلاس ارائه شد. " * 8
    for index in range(1, 13)
]
REPAIR_SOURCE = "\n\n".join(
    REPAIR_SOURCE_SECTIONS[:6]
    + ["بخش 7: دوز شروع 500 mg همراه با وعدهٔ غذایی است."]
    + REPAIR_SOURCE_SECTIONS[6:]
)


def repair_notes() -> StructuredNotes:
    """Grounded notes: every source section summarised, the 500 mg dose lost."""
    sections = []
    for index in range(1, 13):
        sections.append(
            NoteSection(
                heading=f"بخش {index}",
                paragraphs=(
                    f"بخش {index}: مراقبت از بیمار شمارهٔ {index} با دوز مشخص انجام می‌شود. "
                    + "توضیح بیشتر دربارهٔ این موضوع در کلاس ارائه شد. " * 8,
                ),
            )
        )
    return StructuredNotes(title="جزوهٔ بخش‌بندی‌شده", sections=tuple(sections))


class TargetedRepairTests(unittest.IsolatedAsyncioTestCase):
    """F-1b: an oversized repair request targets bounded sections, merged back."""

    def build(self):
        notes = repair_notes()
        gated, report = gate_notes(notes, REPAIR_SOURCE, strict=True)
        self.assertTrue(report.audit.missing or report.audit.fabricated)
        return gated, report

    async def test_an_oversized_repair_carries_bounded_sections_and_merges_by_heading(self):
        gated, report = self.build()
        base = len(build_compile_document(LectureContext(), StructuredNotes()))
        budget = base + 2200
        # The repaired section earns its place through its restored fact.
        restored = "دوز شروع 500 mg همراه با وعدهٔ غذایی است."

        def edit(payload):
            for section in payload["sections"]:
                if section["heading"] == "بخش 7":
                    section["paragraphs"].append(restored)
            return payload

        session = EchoSession([], edit=edit)
        with mock.patch.object(notes_module, "COMPILE_HARD_MAX_CHARS", budget):
            repaired, repaired_report = await _repair_once(
                gated,
                report,
                REPAIR_SOURCE,
                make_settings(note_strict_grounding=True),
                session,
                mode="full",
                presentation=False,
                semaphore=__import__("asyncio").Semaphore(1),
            )

        requests = compile_calls(session)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertLessEqual(len(request), budget)
        payload = json.loads(_BOOKLET_RE.search(request).group(1))
        self.assertLessEqual(len(payload["sections"]), MAX_REPAIR_SECTIONS)
        # The section holding the lost fact is the one that was repaired.
        self.assertIn("بخش 7", [section["heading"] for section in payload["sections"]])
        # Everything outside the returned sections is untouched.
        self.assertEqual(len(repaired.sections), 12)
        self.assertEqual(repaired.sections[6].paragraphs[-1], restored)
        for index, section in enumerate(repaired.sections):
            if index == 6:
                continue
            self.assertEqual(section.heading, gated.sections[index].heading)
            # Untouched sections keep their text (whitespace is normalized by
            # the quality gate, so the comparison is word-wise).
            self.assertEqual(
                " ".join(section.paragraphs).split(),
                " ".join(gated.sections[index].paragraphs).split(),
            )
        self.assertGreater(
            repaired_report.audit.number_coverage, report.audit.number_coverage
        )

    async def test_a_section_the_model_invented_is_never_merged_in(self):
        gated, report = self.build()

        def edit(payload):
            payload["sections"].append(
                {"heading": "بخش تازهٔ ساختگی", "paragraphs": ["متن تازهٔ ناموجود."]}
            )
            return payload

        session = EchoSession([], edit=edit)
        settings = make_settings(note_strict_grounding=True)
        base = len(build_compile_document(LectureContext(), StructuredNotes()))
        with mock.patch.object(notes_module, "COMPILE_HARD_MAX_CHARS", base + 2200):
            repaired, _ = await _repair_once(
                gated,
                report,
                REPAIR_SOURCE,
                settings,
                session,
                mode="full",
                presentation=False,
                semaphore=__import__("asyncio").Semaphore(1),
            )
        self.assertEqual(len(repaired.sections), 12)
        self.assertNotIn(
            "بخش تازهٔ ساختگی", [section.heading for section in repaired.sections]
        )




class RawPartsTests(unittest.TestCase):
    """F-2: the raw transcript is split into bounded parts, never truncated."""

    def test_a_long_transcript_splits_into_bounded_parts_that_lose_nothing(self):
        paragraph = "جملهٔ کامل دربارهٔ موضوع درس. " * 40 + "\n\n"
        text = (paragraph * 400).strip()  # ~ 430k characters
        parts = raw_parts(text, max_chars=100_000, max_parts=8)
        self.assertGreater(len(parts), 1)
        self.assertLessEqual(len(parts), 8)
        for part in parts:
            self.assertLessEqual(len(part), 100_000)
        # Joining the parts reproduces the transcript: no word or line lost.
        self.assertEqual("".join(parts), text)

    def test_a_transcript_that_needs_more_parts_than_allowed_is_refused(self):
        text = "متن " * 300_000  # 1.2M characters: beyond any allowed part count
        self.assertEqual(raw_parts(text, max_chars=100_000, max_parts=8), [])

    def test_a_short_transcript_is_one_part_and_blank_text_is_none(self):
        self.assertEqual(raw_parts("متن کوتاه", max_chars=100_000, max_parts=8),
                         ["متن کوتاه"])
        self.assertEqual(raw_parts("   \n ", max_chars=100_000, max_parts=8), [])


class ScriptedClient(FakeTelegramClient):
    """The Telegram double, with the ability to fail chosen upload attempts."""

    def __init__(self, recorder, *, fail_indexes=()):
        super().__init__(recorder)
        self.fail_indexes = set(fail_indexes)
        self.file_attempts = 0

    async def send_file(self, *args, **kwargs):
        self.file_attempts += 1
        if self.file_attempts in self.fail_indexes:
            raise RuntimeError("Telegram refused the upload (simulated)")
        return await super().send_file(*args, **kwargs)


class RawFallbackDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """F-2: a failed ``.txt`` upload of a long transcript still delivers."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "large.db",
            temp_dir=root / "tmp",
            receipt_dir=root / "receipts",
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.user_id = 404
        await self.db.ensure_user(self.user_id)
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.event = FakeJobEvent()
        self.bot.client = self.event.client

    async def asyncTearDown(self) -> None:
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def job(self) -> tuple[Job, Path]:
        submission = await self.db.create_submission(self.user_id, "audio", "a.wav")
        raw_path = Path(self._tmp.name) / "raw.txt"
        job = Job(
            submission_id=submission,
            user_id=self.user_id,
            kind="audio",
            filename="a.wav",
            message=self.event.message,
            is_admin=False,
        )
        return job, raw_path

    def long_text(self, characters: int) -> str:
        block = "این یک جملهٔ کامل از متن پیاده‌سازی‌شده است. " * 30 + "\n\n"
        return (block * (characters // len(block) + 1))[:characters]

    async def test_a_failed_single_upload_of_a_long_transcript_delivers_all_parts(self):
        job, raw_path = await self.job()
        raw_text = self.long_text(250_000)
        raw_path.write_text(raw_text, encoding="utf-8")
        self.bot.client = self.event.client = ScriptedClient(
            self.event, fail_indexes={1}
        )
        await GamasBot._deliver_raw(self.bot, job, raw_text, raw_path, "GMS-7")
        delivered = self.event.client.deliveries
        self.assertGreater(len(delivered), 1)
        payloads = [payload.decode("utf-8") for _path, payload in self.event.file_payloads]
        for payload in payloads:
            self.assertLessEqual(len(payload), 100_000)
        # Nothing was dropped between the parts.
        self.assertEqual("".join(payloads), raw_text)
        # The workdir holds no part files after delivery.
        self.assertEqual(sorted(path.name for path in raw_path.parent.glob("raw.*")),
                         ["raw.txt"])

    async def test_a_failed_part_fails_the_delivery_instead_of_settling_it(self):
        job, raw_path = await self.job()
        raw_text = self.long_text(250_000)
        raw_path.write_text(raw_text, encoding="utf-8")
        self.bot.client = self.event.client = ScriptedClient(
            self.event, fail_indexes={1, 3}
        )
        with self.assertRaises(RuntimeError):
            await GamasBot._deliver_raw(self.bot, job, raw_text, raw_path, "GMS-8")
        # The original .txt is untouched and no part file is left behind.
        self.assertEqual(sorted(path.name for path in raw_path.parent.glob("raw.*")),
                         ["raw.txt"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
