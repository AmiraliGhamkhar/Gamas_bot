"""Source-fidelity tests for the Free Office document-note adapter."""

from __future__ import annotations

import unittest

from gamas_bot.doc_extract import DocSection, chunk_sections
from gamas_bot.office_notes import (
    MAX_SOURCE_LOCATORS_PER_SECTION,
    basic_source_booklet,
    generate_compact_notes,
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


if __name__ == "__main__":
    unittest.main()
