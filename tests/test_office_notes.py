"""Source-fidelity tests for the Free Office document-note adapter."""

from __future__ import annotations

import unittest

from gamas_bot.doc_extract import DocSection, chunk_sections
from gamas_bot.office_notes import (
    MAX_SOURCE_LOCATORS_PER_SECTION,
    basic_source_booklet,
    generate_compact_notes,
    generate_full_booklet,
    locators_in_chunks,
    office_source_chunks,
    source_text,
)
from tests.support import make_settings


class OfficeNoteSourceFidelityTests(unittest.IsolatedAsyncioTestCase):
    async def test_locator_looking_user_text_is_not_removed_or_reclassified(self):
        text = "[صفحهٔ ۲]\nاین برچسب بخشی از متن ارسالی کاربر است.\nیک واقعیت فنی دیگر."
        chunks = office_source_chunks(text, max_chars=1024)

        self.assertEqual(locators_in_chunks(chunks), ())
        self.assertEqual(source_text(chunks), text)
        self.assertEqual(source_text(chunks, include_locators=False), text)
        booklet = basic_source_booklet(chunks, title="منبع")
        self.assertIn(
            "[صفحهٔ ۲]",
            "\n".join(paragraph for section in booklet.sections for paragraph in section.paragraphs),
        )

        compact, used_fallback = await generate_compact_notes(
            chunks,
            title="منبع",
            settings=make_settings(note_api_key=""),
            session=None,
        )
        self.assertTrue(used_fallback)
        self.assertIn("[صفحهٔ ۲]", compact.point_text)
        self.assertTrue(all(not section.source_locators for section in compact.sections))

    async def test_cached_source_locators_are_all_preserved_with_bounded_groups(self):
        sections = tuple(
            DocSection(
                locator=f"صفحهٔ {index}",
                heading="",
                text=f"متن صفحهٔ {index} با جزئیات کافی برای ردیابی منبع.",
            )
            for index in range(1, 10)
        )
        cached = chunk_sections(sections, max_chars=4096)
        chunks = office_source_chunks("", cached=cached, max_chars=4096)
        expected = {section.locator for section in sections}

        self.assertEqual(set(locators_in_chunks(chunks)), expected)
        self.assertTrue(
            all(len(chunk.locators) <= MAX_SOURCE_LOCATORS_PER_SECTION for chunk in chunks)
        )
        self.assertTrue(all(f"[{locator}]" in source_text(chunks) for locator in expected))


class OfficeNoteProgressTests(unittest.IsolatedAsyncioTestCase):
    """The document flow reports the same pipeline stages as the lecture flow."""

    def _chunks(self, text: str = "فصل اول درس دربارهٔ منطق است.\\nیک واقعیت دوم."):
        return office_source_chunks(text, max_chars=1024)

    async def test_full_booklet_reports_validation_and_editorial_stages(self):
        settings = make_settings(note_api_key="", note_strict_grounding=True)
        stages: list[str] = []
        incompletes: list[str] = []

        # A booklet above the compile threshold exercises the same visible
        # stages the lecture pipeline shows for a substantive document.
        text = "\n".join(
            f"بخش {index}: توضیح مفصل دربارهٔ مفهوم شمارهٔ {index} با یک مثال."
            for index in range(20)
        )
        notes, fallback = await generate_full_booklet(
            self._chunks(text),
            title="منبع",
            settings=settings,
            session=None,
            on_stage=lambda name: _append(stages, name),
            on_incomplete=lambda reason: _append(incompletes, reason),
        )

        self.assertTrue(fallback)
        self.assertTrue(notes.sections)
        # Same contract as the voice flow: the deterministic booklet passes
        # the evidence gate («validating») and the editor-assembled fallback
        # is the deterministic analogue of the final editorial pass
        # («compiling»).
        self.assertEqual(stages, ["validating", "compiling"])
        self.assertEqual(incompletes, [])

    async def test_full_booklet_survives_a_failing_stage_callback(self):
        settings = make_settings(note_api_key="")

        async def broken(_name: str) -> None:
            raise RuntimeError("telegram edit failed")

        notes, _fallback = await generate_full_booklet(
            self._chunks(),
            title="منبع",
            settings=settings,
            session=None,
            on_stage=broken,
        )
        self.assertTrue(notes.sections)

    async def test_compact_notes_report_chunk_progress_and_validation(self):
        settings = make_settings(note_api_key="")
        progress: list[tuple[int, int]] = []
        stages: list[str] = []
        chunks = office_source_chunks(
            "\\n".join(f"نکتهٔ {index}." for index in range(30)),
            max_chars=80,
        )
        self.assertGreater(len(chunks), 1)

        notes, fallback = await generate_compact_notes(
            chunks,
            title="مرور",
            settings=settings,
            session=None,
            on_stage=lambda name: _append(stages, name),
            on_chunk=lambda done, total: _append(progress, (done, total)),
        )

        self.assertTrue(fallback)
        self.assertEqual(progress[-1], (len(chunks), len(chunks)))
        self.assertEqual([done for done, _ in progress], list(range(1, len(chunks) + 1)))
        self.assertEqual(stages, ["validating"])
        self.assertEqual(len(notes.sections), len(chunks))

    async def test_compact_notes_without_callbacks_still_build(self):
        settings = make_settings(note_api_key="")
        notes, fallback = await generate_compact_notes(
            self._chunks(), title="مرور", settings=settings, session=None
        )
        self.assertTrue(fallback)
        self.assertTrue(notes.sections)


async def _append(target: list, value) -> None:
    target.append(value)


if __name__ == "__main__":
    unittest.main()
