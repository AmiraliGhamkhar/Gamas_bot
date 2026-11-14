"""Render the three representative Gamas booklets and inspect them.

    python -m scripts.render_booklet_samples

Writes DOCX, PDF, page PNGs and a ``qa.json`` report under
``.render/booklet-samples/<case>/``. The three cases are the acceptance set:

* **A — short Persian lecture**: a compact note that must not be padded.
* **B — long medical/scientific lecture**: chapters, a wide table, formulas,
  classifications, warnings, all card types.
* **C — mixed Persian/English/numbers**: technical terms, units, LTR formulas
  and a long unbreakable token.

The script is QA tooling, not part of the bot's runtime path.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from gamas_bot.document.assets import all_assets, topic_for, validate_manifest
from gamas_bot.document.qa import inspect_docx, inspect_pdf, write_pdf_pages
from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_design, resolve_fonts
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

OUTPUT = Path(__file__).resolve().parent.parent / ".render" / "booklet-samples"
META = DocumentMeta(
    reference="GMS-QA-1042",
    source_name="lecture-sample.mp3",
    created_at=datetime(2026, 11, 10, 9, 30),
)


def case_a() -> StructuredNotes:
    return StructuredNotes(
        title="مقدمهٔ تنظیم فشار خون",
        summary=(
            "این جلسه سه مفهوم پایه را معرفی می‌کند: تعریف فشار خون، اندازه‌گیری "
            "استاندارد و تفاوت فشار خون اولیه با ثانویه."
        ),
        sections=(
            NoteSection(
                heading="تعریف و اندازه‌گیری",
                paragraphs=(
                    "فشار خون از حاصل‌ضرب برون‌ده قلبی در مقاومت عروق محیطی به دست می‌آید "
                    "و با دو عدد سیستول و دیاستول گزارش می‌شود.",
                ),
                definitions=(
                    NoteDefinition(
                        term="فشار خون",
                        definition="نیرویی که خون بر دیوارهٔ سرخرگ‌ها وارد می‌کند؛ بر حسب میلی‌متر جیوه بیان می‌شود.",
                        term_en="Blood pressure",
                    ),
                ),
                bullets=("اندازه‌گیری در حالت نشسته و پس از پنج دقیقه استراحت",),
            ),
            NoteSection(
                heading="اولیه در برابر ثانویه",
                paragraphs=(
                    "در بیشتر موارد علت مشخصی پیدا نمی‌شود؛ این شکل را فشار خون اولیه می‌نامند. "
                    "شکل ثانویه علت زمینه‌ای شناخته‌شده دارد و باید بررسی شود.",
                ),
                key_points=("ثانویه را در سن پایین یا فشار خون مقاوم در نظر بگیرید.",),
                callouts=(NoteCallout("نکته", "اندازه‌گیری مکرر، یک بار بالا بودن را تأیید نمی‌کند."),),
            ),
        ),
        learning_objectives=("تعریف فشار خون را بیان کند.", "دو شکل اصلی را از هم جدا کند."),
        key_points=("فشار خون اولیه شایع‌ترین شکل است.",),
        glossary=(GlossaryEntry("فشار خون", "نیروی خون بر دیوارهٔ سرخرگ", "Blood pressure"),),
    )


def case_b() -> StructuredNotes:
    rows = [
        ["متفورمین", "۵۰۰ تا ۲۰۰۰", "کاهش تولید گلوکز کبدی", "اسیدوز لاکتیک (نادر)"],
        ["گلی‌بن‌کلامید", "۲٫۵ تا ۲۰", "افزایش ترشح انسولین", "افت قند"],
        ["امپاگلیفلوزین", "۱۰ تا ۲۵", "دفع گلوکز از ادرار", "عفونت ادراری"],
        ["سیتاگلیپتین", "۱۰۰", "مهار DPP-4", "پانکراتیت (نادر)"],
        ["لیراگلوتاید", "۰٫۶ تا ۱٫۸", "آگونیست GLP-1", "تهوع و کاهش وزن"],
        ["انسولین بازال", "بر اساس وزن", "جبران کمبود انسولین", "افت قند شبانه"],
    ]
    return StructuredNotes(
        title="داروشناسی دیابت نوع ۲",
        summary=(
            "مرور داروهای کاهندهٔ قند خون با تکیه بر مکانیسم، دوز رایج، عارضهٔ مهم و "
            "جایگاه هر گروه در الگوریتم درمان."
        ),
        sections=(
            NoteSection(
                heading="اصول درمان دارویی",
                paragraphs=(
                    "انتخاب دارو بر پایهٔ میزان کاهش HbA1c مورد انتظار، عارضه‌های قلبی‌عروقی و "
                    "کلیوی، وزن بیمار و خطر افت قند انجام می‌شود. درمان باید همراه با آموزش "
                    "سبک زندگی آغاز شود و در هر ویزیت بازبینی گردد.",
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
                    "هدف معمول HbA1c کمتر از ۷ درصد است و در سالمندان می‌تواند ملایم‌تر باشد.",
                    "پیش از شروع متفورمین، عملکرد کلیه بررسی شود.",
                ),
                examples=(
                    "بیماری با eGFR کمتر از ۳۰ باید متفورمین را قطع کند.",
                ),
                steps=(
                    "قند خون ناشتا و HbA1c را اندازه بگیرید.",
                    "عارضه‌های قلبی‌عروقی و کلیوی را ارزیابی کنید.",
                    "دارو را با دوز کم شروع و بر اساس پاسخ تنظیم کنید.",
                ),
                formulas=(
                    "eGFR = 175 × (SCr)^-1.154 × (Age)^-0.203",
                    "HbA1c (%) ≈ (میانگین قند خون + 46.7) / 28.7",
                ),
                key_points=(
                    "متفورمین خط اول درمان در بیشتر بیماران است.",
                    "در نارسایی کلیوی شدید باید قطع شود.",
                ),
                table=NoteTable(
                    headers=["دارو", "دوز روزانه (mg)", "مکانیسم", "عارضهٔ شاخص"],
                    rows=rows,
                    caption="مقایسهٔ گروه‌های دارویی",
                ),
                callouts=(
                    NoteCallout("هشدار", "در آسیب حاد کلیه، متفورمین را قطع کنید."),
                    NoteCallout("یادآوری", "پایش HbA1c هر سه ماه."),
                ),
                source_locators=("اسلاید ۱۲",),
            ),
            NoteSection(
                heading="دسته‌بندی داروها",
                paragraphs=(
                    "داروهای خوراکی به گروه‌های حساس‌کننده به انسولین، کاهندهٔ تولید گلوکز و "
                    "افزایش‌دهندهٔ دفع گلوکز تقسیم می‌شوند. هر گروه پروفایل عارضهٔ متفاوتی دارد.",
                ),
                tables=(
                    NoteTable(
                        headers=["گروه", "نمونه", "نکتهٔ کلیدی"],
                        rows=[
                            ["بیگوانید", "متفورمین", "خط اول؛ پایش کلیه"],
                            ["سولفونیل‌اوره", "گلی‌بن‌کلامید", "خطر افت قند"],
                            ["مهارکنندهٔ SGLT2", "امپاگلیفلوزین", "مزیت قلبی و کلیوی"],
                        ],
                        caption="طبقه‌بندی درمانی",
                    ),
                ),
                key_points=("عارضهٔ شاخص هر گروه را با مکانیسم آن پیوند بزنید.",),
                callouts=(NoteCallout("نکته", "در بیمار با نارسایی قلبی، SGLT2i را در نظر بگیرید."),),
            ),
            NoteSection(
                heading="پایش و ایمنی",
                paragraphs=(
                    "پایش شامل HbA1c، عملکرد کلیه، وزن و ثبت موارد افت قند است. آموزش بیمار "
                    "دربارهٔ علائم افت قند بخشی از ایمنی درمان است.",
                ),
                bullets=("ثبت افت قند شبانه در پرونده", "بررسی عملکرد کلیه پیش از افزایش دوز"),
                callouts=(NoteCallout("هشدار", "افت قند در سالمند می‌تواند تظاهر غیرمعمول داشته باشد."),),
            ),
            NoteSection(
                heading="جمع‌بندی و کاربرد بالینی",
                paragraphs=(
                    "درمان دیابت نوع ۲ فردمحور است: هدف قند، عارضه‌های همراه و ترجیح بیمار "
                    "انتخاب دارو را هدایت می‌کند. بازبینی منظم درمان از انباشت داروهای بی‌اثر "
                    "جلوگیری می‌کند.",
                ),
                key_points=("هدف درمان را با بیمار توافق کنید.",),
            ),
        ),
        learning_objectives=(
            "مکانیسم گروه‌های اصلی دارویی را توضیح دهد.",
            "دوز رایج و عارضهٔ شاخص هر دارو را بیان کند.",
            "شرایط قطع متفورمین را تشخیص دهد.",
        ),
        key_points=(
            "متفورمین خط اول است مگر منع مصرف وجود داشته باشد.",
            "HbA1c هدف معمول زیر ۷ درصد است.",
            "SGLT2i در بیماری قلبی‌کلیوی مزیت دارد.",
        ),
        glossary=(
            GlossaryEntry("متفورمین", "کاهندهٔ تولید گلوکز کبدی", "Metformin"),
            GlossaryEntry("هموگلوبین گلیکوزیله", "شاخص کنترل سه‌ماهه", "HbA1c"),
            GlossaryEntry("نارسایی کلیه", "کاهش عملکرد تصفیه‌ای کلیه", "Renal failure"),
        ),
        practical_tips=(
            PracticalTip("دوز متفورمین را با غذا شروع کنید.", basis="شروع با دوز کم", kind="کاربردی"),
            PracticalTip("eGFR را پیش از هر افزایش دوز بررسی کنید.", basis="پایش کلیه", kind="آزمونی"),
            PracticalTip("در آسیب حاد کلیه متفورمین را قطع کنید.", basis="خطر اسیدوز", kind="هشدار"),
        ),
        review_questions=(
            "مکانیسم متفورمین چیست؟",
            "در چه شرایطی متفورمین قطع می‌شود؟",
        ),
        speakers=("مدرس",),
        timeline=("[۰۰:۰۰] مقدمه", "[۱۲:۳۰] مکانیسم داروها"),
    )


def case_c() -> StructuredNotes:
    return StructuredNotes(
        title="الگوریتم‌ها و ساختار داده در سلامت",
        summary=(
            "این جلسه رابطهٔ پیچیدگی زمانی، پایگاه داده و شبکهٔ عصبی را با مثال‌های "
            "عددی و اصطلاح‌های فنی بررسی می‌کند."
        ),
        sections=(
            NoteSection(
                heading="پیچیدگی و کارایی O(n log n)",
                paragraphs=(
                    "مرتب‌سازی ادغامی با پیچیدگی O(n log n) و حافظهٔ کمکی O(n) کار می‌کند؛ "
                    "در مقابل، مرتب‌سازی حبابی O(n²) است و برای داده‌های بزرگ مناسب نیست. "
                    "در سامانه‌های سلامت، زمان پاسخ ۲۰۰ ms تا ۵۰۰ ms هدف گذاشته می‌شود.",
                ),
                formulas=("T(n) = 2T(n/2) + O(n)", "throughput = requests / second"),
                definitions=(
                    NoteDefinition(
                        term="پیچیدگی زمانی",
                        definition="معیار رشد زمان اجرا نسبت به اندازهٔ ورودی.",
                        term_en="Time complexity",
                    ),
                ),
                table=NoteTable(
                    headers=["الگوریتم", "بهترین", "میانگین", "حافظه"],
                    rows=[
                        ["Merge sort", "O(n log n)", "O(n log n)", "O(n)"],
                        ["Quick sort", "O(n log n)", "O(n log n)", "O(log n)"],
                        ["Bubble sort", "O(n)", "O(n²)", "O(1)"],
                    ],
                    caption="مقایسهٔ پیچیدگی",
                ),
                callouts=(NoteCallout("نکته", "O(1) به معنی زمان ثابت است، نه سریع‌ترین."),),
            ),
            NoteSection(
                heading="پایگاه داده و شاخص‌گذاری",
                paragraphs=(
                    "پرس‌وجوی SELECT با شرط روی ستون شاخص‌گذاری‌شده سریع‌تر اجرا می‌شود. "
                    "در PostgreSQL ایندکس B-tree برای مقایسه و GIN برای جست‌وجوی متنی به کار می‌رود. "
                    "آدرس مرجع: https://example.org/gamas/healthcare-indexing-reference",
                ),
                bullets=("پرهیز از SELECT * در جدول بزرگ", "پایش pg_stat_statements"),
                key_points=("شاخص مناسب، پرس‌وجوی پرتکرار را سریع می‌کند.",),
            ),
            NoteSection(
                heading="شبکهٔ عصبی و دادهٔ بالینی",
                paragraphs=(
                    "شبکهٔ عصبی Artificial Neural Network برای طبقه‌بندی تصویر پزشکی به کار می‌رود، "
                    "اما دادهٔ برچسب‌دار و ارزیابی خارجی لازم دارد. دقت ۰٫۸۹ روی مجموعهٔ آزمون "
                    "به‌تنهایی برای کاربرد بالینی کافی نیست.",
                ),
                examples=("MIMIC-IV یک مجموعهٔ دادهٔ بالینی باز است (دسترسی با مجوز).",),
                callouts=(NoteCallout("هشدار", "خروجی مدل جای تصمیم بالینی را نمی‌گیرد."),),
            ),
        ),
        learning_objectives=("تفاوت O(n log n) و O(n²) را توضیح دهد.",),
        key_points=("پیچیدگی، حافظه و شاخص‌گذاری سه محور کارایی‌اند.",),
        glossary=(
            GlossaryEntry("شاخص", "ساختار کمکی برای تسریع جست‌وجو", "Index"),
            GlossaryEntry("شبکهٔ عصبی", "مدل یادگیری با لایه‌های وزن‌دار", "Artificial Neural Network"),
        ),
        practical_tips=(
            PracticalTip("پرس‌وجو را با EXPLAIN ANALYZE بسنجید.", basis="پایش کارایی", kind="کاربردی"),
        ),
        review_questions=("چرا حبابی برای دادهٔ بزرگ مناسب نیست؟",),
    )


CASES = {"A_short": case_a, "B_long_medical": case_b, "C_mixed": case_c}


def main() -> int:
    problems = validate_manifest()
    if problems:
        print("ASSET MANIFEST PROBLEMS:")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"assets: {len(all_assets())} available")
    for case in CASES.values():
        print(f"topic: {topic_for(str(case().title))}")

    failed = False
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, factory in CASES.items():
        notes = factory()
        directory = OUTPUT / name
        directory.mkdir(parents=True, exist_ok=True)

        docx_bytes = build_notes_docx(
            notes, fonts=resolve_fonts(), design=resolve_design(), meta=META
        )
        pdf_bytes = build_notes_pdf(
            notes, meta=META, duration_seconds=10_800, session_number=3
        )
        (directory / "booklet.docx").write_bytes(docx_bytes)
        (directory / "booklet.pdf").write_bytes(pdf_bytes)
        pages = write_pdf_pages(pdf_bytes, directory, dpi=110)

        pdf_report = inspect_pdf(pdf_bytes)
        docx_report = inspect_docx(docx_bytes)
        report = {
            "case": name,
            "title": notes.title,
            "pdf": {
                "pages": pdf_report.pages,
                "fonts": list(pdf_report.fonts),
                "issues": list(pdf_report.issues),
                "rasterized": pdf_report.rasterized,
                "page_reports": [
                    {
                        "page": p.number,
                        "ink_ratio": p.ink_ratio,
                        "blank": p.blank,
                        "out_of_frame": p.out_of_frame,
                        "overlaps": p.overlaps,
                    }
                    for p in pdf_report.page_reports
                ],
            },
            "docx": {
                "paragraphs": docx_report.paragraphs,
                "tables": docx_report.tables,
                "headings": docx_report.headings,
                "media": docx_report.media,
                "issues": list(docx_report.issues),
                "notes": list(docx_report.notes),
            },
            "png_pages": [str(path.relative_to(OUTPUT)) for path in pages],
        }
        (directory / "qa.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n{name}: {notes.title}")
        print(f"  PDF pages {pdf_report.pages}, rasterized={pdf_report.rasterized}")
        print(f"  DOCX paragraphs {docx_report.paragraphs}, tables {docx_report.tables}, "
              f"media {docx_report.media}")
        for issue in pdf_report.issues:
            print("  PDF ISSUE:", issue)
            failed = True
        for issue in docx_report.issues:
            print("  DOCX ISSUE:", issue)
            failed = True
        for note in docx_report.notes:
            print("  note:", note)
        print(f"  pages: {len(pages)} PNGs in {directory}")

    print("\nRESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
