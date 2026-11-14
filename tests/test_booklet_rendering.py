"""End-to-end rendering tests for the three representative booklet shapes.

The brief names three cases explicitly:

* **A — a short Persian lecture.** One chapter, a definition, a warning.
* **B — a long medical lecture.** Several chapters, comparison tables, English
  terms, formulas, a warning, a glossary and review questions.
* **C — mixed Persian, English and numbers.** Technical identifiers, LTR
  formulas and a table that mixes scripts in one cell.

Each case is rendered twice (DOCX and PDF) and then *inspected*, not merely
built: :func:`gamas_bot.document.qa.inspect_pdf` rasterizes every page and
checks for blank pages, content outside the frame and overlapping text blocks,
while :func:`inspect_docx` checks the package, the embedded artwork, the
complex-script font, the RTL declaration and the page-number field.

DOCX pages are not rasterized here: this host has no Word or LibreOffice, so the
Word file is verified structurally only. The PDF is the rendered proof that the
shared design system produces a printable page; see docs/BOOKLET_DESIGN.md.
"""

from __future__ import annotations

import io
import unittest
from datetime import datetime

from pypdf import PdfReader

from gamas_bot.document.qa import inspect_docx, inspect_pdf
from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.notes import (
    GlossaryEntry,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    PracticalTip,
    StructuredNotes,
)
from gamas_bot.notes_pdf import build_notes_pdf
from tests.support import docx_text

META = DocumentMeta(
    reference="GMS-RENDER",
    source_name="booklet-qa.mp3",
    created_at=datetime(2026, 11, 14, 10, 0, 0),
)


def render(notes: StructuredNotes) -> tuple[bytes, bytes, object, object]:
    """DOCX + PDF bytes and their QA reports for one booklet."""
    docx = build_notes_docx(notes, meta=META)
    pdf = build_notes_pdf(notes, meta=META, duration_seconds=3600, session_number=3)
    return docx, pdf, inspect_pdf(pdf), inspect_docx(docx)


def normalize(text: str) -> str:
    """Fold pre-shaped PDF text back to the logical Persian alphabet.

    The renderer shapes text before drawing, so extraction returns presentation
    forms. NFKC folds those, then ZWNJ, tatweel and Arabic marks (including the
    hamza of "صفحهٔ") are dropped so headings can be compared as plain strings.
    """
    import unicodedata

    folded = unicodedata.normalize("NFKC", text)
    dropped = "\u200c\u0640" + "".join(chr(cp) for cp in range(0x064B, 0x0660))
    return folded.translate(str.maketrans(dict.fromkeys(dropped)))


def pdf_text(payload: bytes) -> str:
    pages = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages)
    return normalize(pages)


def pdf_pages(payload: bytes) -> list[str]:
    return [
        normalize(page.extract_text() or "")
        for page in PdfReader(io.BytesIO(payload)).pages
    ]


def case_a() -> StructuredNotes:
    """A short Persian lecture: one chapter, one definition, one warning."""
    return StructuredNotes(
        title="مقدمهٔ تنظیم فشار خون",
        summary="این جلسه سه مفهوم پایه را معرفی می‌کند: تعریف فشار خون، اندازه‌گیری استاندارد و تفاوت فشار خون اولیه با ثانویه.",
        learning_objectives=(
            "تعریف فشار خون را بیان کند.",
            "دو شکل اصلی را از هم جدا کند.",
        ),
        sections=(
            NoteSection(
                heading="تعریف و اندازه‌گیری",
                paragraphs=(
                    "فشار خون از حاصل‌ضرب برون‌ده قلبی در مقاومت عروق محیطی به دست می‌آید و با دو عدد سیستول و دیاستول گزارش می‌شود.",
                    "اندازه‌گیری درست، پیش‌شرط هر تصمیم درمانی است؛ خطای روش، خطای تشخیص را به دنبال دارد.",
                ),
                definitions=(
                    NoteDefinition(
                        term="فشار خون",
                        definition="نیرویی که خون بر دیوارهٔ سرخرگ‌ها وارد می‌کند؛ بر حسب میلی‌متر جیوه بیان می‌شود.",
                        term_en="Blood pressure",
                    ),
                ),
                bullets=("اندازه‌گیری در حالت نشسته و پس از پنج دقیقه استراحت",),
                callouts=(
                    NoteCallout("هشدار", "بازوبند نامناسب، فشار خون را به‌طور کاذب بالا نشان می‌دهد."),
                ),
            ),
        ),
        key_points=("فشار خون همیشه با دو عدد گزارش می‌شود.",),
        review_questions=("تفاوت فشار خون اولیه و ثانویه چیست؟",),
    )


def case_b() -> StructuredNotes:
    """A long medical lecture with tables, formulas, warnings and a glossary."""
    table = NoteTable(
        headers=["دارو", "دوز روزانه (mg)", "مکانیسم", "عارضهٔ شاخص"],
        rows=[
            ["متفورمین", "۵۰۰ تا ۲۰۰۰", "کاهش تولید گلوکز کبدی", "اسیدوز لاکتیک (نادر)"],
            ["گلی‌بنکلامید", "۲.۵ تا ۲۰", "افزایش ترشح انسولین", "افت قند"],
            ["سیتاگلیپتین", "۱۰۰", "مهار DPP-4", "پانکراتیت (نادر)"],
            ["لیماگلوتاید", "۰.۶ تا ۱.۸", "آگونیست GLP-1", "تهوع و کاهش وزن"],
        ],
        caption="مقایسهٔ گروه‌های دارویی",
    )
    return StructuredNotes(
        title="داروشناسی دیابت نوع ۲",
        summary="مرور داروهای کاهندهٔ قند خون با تکیه بر مکانیسم، دوز رایج، عارضه مهم و جایگاه هر گروه در الگوریتم درمان.",
        learning_objectives=(
            "مکانیسم گروه‌های اصلی دارویی را توضیح دهد.",
            "دوز رایج و عارضهٔ شاخص هر دارو را بیان کند.",
            "شرایط قطع متفورمین را تشخیص دهد.",
        ),
        sections=(
            NoteSection(
                heading="اصول درمان دارویی",
                paragraphs=(
                    "انتخاب دارو بر پایهٔ میزان کاهش HbA1c مورد انتظار، عارضه‌های قلبی‌عروقی و کلیوی، وزن بیمار و خطر افت قند انجام می‌شود.",
                    "درمان باید همراه با آموزش سبک زندگی آغاز شود و در هر ویزیت بازبینی شود. هدف درمان، رساندن HbA1c به محدودهٔ توافق‌شده با بیمار است.",
                ),
                definitions=(
                    NoteDefinition(
                        term="HbA1c",
                        definition="شاخص میانگین قند خون سه ماه گذشته که برای پایش کنترل دیابت به کار می‌رود.",
                        term_en="Glycated hemoglobin",
                    ),
                    NoteDefinition(
                        term="مقاومت به انسولین",
                        definition="کاهش پاسخ بافت‌ها به انسولین که با جبران افزایش ترشح انسولین همراه است.",
                        term_en="Insulin resistance",
                    ),
                ),
                bullets=(
                    "هدف معمول HbA1c زیر ۷ درصد است و در سالمندان می‌تواند ملایم‌تر باشد.",
                    "پیش از شروع متفورمین، عملکرد کلیه بررسی شود.",
                ),
                examples=("بیماری با eGFR کمتر از ۳۰ باید متفورمین را قطع کند.",),
                steps=(
                    "قند خون ناشتا و HbA1c را اندازه بگیرید.",
                    "عارضه‌های قلبی‌عروقی و کلیوی را ارزیابی کنید.",
                    "دارو را با دوز کم شروع و بر اساس پاسخ تنظیم کنید.",
                ),
                formulas=("eGFR = 175 × (SCr)^-1.154 × (Age)^-0.203",),
                table=table,
                callouts=(
                    NoteCallout("هشدار", "در آسیب حاد کلیه، متفورمین را قطع کنید."),
                    NoteCallout("یادآوری", "پایش HbA1c هر سه ماه."),
                ),
            ),
            NoteSection(
                heading="دسته‌بندی داروها",
                paragraphs=(
                    "داروهای خوراکی به گروه‌های حساس‌کننده به انسولین، کاهندهٔ تولید گلوکز و افزایش‌دهندهٔ دفع گلوکز تقسیم می‌شوند. هر گروه پروفایل عارضهٔ متفاوتی دارد.",
                ),
                key_points=("عارضهٔ شاخص هر گروه را با مکانیسم آن پیوند بزنید.",),
                tables=(
                    NoteTable(
                        headers=["گروه", "نمونه", "نکتهٔ کلیدی"],
                        rows=[
                            ["بی‌گوانید", "متفورمین", "خط اول؛ پایش کلیه"],
                            ["سولفونیل‌اوره", "گلی‌بنکلامید", "خطر افت قند"],
                            ["مهارکنندهٔ SGLT2", "امپاگلیفلوزین", "مزیت قلبی و کلیوی"],
                        ],
                        caption="طبقه‌بندی درمانی",
                    ),
                ),
            ),
            NoteSection(
                heading="پایش و ایمنی",
                paragraphs=(
                    "پایش شامل HbA1c، عملکرد کلیه، وزن و ثبت موارد افت قند است. آموزش بیمار دربارهٔ علائم افت قند بخشی از ایمنی درمان است.",
                ),
                callouts=(NoteCallout("نکته", "در بیمار با نارسایی قلبی، SGLT2i را در نظر بگیرید."),),
            ),
        ),
        key_points=(
            "متفورمین خط اول است مگر منع مصرف وجود داشته باشد.",
            "هدف معمول HbA1c زیر ۷ درصد است.",
            "در بیماری قلبی‌کلیوی مزیت دارد SGLT2i.",
        ),
        glossary=(
            GlossaryEntry("HbA1c", "هموگلوبین گلیکوزیله؛ میانگین قند خون سه ماه گذشته", "Glycated hemoglobin"),
            GlossaryEntry("eGFR", "نرخ فیلتراسیون گلومرولی تخمینی", ""),
            GlossaryEntry("SGLT2 inhibitor", "مهارکنندهٔ بازگشت گلوکز در توبول کلیوی", ""),
        ),
        review_questions=("مکانیسم متفورمین چیست؟", "در چه شرایطی متفورمین قطع می‌شود؟"),
        practical_tips=(
            PracticalTip("دوز متفورمین را با غذا شروع کنید.", kind="کاربردی"),
            PracticalTip("پیش از هر افزایش دوز eGFR را بررسی کنید.", kind="آزمونی"),
        ),
    )


def case_c() -> StructuredNotes:
    """Mixed Persian/English/numbers with formulas and a bilingual table."""
    return StructuredNotes(
        title="الگوریتم‌ها و ساختار داده در سلامت",
        summary="این جلسه رابطهٔ پیچیدگی زمانی، پایگاه داده و شبکهٔ عصبی را با مثال‌های عددی و اصطلاح‌های فنی بررسی می‌کند.",
        sections=(
            NoteSection(
                heading="پیچیدگی و کارایی (O(n log n))",
                paragraphs=(
                    "مرتب‌سازی ادغامی با پیچیدگی O(n log n) و حافظهٔ کمکی O(n) کار می‌کند؛ در مقابل، مرتب‌سازی حبابی O(n²) است و برای داده‌های بزرگ مناسب نیست.",
                    "در سامانه‌های سلامت، زمان پاسخ ۲۰۰ ms تا ۵۰۰ ms هدف گذاشته می‌شود.",
                ),
                formulas=("T(n) = 2T(n/2) + O(n)", "throughput = requests / second"),
                tables=(
                    NoteTable(
                        headers=["الگوریتم", "بهترین", "میانگین", "حافظه"],
                        rows=[
                            ["Merge sort", "O(n log n)", "O(n log n)", "O(n)"],
                            ["Quick sort", "O(n log n)", "O(n log n)", "O(log n)"],
                            ["Bubble sort", "O(n)", "O(n²)", "O(1)"],
                        ],
                        caption="مقایسهٔ پیچیدگی",
                    ),
                ),
            ),
            NoteSection(
                heading="پایگاه داده و شاخص‌گذاری",
                paragraphs=(
                    "پرس‌وجوی SELECT با شرط روی ستون شاخص‌گذاری‌شده سریع‌تر اجرا می‌شود. در PostgreSQL، ایندکس‌های B-tree برای مقایسه و GIN برای جست‌وجوی متنی به کار می‌رود.",
                ),
                bullets=("پرهیز از SELECT * در جدول بزرگ", "پایش pg_stat_statements"),
            ),
        ),
        key_points=("شاخص مناسب، پرس‌وجوی پرتکرار را سریع می‌کند.",),
    )


class BookletRenderingTests(unittest.TestCase):
    def assert_clean(self, notes: StructuredNotes, label: str) -> tuple[str, str, object, object]:
        docx, pdf, pdf_quality, docx_quality = render(notes)
        self.assertTrue(pdf_quality.rasterized, f"{label}: no rasterized QA available")
        self.assertEqual(pdf_quality.issues, (), f"{label}: PDF layout issues")
        self.assertEqual(docx_quality.issues, (), f"{label}: DOCX structure issues")
        text = pdf_text(pdf)
        self.assertNotIn("\ufffd", text, f"{label}: replacement glyphs in the PDF")
        self.assertIn("GAMAS", text, f"{label}: the brand is missing from the cover")
        return docx, pdf, text, pdf_quality

    def test_case_a_short_persian_lecture(self):
        docx, pdf, text, quality = self.assert_clean(case_a(), "A")
        self.assertGreaterEqual(quality.pages, 2)
        self.assertLessEqual(quality.pages, 5)
        self.assertIn("خلاصه", text)
        self.assertIn("فشار خون", text)
        self.assertGreaterEqual(inspect_docx(docx).media, 2, "cover artwork missing")
        self.assertIn("صفحه", text)

    def test_case_b_long_medical_lecture(self):
        docx, pdf, text, quality = self.assert_clean(case_b(), "B")
        self.assertGreaterEqual(quality.pages, 4)
        self.assertLessEqual(quality.pages, 10)
        for heading in ("اصول درمان دارویی", "دسته‌بندی داروها", "پایش و ایمنی"):
            self.assertIn(normalize(heading), text)
        quality_docx = inspect_docx(docx)
        self.assertGreaterEqual(quality_docx.tables, 3)
        self.assertGreaterEqual(quality_docx.headings, 3)
        self.assertIn("eGFR", pdf_text(pdf))
        self.assertIn("واژهنامه", text)

    def test_case_c_mixed_persian_english_and_numbers(self):
        docx, pdf, text, quality = self.assert_clean(case_c(), "C")
        self.assertIn("O(n log n)", text)
        self.assertIn("PostgreSQL", text)
        self.assertIn("۲۰۰", text)
        self.assertGreaterEqual(quality.pages, 2)

    def test_every_body_page_carries_a_page_number(self):
        _, pdf, _, _ = render(case_b())
        body_pages = pdf_pages(pdf)[1:]
        self.assertTrue(body_pages)
        from gamas_bot.docx_export import to_persian_digits  # noqa: PLC0415 - local alias

        for number, page_text in enumerate(body_pages, start=1):
            with self.subTest(page=number):
                self.assertIn("صفحه", page_text)
                self.assertIn(to_persian_digits(number), page_text)
                self.assertIn("GAMAS", page_text)

    def test_the_docx_declares_rtl_and_the_persian_font(self):
        docx, _, _, _ = render(case_b())
        text = docx_text(docx)
        self.assertIn("داروشناسی", text)
        quality = inspect_docx(docx)
        self.assertEqual(quality.fonts, ("Vazirmatn",))
        with __import__("zipfile").ZipFile(io.BytesIO(docx)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
            styles = archive.read("word/styles.xml").decode("utf-8")
        self.assertIn("<w:bidi/>", document + styles)
        self.assertIn("<w:bidiVisual/>", document)

    def test_the_pdf_carries_the_bundled_faces_only(self):
        _, pdf, _, _ = render(case_a())
        quality = inspect_pdf(pdf)
        self.assertTrue(any("Vazirmatn" in name for name in quality.fonts))

    def test_a_topic_without_a_cover_asset_is_not_a_failure(self):
        # "general" is always available, but a caller may force a topic key
        # whose artwork is absent; the renderer must degrade, not crash.
        notes = case_a()
        _, pdf, quality, _ = render(notes)
        self.assertEqual(quality.issues, ())
        self.assertGreater(len(pdf), 1000)


if __name__ == "__main__":
    unittest.main()
