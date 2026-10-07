"""Real-render validation of the static, page-numbered, clickable TOC.

Unlike :mod:`scripts.validate_docx` (XML structure only), this script builds a
set of representative booklets through the production
:func:`gamas_bot.docx_export.build_notes_docx` pipeline -- which itself renders
them with LibreOffice to measure heading pages -- and then *independently*
re-renders every final ``.docx`` and checks the delivered file:

* page 1 is the cover only (no outline heading, no TOC),
* page 2 starts the static TOC (``فهرست مطالب``),
* page 3 holds the first lecture heading,
* every visible TOC page number equals the heading's rendered page,
* every TOC hyperlink anchor is a bookmark on the heading it names,
* the document has no TOC field, no ``instrText TOC``, no F9 text and no
  ``w:updateFields``.

It requires LibreOffice (``soffice``) and ``pypdf``; it fails loudly instead of
skipping a check. With ``pypdfium2`` installed (optional, not a runtime
dependency) it also rasterizes pages 1-3 and the first table page to PNG for
human inspection.

Usage::

    python -m scripts.validate_docx_toc --out .render/toc
    python -m scripts.validate_docx_toc --out .render/toc --renderer /path/to/soffice
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree

from gamas_bot.docx_export import (
    DocumentMeta,
    _flatten_outline,
    _render_pdf,
    _title_key,
    build_notes_docx,
    resolve_design,
)
from gamas_bot.structuring import (
    GlossaryEntry,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    StructuredNotes,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
META = DocumentMeta(
    reference="GMS-VALID", source_name="lecture.mp3", engine="validation",
    created_at=datetime(2026, 10, 7, 9, 0),
)

PERSIAN_PARAGRAPH = (
    "در این بخش مفاهیم اصلی درس با جزئیات کامل توضیح داده می‌شود تا دانشجو "
    "بتواند ارتباط میان مفاهیم را درک کند و در آزمون از آن استفاده کند. "
)
MIXED_PARAGRAPH = (
    "شاخص HbA1c بالاتر از 6.5% و eGFR کمتر از 60 mL/min/1.73m² در Type 2 Diabetes "
    "اهمیت دارد؛ دوز Metformin برابر 500 mg و فشار خون 120/80 mmHg ثبت شد. "
    "قند ناشتا 126 mg/dL، بازهٔ 70–100 mg/dL، تاریخ 2026-10-07 و ۱۴۰۵/۰۷/۱۵. "
    "منبع: https://example.org/guidelines?id=42 و ایمیل info@example.ir. "
)


def _section(title: str, *, mixed: bool, rich: bool, paragraphs: int = 2) -> NoteSection:
    body = (MIXED_PARAGRAPH if mixed else PERSIAN_PARAGRAPH) * 2
    return NoteSection(
        heading=title,
        paragraphs=tuple(body for _ in range(paragraphs)),
        bullets=("نکتهٔ نخست دربارهٔ " + title, "نکتهٔ دوم با 95% CI"),
        definitions=(
            (NoteDefinition("انسولین", "هورمونی که قند خون را تنظیم می‌کند", "Insulin"),)
            if rich else ()
        ),
        formulas=(("BMI = weight (kg) / height² (m²)", "E = mc²") if rich else ()),
        steps=(("اندازه‌گیری HbA1c", "تفسیر نتیجه با eGFR") if rich else ()),
        key_points=("نکتهٔ کلیدی: کنترل قند خون",) if rich else (),
        callouts=((NoteCallout("هشدار", "دوز 1000 mg را بدون مشورت تغییر ندهید."),) if rich else ()),
        table=(
            NoteTable(
                ["شاخص", "مقدار طبیعی", "واحد"],
                [["HbA1c", "< 5.7", "%"], ["قند ناشتا", "70–100", "mg/dL"], ["eGFR", "≥ 90", "mL/min"]],
            )
            if rich else None
        ),
    )


def samples() -> dict[str, StructuredNotes]:
    medical = [
        "دیابت نوع ۲ (Type 2 Diabetes)", "علائم بالینی", "روش‌های تشخیص با HbA1c",
        "درمان دارویی: Metformin 500 mg",
    ]
    ten = medical + [
        "عوارض کلیوی و eGFR", "فشار خون 120/80 mmHg", "تغذیه و BMI", "ورزش و فعالیت بدنی",
        "پایش قند خون (mg/dL)", "جمع‌بندی",
    ]
    twenty = [f"موضوع شمارهٔ {i} از درس" for i in range(1, 23)]
    long_titles = [
        "بررسی جامع سازوکارهای مولکولی مقاومت به انسولین در بافت‌های محیطی و نقش التهاب مزمن "
        f"در پیشرفت دیابت نوع ۲ — بخش {i}"
        for i in range(1, 9)
    ]
    duplicated = ["مقدمه", "روش‌ها", "مقدمه", "نتایج", "روش‌ها"]
    return {
        "01_persian_only_4": StructuredNotes(
            title="جزوهٔ ادبیات فارسی", summary="خلاصهٔ درس ادبیات.",
            sections=tuple(_section(t, mixed=False, rich=False, paragraphs=4) for t in
                           ["سبک خراسانی", "سبک عراقی", "سبک هندی", "بازگشت ادبی"]),
        ),
        "02_medical_mixed_4": StructuredNotes(
            title="جزوهٔ غدد — Endocrinology", summary="خلاصه با HbA1c و eGFR.",
            sections=tuple(_section(t, mixed=True, rich=True) for t in medical),
            glossary=(GlossaryEntry("HbA1c", "هموگلوبین گلیکوزیله"),
                      GlossaryEntry("eGFR", "نرخ فیلتراسیون گلومرولی")),
        ),
        "03_tables_formulas_urls_10": StructuredNotes(
            title="جزوهٔ کامل دیابت", summary="خلاصهٔ ده بخش.",
            sections=tuple(_section(t, mixed=True, rich=True) for t in ten),
        ),
        "04_long_22_topics": StructuredNotes(
            title="جزوهٔ بلند", summary="بیست‌ودو موضوع.",
            sections=tuple(_section(t, mixed=i % 2 == 0, rich=i % 3 == 0) for i, t in enumerate(twenty)),
        ),
        "05_long_titles": StructuredNotes(
            title="جزوهٔ عنوان‌های بلند", summary="عنوان‌های چندسطری.",
            sections=tuple(_section(t, mixed=True, rich=False) for t in long_titles),
        ),
        "06_duplicate_titles": StructuredNotes(
            title="جزوهٔ عنوان‌های تکراری", summary="عنوان‌های تکراری.",
            sections=tuple(_section(t, mixed=False, rich=False) for t in duplicated),
        ),
    }


def _toc_rows(document_xml: bytes) -> tuple[list[dict], dict[str, str]]:
    root = etree.fromstring(document_xml)
    body = root.find(f"{W}body")
    toc_table = next(child for child in body if child.tag == f"{W}tbl")
    rows = []
    for tr in toc_table.findall(f"{W}tr"):
        cells = tr.findall(f"{W}tc")
        title_link = cells[0].find(f".//{W}hyperlink")
        page_link = cells[1].find(f".//{W}hyperlink")
        rows.append({
            "anchor": title_link.get(f"{W}anchor"),
            "page_anchor": page_link.get(f"{W}anchor") if page_link is not None else None,
            "title": "".join(t.text or "" for t in title_link.iter(f"{W}t")),
            "page_text": "".join(t.text or "" for t in cells[1].iter(f"{W}t")),
        })
    bookmarks = {}
    for paragraph in root.iter(f"{W}p"):
        start = paragraph.find(f"{W}bookmarkStart")
        if start is not None:
            bookmarks[start.get(f"{W}name")] = "".join(t.text or "" for t in paragraph.iter(f"{W}t"))
    return rows, bookmarks


def validate_file(data: bytes, renderer: str, out_dir: Path, name: str) -> dict:
    from pypdf import PdfReader

    problems: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document_xml = archive.read("word/document.xml")
        settings_xml = archive.read("word/settings.xml").decode("utf-8")
    xml_text = document_xml.decode("utf-8")
    if "F9" in xml_text:
        problems.append("F9 text present")
    if re.search(r"<w:instrText[^>]*>\s*TOC", xml_text):
        problems.append("TOC field present")
    if "updateFields" in settings_xml:
        problems.append("w:updateFields present")
    rows, bookmarks = _toc_rows(document_xml)

    with tempfile.TemporaryDirectory() as temporary:
        pdf_path = _render_pdf(data, Path(temporary), renderer=renderer, timeout_seconds=300)
        shutil.copy(pdf_path, out_dir / f"{name}.pdf")
    reader = PdfReader(str(out_dir / f"{name}.pdf"))
    outline = _flatten_outline(reader, reader.outline)
    outline_pages = [page for _depth, _title, page in outline]
    if not outline:
        problems.append("rendered PDF has no heading outline")
    elif min(outline_pages) != 3:
        problems.append(f"first heading is on page {min(outline_pages)}, expected 3")
    page2 = _title_key(reader.pages[1].extract_text() or "")
    toc_key = _title_key("فهرست مطالب")
    if toc_key not in page2 and toc_key[::-1] not in page2:
        problems.append("page 2 does not contain the TOC title")
    page1 = _title_key(reader.pages[0].extract_text() or "")
    if toc_key in page1 or toc_key[::-1] in page1:
        problems.append("TOC leaked onto the cover page")

    cursor = 0
    checked = []
    for row in rows:
        if row["anchor"] not in bookmarks:
            problems.append(f"TOC anchor {row['anchor']} has no bookmark")
            continue
        if row["page_anchor"] != row["anchor"]:
            problems.append(f"page-number link for {row['anchor']} points elsewhere")
        if _title_key(bookmarks[row["anchor"]]) != _title_key(row["title"]):
            problems.append(f"anchor {row['anchor']} is on a different heading")
        wanted = _title_key(row["title"])
        actual = None
        while cursor < len(outline):
            _depth, title, page = outline[cursor]
            cursor += 1
            if _title_key(title) == wanted:
                actual = page
                break
        visible = int(row["page_text"].translate(DIGITS) or 0)
        checked.append({"title": row["title"], "visible": visible, "rendered": actual})
        if actual is None:
            problems.append(f"heading not found in render: {row['title']}")
        elif visible != actual:
            problems.append(f"{row['title']}: TOC says {visible}, rendered on {actual}")
    if len({row["anchor"] for row in rows}) != len(rows):
        problems.append("duplicate TOC anchors")

    images = []
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(out_dir / f"{name}.pdf"))
        for index in (0, 1, 2):
            if index < len(pdf):
                target = out_dir / f"{name}-p{index + 1}.png"
                pdf[index].render(scale=1.3).to_pil().save(target)
                images.append(target.name)
    except ImportError:
        images.append("pypdfium2 not installed: no PNG inspection images")
    return {
        "name": name, "pages": len(reader.pages), "toc_rows": len(rows),
        "entries": checked, "problems": problems, "images": images,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(".render/toc"))
    parser.add_argument("--renderer", default=None)
    parser.add_argument("--only", default=None, help="substring filter for sample names")
    args = parser.parse_args(argv)
    renderer = args.renderer or shutil.which("soffice") or shutil.which("libreoffice")
    if not renderer:
        print("FAILED: LibreOffice (soffice) is required; no check was performed.")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    design = replace(resolve_design(), toc_enabled=True, pagination_renderer_bin=renderer,
                     pagination_timeout_seconds=300)
    results = []
    for name, notes in samples().items():
        if args.only and args.only not in name:
            continue
        data = build_notes_docx(notes, meta=META, design=design)
        (args.out / f"{name}.docx").write_bytes(data)
        result = validate_file(data, renderer, args.out, name)
        results.append(result)
        status = "OK" if not result["problems"] else "FAILED"
        print(f"{status:6} {name}: pages={result['pages']} toc_rows={result['toc_rows']}")
        for entry in result["entries"]:
            print(f"         {entry['visible']:>3} / rendered {entry['rendered']}  {entry['title'][:60]}")
        for problem in result["problems"]:
            print(f"         PROBLEM: {problem}")
    (args.out / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    return 0 if results and all(not r["problems"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
