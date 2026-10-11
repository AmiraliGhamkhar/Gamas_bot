"""Tests for the offline page renderer used as visual validation.

The repository ships no Word/LibreOffice/PDF tool, so ``scripts.render_docx_pages``
is the only way to *look* at a generated booklet before delivering it. These
tests keep that tool honest: a healthy document must render with no layout
warnings, the page geometry must match A4, page numbering must be reported
correctly across sections, and the defect detectors (overflow, blank page)
must actually fire when something is wrong — otherwise the renderer would
happily hand out a false "all good".
"""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from support import docx_text

try:  # Pillow/lxml are the renderer's only requirements
    from scripts import render_docx_pages as R

    RENDER_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the host environment
    R = None
    RENDER_AVAILABLE = False

from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_design
from gamas_bot.structuring import parse_structured_notes

def _part(fragment: str) -> str:
    """Wrap a fragment in a minimal part, as a real document part is namespaced."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + fragment + "</w:body></w:document>"
    )


META = DocumentMeta(
    reference="GMS-777777",
    source_name="lecture.mp3",
    engine="speechmatics",
    created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
)

LONG_NOTES = (
    '{"title": "جزوهٔ آزمون", "summary": "خلاصهٔ درس با چند جمله برای بررسی جعبه.", '
    '"learning_objectives": ["هدف نخست", "هدف دوم"], '
    '"review_questions": ["پرسش مرور نخست؟"], "sections": ['
    '{"heading": "موضوع یکم", "paragraphs": ["توضیح کامل موضوع یکم با جزئیات کافی که '
    'باید در چند خط شکسته شود تا ترازبندی و شکست خط بررسی شود."], '
    '"bullets": ["مورد الف", "مورد ب"], '
    '"definitions": [{"term": "اصطلاح", "term_en": "Term", "definition": "تعریف اصطلاح."}], '
    '"examples": ["مثال با ۵۰۰ mg"], "steps": ["گام یکم", "گام دوم"], '
    '"key_points": ["نکتهٔ کلیدی"], '
    '"table": {"headers": ["ستون یک", "ستون دو"], "rows": [["الف", "۱"], ["ب", "۲"]]}, '
    '"callouts": [{"kind": "هشدار", "text": "هشدار مهم"}]}, '
    '{"heading": "موضوع دوم", "paragraphs": ["توضیح کامل موضوع دوم با جزئیات کافی."], '
    '"bullets": ["مورد پ"]}, '
    '{"heading": "موضوع سوم", "paragraphs": ["توضیح کامل موضوع سوم با جزئیات کافی."]}, '
    '{"heading": "موضوع چهارم", "paragraphs": ["توضیح کامل موضوع چهارم با جزئیات کافی."]}'
    '], "key_points": ["نکتهٔ نهایی"], '
    '"glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}]}'
)


@unittest.skipUnless(RENDER_AVAILABLE, "the page renderer needs Pillow and lxml")
class RendererTests(unittest.TestCase):
    def build_docx(self) -> bytes:
        notes = parse_structured_notes(LONG_NOTES)
        return build_notes_docx(notes, meta=META, design=resolve_design({"toc_enabled": False}))

    def render(self, data: bytes, **kwargs) -> dict:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "booklet.docx"
            path.write_bytes(data)
            return R.render(path, kwargs.get("out"), kwargs.get("sheet"))

    def test_healthy_document_renders_without_warnings(self):
        facts = self.render(self.build_docx())
        self.assertEqual(facts["warnings"], [])
        self.assertEqual(facts["sections"], 2)  # cover section + body section
        self.assertGreaterEqual(len(facts["pages"]), 3)
        for page in facts["pages"]:
            self.assertGreater(page["blocks"], 0, f"blank page {page['index']}")
            self.assertLess(page["ink_ratio"], 0.5)

    def test_body_pages_report_a_restarted_word_numbering(self):
        facts = self.render(self.build_docx())
        self.assertEqual(facts["pages"][0]["index"], 1)
        self.assertEqual(facts["pages"][0]["page"], 1)  # the cover
        self.assertEqual(facts["pages"][1]["page"], 1)  # body restarts at ۱
        self.assertTrue(facts["pages"][1]["numbering_restarted"])
        self.assertFalse(facts["pages"][2]["numbering_restarted"])

    def test_page_geometry_matches_a4(self):
        facts = self.render(self.build_docx())
        width, height = facts["page_size_px"]
        # A4 at 100 dpi, within a pixel of rounding
        self.assertAlmostEqual(width, 827, delta=2)
        self.assertAlmostEqual(height, 1169, delta=2)

    def test_page_images_and_contact_sheet_are_written(self):
        with TemporaryDirectory() as folder:
            out = Path(folder) / "pages"
            sheet = Path(folder) / "sheet.png"
            out.mkdir()
            facts = self.render(self.build_docx(), out=out, sheet=sheet)
            files = sorted(out.glob("page-*.png"))
            self.assertEqual(len(files), len(facts["pages"]))
            self.assertTrue(sheet.is_file())
            self.assertGreater(sheet.stat().st_size, 1000)

    def test_overflow_and_blank_page_are_detected(self):
        """The detector must fail loudly, otherwise it is decoration."""
        data = self.build_docx()
        with TemporaryDirectory() as folder:
            path = Path(folder) / "booklet.docx"
            path.write_bytes(data)
            # a block that paints past the bottom margin
            with patch.object(R, "_draw_paragraph", return_value=100_000):
                facts = R.render(path)
        self.assertTrue(
            any("overflows the bottom margin" in warning for warning in facts["warnings"]),
            facts["warnings"],
        )

        with TemporaryDirectory() as folder:
            path = Path(folder) / "booklet.docx"
            path.write_bytes(data)
            with patch.object(R, "_draw_paragraph", return_value=0), patch.object(
                R, "_draw_table", return_value=0
            ):
                facts = R.render(path)
        self.assertTrue(
            any("blank page" in warning for warning in facts["warnings"]),
            facts["warnings"],
        )

    def test_cover_page_holds_the_brand_and_the_quote(self):
        data = self.build_docx()
        with TemporaryDirectory() as folder:
            out = Path(folder) / "pages"
            out.mkdir()
            path = Path(folder) / "booklet.docx"
            path.write_bytes(data)
            R.render(path, out)
            cover = (out / "page-01.png").read_bytes()
        self.assertGreater(len(cover), 5000)
        # ...and the document text really carries them
        text = docx_text(data)
        self.assertIn("به نام خدا", text)
        self.assertIn("دانش اگر در ثریا باشد", text)

    def test_rendering_never_mutates_the_document(self):
        data = self.build_docx()
        self.render(data)
        self.assertTrue(io.BytesIO(data).read(2) == b"PK")

    def test_a_callout_box_on_the_page_bottom_is_not_a_false_overflow(self):
        """Reproduced: callout boxes were measured without their own padding.

        The painter draws a shaded callout four pixels beyond its text, but the
        height reserved before placing the block counted only the text. A
        callout landing near the bottom margin was therefore drawn past it, and
        the renderer reported "content overflows the bottom margin" — a defect
        warning created by the verifier's own drawing rather than by the
        document, which is exactly the kind of false alarm an operator cannot
        afford when the only visual evidence available is this tool.
        """
        payload = {
            "title": "جزوهٔ آزمون",
            "summary": "خلاصهٔ درس",
            "sections": [
                {
                    "heading": f"سرفصل {index} (tier {index})",
                    "paragraphs": [
                        f"در این بخش سرفصل {index} توضیح داده می‌شود و مقدار "
                        f"TSH {index} mIU/L ذکر می‌شود.",
                        f"The English note says chapter {index}, measured at {index} mIU/L.",
                    ],
                    "bullets": [f"نکتهٔ {index}: مقدار مرجع {index} mIU/L"],
                    "key_points": [f"مقدار کلیدی {index}"],
                    "callouts": [{"kind": "هشدار", "text": f"پایش سرفصل {index} ضروری است"}],
                }
                for index in range(1, 7)
            ],
            "key_points": ["لووتیروکسین ۵۰ میکروگرم"],
            "glossary": [{"term": "TSH", "definition": "هورمون محرک تیروئید"}],
        }
        notes = parse_structured_notes(json.dumps(payload, ensure_ascii=False))
        data = build_notes_docx(notes, meta=META, design=resolve_design({"toc_enabled": True}))

        facts = self.render(data)

        self.assertEqual(facts["warnings"], [])
        # Silencing the warning by dropping content would be far worse than the
        # false positive: the pages must still carry the whole booklet.
        self.assertGreaterEqual(len(facts["pages"]), 4)
        for page in facts["pages"]:
            self.assertGreater(page["blocks"], 0, f"blank page {page['index']}")


@unittest.skipUnless(RENDER_AVAILABLE, "the validator needs Pillow and lxml")
@unittest.skipUnless(RENDER_AVAILABLE, "the page renderer needs Pillow and lxml")
class RendererCapabilityTests(unittest.TestCase):
    """A layout-only render must say so instead of implying the pages look right.

    Pillow in this repository draws with whatever face it finds; when that face
    has no Arabic coverage every Persian word becomes a box. Reporting "no layout
    warnings" without that caveat invites an operator to read a page of tofu as
    "the Persian renders correctly", which is the one judgement this tool cannot
    make.
    """

    def build_docx(self) -> bytes:
        notes = parse_structured_notes(LONG_NOTES)
        return build_notes_docx(notes, meta=META, design=resolve_design({"toc_enabled": False}))

    def render(self, data: bytes, **kwargs) -> dict:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "booklet.docx"
            path.write_bytes(data)
            return R.render(path, kwargs.get("out"), kwargs.get("sheet"))

    def test_a_face_without_persian_coverage_is_detected(self):
        # Pillow's bundled default face carries no Arabic glyphs, so the probe
        # must conclude that Persian cannot be drawn with it.
        from PIL import ImageFont

        with patch.object(R, "font", lambda *args, **kwargs: ImageFont.load_default(size=12)):
            self.assertFalse(R.face_renders_persian())

    def test_a_face_with_persian_coverage_is_not_flagged(self):
        from PIL import Image

        def distinct(_face, text):
            return Image.new("L", (8, 8), 255 if text == R._PERSIAN_PROBE else 0)

        with patch.object(R, "_glyph_bitmap", side_effect=distinct):
            self.assertTrue(R.face_renders_persian())

    def test_the_note_is_a_capability_limit_and_never_a_document_defect(self):
        with patch.object(R, "face_renders_persian", return_value=False):
            facts = self.render(self.build_docx())
        self.assertEqual(facts["warnings"], [])
        self.assertTrue(facts["ok"])
        self.assertEqual(len(facts["capability_notes"]), 1)
        self.assertIn("layout and direction only", facts["capability_notes"][0])


class ValidatorOrderingTests(unittest.TestCase):
    """``scripts.validate_docx`` must catch real order bugs, not nested tags."""

    def test_direct_children_are_checked(self):
        from scripts.validate_docx import PPR_ORDER, _check_order

        bad = _part('<w:pPr><w:jc w:val="both"/><w:bidi w:val="1"/></w:pPr>')
        self.assertTrue(_check_order(bad, "pPr", PPR_ORDER))

    def test_a_nested_sectPr_is_not_mistaken_for_a_sibling(self):
        # ``w:bidi`` inside ``w:sectPr`` follows ``w:pgBorders`` by schema; a
        # text scan would report this healthy paragraph as broken.
        healthy = _part(
            '<w:pPr><w:sectPr><w:pgBorders w:offsetFrom="page"/>'
            '<w:cols w:space="720"/><w:bidi w:val="1"/></w:sectPr></w:pPr>'
        )
        from scripts.validate_docx import PPR_ORDER, _check_order

        self.assertEqual(_check_order(healthy, "pPr", PPR_ORDER), [])

    def test_the_generated_documents_pass_the_validator(self):
        from scripts.validate_docx import validate

        notes = parse_structured_notes(LONG_NOTES)
        payload = build_notes_docx(notes, meta=META, design=resolve_design({"toc_enabled": False}))
        results = validate(payload, expect_tables=True)
        self.assertTrue(results["element_ordering_valid"], results["ordering_violations"])
        self.assertTrue(results["page_number_field"])
        self.assertTrue(results["reopens_with_python_docx"])
        self.assertTrue(results["no_unresolved_placeholders"])


class ValidateRenderStatusTests(unittest.TestCase):
    def test_real_office_pdf_is_copied_out_of_its_temporary_directory(self):
        from scripts.validate_docx import _render_status

        with TemporaryDirectory() as folder:
            output = Path(folder)
            sample = output / "notes.docx"
            sample.write_bytes(b"mock docx")

            def fake_soffice(command, **_kwargs):
                temporary_output = Path(command[command.index("--outdir") + 1])
                (temporary_output / "notes.pdf").write_bytes(b"%PDF-mock")

            with patch("scripts.validate_docx.shutil.which", return_value="/usr/bin/soffice"), patch(
                "scripts.validate_docx.subprocess.run", side_effect=fake_soffice
            ):
                message = _render_status(output, sample)

            self.assertIn("rendered with /usr/bin/soffice", message)
            self.assertEqual((output / "notes.pdf").read_bytes(), b"%PDF-mock")


    def test_the_offline_report_surfaces_the_capability_note(self):
        from scripts.validate_docx import _render_status

        with TemporaryDirectory() as folder:
            output = Path(folder)
            sample = output / "notes.docx"
            sample.write_bytes(b"mock docx")
            facts = {
                "pages": [{}, {}],
                "warnings": [],
                "capability_notes": ["the selected font face cannot draw Persian glyphs"],
            }
            with patch("scripts.validate_docx.shutil.which", return_value=None), patch(
                "scripts.render_docx_pages.render", return_value=facts
            ):
                message = _render_status(output, sample)
        self.assertIn("renderer capability:", message)
        self.assertIn("cannot draw Persian glyphs", message)
        self.assertIn("no layout warnings", message)


if __name__ == "__main__":
    unittest.main()
