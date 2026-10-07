"""Structural validation for the generated .docx files.

This checks **XML correctness only**. It is deliberately explicit about that
limit: a document can satisfy every rule below and still look wrong on screen,
because glyph shaping, font substitution, justification and pagination are the
renderer's job (Word/LibreOffice), not python-docx's. Anything this script
reports as "ok" is a statement about the file's structure, never about how it
looks.

A visual check uses LibreOffice (``soffice``) when it is installed. When it is
not, ``--render`` falls back to :mod:`scripts.render_docx_pages`, the offline
Pillow renderer, which draws every page (cover, page frame, header/footer,
heading hierarchy, tables, breaks) and reports blank pages, overflow and orphan
headings. Either way the script never implies that a check happened when it did
not.

Usage::

    python -m scripts.validate_docx
    python -m scripts.validate_docx --out build/          # also write samples
    python -m scripts.validate_docx --out build/ --render # attempt PDF render
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

if __package__ in (None, ""):  # allow "python scripts/validate_docx.py"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot.docx_export import (
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    resolve_fonts,
)
from gamas_bot.structuring import (
    GlossaryEntry,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    StructuredNotes,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: The ECMA-376 / python-docx child orders that are order-sensitive. Appending
#: out of order produces a schema-invalid file that Word may offer to "repair",
#: which is how a whole document is lost to the user.
PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl",
    "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens",
    "kinsoku", "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE",
    "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind",
    "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
    "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
    "sectPr", "pPrChange",
)
RPR_ORDER = (
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike",
    "dstrike", "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid",
    "vanish", "webHidden", "color", "spacing", "w", "kern", "position", "sz",
    "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
    "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath",
)
TBLPR_ORDER = (
    "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
    "tblStyleColBandSize", "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders",
    "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption", "tblDescription",
)
TRPR_ORDER = (
    "cnfStyle", "divId", "gridBefore", "gridAfter", "wBefore", "wAfter", "cantSplit",
    "trHeight", "tblHeader", "tblCellSpacing", "jc", "hidden",
)


def _sample_notes() -> StructuredNotes:
    """A notes object exercising every block the renderer can emit."""
    return StructuredNotes(
        title="جزوهٔ نمونه — Mixed Persian/English",
        summary="خلاصهٔ آزمایشی برای بررسی ساختار سند.",
        sections=(
            NoteSection(
                heading="مفهوم بنیادی HbA1c",
                paragraphs=(
                    "شاخص HbA1c بر پایهٔ ۶.۵ درصد تشخیص دیابت را نشان می‌دهد. "
                    "فشار خون 120/80 mmHg و دوز 500 mg/dL ثبت شد. "
                    "آدرس https://example.ir/a و ایمیل info@test.ir ذکر شد.",
                ),
                bullets=("نکتهٔ اول", "نکتهٔ دوم"),
                definitions=(
                    NoteDefinition("همودینامیک", "مربوط به جریان خون", "Hemodynamic"),
                ),
                examples=("مثال: بیمار با HbA1c برابر ۸.۵ درصد.",),
                steps=("گام اول: اندازه‌گیری.", "گام دوم: تفسیر."),
                formulas=("F = ma", "eGFR = 60 mL/min/1.73m²"),
                key_points=("نکتهٔ کلیدی",),
                callouts=(
                    NoteCallout("هشدار", "دوز را خودسرانه قطع نکنید."),
                    NoteCallout("یادآوری", "این نکته را به خاطر بسپارید."),
                ),
                table=NoteTable(["مفهوم", "توضیح"], [["الف", "ب"], ["ج", "د"]]),
            ),
        ),
        glossary=(
            GlossaryEntry("HbA1c", "هموگلوبین گلیکوزیله"),
            GlossaryEntry("eGFR", "نرخ فیلتراسیون گلومرولی"),
        ),
        note_mode="full",
    )


def _check_order(xml: str, tag: str, order: tuple[str, ...]) -> list[str]:
    """Return a list of ordering violations for one property element.

    Only *direct* children count: a ``<w:sectPr>`` nested inside a ``<w:pPr>``
    has its own child order (``w:bidi`` legitimately follows ``w:pgBorders``
    there), so scanning the raw text and treating every nested tag as a child of
    the outer element produces false positives. The document is parsed instead.
    """
    from lxml import etree

    problems: list[str] = []
    rank = {name: index for index, name in enumerate(order)}
    root = etree.fromstring(xml.encode("utf-8"))
    for element in root.iter(f"{{{W}}}{tag}"):
        seen = -1
        for child in element:
            if not isinstance(child.tag, str) or not child.tag.startswith(f"{{{W}}}"):
                continue
            name = child.tag.rsplit("}", 1)[1]
            if name not in rank:
                continue
            if rank[name] < seen:
                problems.append(f"<w:{tag}> child <w:{name}> is out of order")
            seen = max(seen, rank[name])
    return problems


def validate(payload: bytes, *, expect_tables: bool = True) -> dict:
    """Every structural check for one .docx, as a name -> result mapping.

    ``expect_tables`` is False for a plain-text document, which legitimately
    contains no table; checking it there would report a false failure.
    """
    results: dict = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        results["zip_readable"] = True
        results["has_document_part"] = "word/document.xml" in names
        results["has_content_types"] = "[Content_Types].xml" in names
        document = archive.read("word/document.xml").decode("utf-8")
        if "word/_rels/document.xml.rels" in names:
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
            # Every r:id referenced by the document must be declared.
            referenced = set(re.findall(r'r:(?:id|embed)="([^"]+)"', document))
            declared = set(re.findall(r'Id="([^"]+)"', rels))
            results["relationships_resolve"] = referenced <= declared
        xml = document
        if "word/styles.xml" in names:
            styles = archive.read("word/styles.xml").decode("utf-8")
            results["styles_part_present"] = True
            results["has_normal_style"] = 'w:styleId="Normal"' in styles
        if "word/fontTable.xml" in names:
            fonts = archive.read("word/fontTable.xml").decode("utf-8")
            results["font_table_present"] = True
            results["has_alt_name_fallback"] = "<w:altName" in fonts
        else:
            results["font_table_present"] = False
            results["has_alt_name_fallback"] = False

        results["no_unresolved_placeholders"] = "@codebuff" not in xml
        results["rtl_paragraphs_present"] = '<w:bidi w:val="1"/>' in xml
        results["ltr_runs_marked"] = '<w:rtl w:val="0"/>' in xml
        results["complex_script_faces_set"] = "w:cs=" in xml

        if expect_tables:
            results["tables_present"] = "<w:tbl>" in xml
            results["rtl_tables_present"] = "bidiVisual" in xml
            results["repeat_table_headers"] = "w:tblHeader" in xml

        header_parts = [name for name in names if name.startswith("word/header")]
        footer_parts = [name for name in names if name.startswith("word/footer")]
        results["running_header"] = bool(header_parts)
        results["footer"] = bool(footer_parts)
        # The PAGE field lives in the *footer* part, not the document body.
        footer_xml = "".join(
            archive.read(name).decode("utf-8") for name in footer_parts
        )
        results["page_number_field"] = "PAGE" in footer_xml
        results["page_number_field_instruction"] = "w:instrText" in footer_xml

        ordering: list[str] = []
        ordering += _check_order(xml, "pPr", PPR_ORDER)
        ordering += _check_order(xml, "rPr", RPR_ORDER)
        ordering += _check_order(xml, "tblPr", TBLPR_ORDER)
        ordering += _check_order(xml, "trPr", TRPR_ORDER)
        results["element_ordering_valid"] = not ordering
        results["ordering_violations"] = sorted(set(ordering))

        # The document must reopen in the same library that wrote it.
        from docx import Document

        Document(io.BytesIO(payload))
        results["reopens_with_python_docx"] = True
    return results


def _render_status(out_dir: Path, sample: Path) -> str:
    """Honest reporting about *visual* validation.

    LibreOffice is used when it exists. Otherwise the offline page renderer
    (`scripts.render_docx_pages`) draws the real layout with Pillow — cover,
    page frame, header/footer, heading hierarchy, tables, page breaks — and
    reports the defects it can detect (blank pages, overflow, orphan headings),
    so "visual validation" is never silently skipped.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        try:
            from scripts.render_docx_pages import render
        except Exception as exc:  # pragma: no cover - environment dependent
            return (
                f"NOT PERFORMED - no office renderer and the offline renderer is "
                f"unavailable ({type(exc).__name__}: {exc})."
            )
        pages_dir = out_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        facts = render(sample, pages_dir, out_dir / "contact-sheet.png")
        lines = [
            "rendered offline with scripts.render_docx_pages "
            f"({len(facts['pages'])} pages) -> {pages_dir}"
        ]
        if facts["warnings"]:
            lines.append("layout warnings:")
            lines.extend(f"  ! {warning}" for warning in facts["warnings"])
        else:
            lines.append("no layout warnings (no blank page, no overflow, no orphan heading)")
        lines.append("Inspect the page images by eye; a renderer cannot judge content.")
        return "\n".join(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_destination = out_dir / sample.with_suffix(".pdf").name
    with tempfile.TemporaryDirectory() as work:
        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", work, str(sample)],
                check=True,
                capture_output=True,
                timeout=180,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            return f"ATTEMPTED BUT FAILED - {type(exc).__name__}: {exc}"
        rendered_pdf = Path(work) / sample.with_suffix(".pdf").name
        if not rendered_pdf.is_file():
            return f"ATTEMPTED BUT FAILED - {soffice} produced no PDF."
        try:
            shutil.copy2(rendered_pdf, pdf_destination)
        except OSError as exc:
            return f"ATTEMPTED BUT FAILED - could not save rendered PDF ({type(exc).__name__})."
    return (
        f"rendered with {soffice} -> {pdf_destination}. "
        "Inspect the pages by eye; this script cannot judge layout."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="directory to write the sample documents to")
    parser.add_argument(
        "--render",
        action="store_true",
        help=(
            "render the pages for visual inspection: LibreOffice when present, "
            "otherwise the offline Pillow renderer (scripts/render_docx_pages.py)"
        ),
    )
    args = parser.parse_args(argv)

    fonts = resolve_fonts(
        {"body": "Vazirmatn", "heading": "Vazirmatn", "latin": "Aptos", "fallback": "Tahoma"}
    )
    documents = {
        "notes.docx": (
            build_notes_docx(
                _sample_notes(),
                meta=DocumentMeta(reference="VALIDATE-1"),
                fonts=fonts,
            ),
            True,
        ),
        "plain.docx": (
            build_plain_docx(
                "سند متنی",
                "خط اول\nخط دوم با HbA1c و 120/80 mmHg\n- بولت اول\n- بولت دوم",
                meta=DocumentMeta(reference="VALIDATE-2"),
                fonts=fonts,
            ),
            False,
        ),
    }

    failures = 0
    for name, (payload, expect_tables) in documents.items():
        results = validate(payload, expect_tables=expect_tables)
        print(f"\n=== {name} ({len(payload):,} bytes) ===")
        for key, value in results.items():
            if key == "ordering_violations":
                if value:
                    print(f"  FAIL  {key}: {value}")
                    failures += 1
                continue
            status = "ok  " if value else "FAIL"
            if not value:
                failures += 1
            print(f"  {status}  {key}")

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, (payload, _expect_tables) in documents.items():
            (out_dir / name).write_bytes(payload)
        print(f"\nsamples written to {out_dir}")

    print("\n--- visual validation ---")
    if args.render and out_dir:
        print(_render_status(out_dir, out_dir / "notes.docx"))
    else:
        print(
            "NOT PERFORMED - pass --render with --out to attempt a real render. "
            "The checks above are XML-level only."
        )

    print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} structural failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
