"""Comprehensive tests for the scientific academic booklet overhaul.

Covers:
1. System-prompt dispatch & runtime folding fallback (Gemma / o1 compatibility).
2. Inline **...** teacher emphasis parsing and rendering across PDF, Word & HTML.
3. StructuredNotes scientific extensions: PracticeQuestion, term_en, «تأکید استاد».
4. Typst primary PDF engine + ReportLab automatic fallback + visual QA check.
5. Direct Raw Text → Scientific Booklet chat intake (/booklet & direct paste).
6. Free Office «🎓 جزوهٔ علمی کامل (PDF + Word + HTML)» workflow.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import unicodedata

from docx import Document

from gamas_bot import texts
from gamas_bot.artifact_validation import validate_docx_artifact, validate_pdf_artifact
from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from gamas_bot.document.qa import inspect_docx, inspect_pdf
from gamas_bot.document.rtl import parse_emphasis_spans, strip_emphasis_markers
from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.llm import complete_json
from gamas_bot.llm_profiles import fold_system_into_user, model_supports_system_prompt
from gamas_bot.notes import (
    GlossaryEntry,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    PracticeQuestion,
    PracticalTip,
    StructuredNotes,
    notes_response_schema,
    structure_transcript,
)
from gamas_bot.notes_pdf import build_notes_pdf
from gamas_bot.notes_typst import build_notes_pdf_typst
from gamas_bot.review_sheet import (
    build_review_planning_document,
    build_review_units,
    parse_review_plan,
)
from gamas_bot.review_sheet_html import render_review_sheet_html
from tests.test_notes import make_settings


def _rich_scientific_notes() -> StructuredNotes:
    return StructuredNotes(
        title="فارماکولوژی داروهای قلبی و دیابت",
        subtitle="درسنامهٔ علمی جامع برای مرور سریع و آزمون",
        summary=(
            "در این فصل سازوکار اثر **Metformin** و تنظیم فشار خون "
            "(Blood Pressure) با فرمول استاندارد و جدول مقایسه‌ای بررسی می‌شود."
        ),
        learning_objectives=(
            "درک سازوکار **Metformin** در مهار گلوکونئوژنز کبدی",
            "تحلیل رابطهٔ همودینامیک BP = CO × SVR و پیچیدگی الگوریتم O(n log n)",
        ),
        sections=(
            NoteSection(
                heading="داروهای خط اول دیابت نوع ۲",
                paragraphs=(
                    "داروی **Metformin** خط اول درمان دیابت نوع ۲ است و با فعال‌سازی "
                    "مسیر **AMPK** تولید گلوکز کبدی (Hepatic Gluconeogenesis) را کاهش می‌دهد.",
                    "در ارزیابی عملکرد کلیوی باید مقدار **eGFR < 30** به عنوان معیار منع مصرف "
                    "به دقت کنترل شود.",
                ),
                bullets=(
                    "کاهش مقاومت به انسولین در بافت‌های محیطی (**Insulin Sensitivity**)",
                    "عدم ایجاد افت قند خون (Hypoglycemia) در مصرف تک‌دارویی",
                ),
                definitions=(
                    NoteDefinition(
                        term="گلوکونئوژنز",
                        term_en="Gluconeogenesis",
                        definition="فرایند ساخت گلوکز جدید از پیش‌سازهای غیرکربوهیدراتی در کبد.",
                    ),
                ),
                examples=(
                    "بیمار ۵۵ ساله با HbA1c معادل ۸.۲٪ درمان را با **۵۰۰ mg** متفورمین آغاز می‌کند.",
                ),
                steps=(
                    "بررسی پایهٔ کراتینین و **eGFR** پیش از شروع درمان",
                    "شروع با دوز پایین همراه وعدهٔ غذایی و افزایش تدریجی",
                ),
                formulas=(
                    "BP = CO × SVR — فشار خون شریانی برابر است با برون‌ده قلبی ضرب‌در مقاومت عروق محیطی",
                    "T(n) = O(n log n) — پیچیدگی زمانی مرتب‌سازی ادغامی (Merge Sort)",
                ),
                tables=(
                    NoteTable(
                        caption="جدول مقایسه‌ای داروهای خوراکی دیابت",
                        headers=("دارو", "سازوکار اصلی", "خطر هیپوگلیسمی"),
                        rows=(
                            ("Metformin", "کاهش گلوکونئوژنز کبدی", "بسیار کم"),
                            ("Sulfonylureas", "تحریک ترشح انسولین", "بالا"),
                        ),
                    ),
                ),
                callouts=(
                    NoteCallout(
                        kind="تأکید استاد",
                        text="این معیار امتحانی است: در **eGFR < 30** مصرف Metformin ممنوع است.",
                    ),
                    NoteCallout(
                        kind="هشدار",
                        text="در نارسایی شدید کلیوی خطر اسیدوز لاکتیک (Lactic Acidosis) وجود دارد.",
                    ),
                ),
                key_points=("**Metformin** داروی خط اول و بدون افزایش وزن است.",),
            ),
        ),
        practical_tips=(
            PracticalTip(
                text="در پرسش‌های تستی، اولین اقدام پیش از تجویز متفورمین بررسی eGFR است.",
                basis="ارزیابی عملکرد کلیوی پیش از شروع درمان",
                kind="آزمونی",
            ),
        ),
        key_points=("معیار کلیدی منع مصرف: **eGFR < 30 mL/min**.",),
        practice_questions=(
            PracticeQuestion(
                question="چرا متفورمین به عنوان خط اول دیابت نوع ۲ انتخاب می‌شود؟",
                answer="زیرا مقاومت به انسولین را کاهش می‌دهد، باعث افت قند خون نمی‌شود و اثر خنثی بر وزن دارد.",
                kind="مفهومی",
            ),
            PracticeQuestion(
                question="مهم‌ترین منع مصرف کلیوی متفورمین در چه حدی است؟",
                answer="در eGFR کمتر از ۳۰ میلی‌لیتر بر دقیقه مصرف متفورمین ممنوع است.",
                kind="آزمونی",
            ),
            PracticeQuestion(
                question="رابطهٔ همودینامیک فشار خون شریانی چگونه بیان می‌شود؟",
                answer="فشار خون شریانی برابر است با حاصل‌ضرب برون‌ده قلبی در مقاومت عروق محیطی (BP = CO × SVR).",
                kind="محاسباتی",
            ),
        ),
        review_questions=("تفاوت اثر Metformin و Sulfonylureas بر خطر افت قند خون چیست؟",),
        common_mistakes=("نادیده‌گرفتن بررسی **eGFR** پیش از افزایش دوز متفورمین.",),
        final_review=("خط اول دیابت نوع ۲: **Metformin** با پایش منظم eGFR.",),
        glossary=(
            GlossaryEntry(
                term="اسیدوز لاکتیک",
                term_en="Lactic Acidosis",
                definition="تجمع غیرطبیعی اسید لاکتیک در خون همراه با افت pH.",
            ),
        ),
    )


def _sample_review_plan_payload(notes: StructuredNotes) -> dict:
    units = build_review_units(notes)
    all_refs = [u.ref for u in units]
    buckets = [all_refs[i::4] for i in range(4)]
    colors = ("green", "amber", "red", "blue")
    icons = ("📘", "🧪", "⚠️", "📐")
    titles = (
        ("مبانی دیابت نوع ۲", "دیابت و متفورمین"),
        ("تعاریف و روند درمان", "تعاریف و روند"),
        ("تأکید استاد و هشدارها", "تأکید و هشدار"),
        ("فرمول‌ها و جدول مقایسه", "فرمول و جدول"),
    )
    return {
        "sections": [
            {
                "heading": titles[i][0],
                "nav_label": titles[i][1],
                "role": "مرور سریع",
                "weight": 2,
                "icon": icons[i],
                "color": colors[i],
                "units": buckets[i],
                "important": [buckets[i][0]],
                "traps": [],
            }
            for i in range(4)
            if buckets[i]
        ],
        "self_test": [],
    }


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    import pymupdf

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        raw = "\n".join(page.get_text("text") or "" for page in doc)
    return unicodedata.normalize("NFKC", raw).replace("\u0654", "").replace("لا", "ال")


class SystemPromptCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_model_supports_system_prompt_matrix(self) -> None:
        from gamas_bot.llm_profiles import (
            MODEL_PROFILES,
            ModelCapabilityProfile,
            normalize_model_id,
            register_model_profile,
        )

        # All three active production models support native system prompts.
        self.assertTrue(model_supports_system_prompt("gemini-3.5-flash-lite", provider_slug="gemini"))
        self.assertTrue(model_supports_system_prompt("deepseek-v4.1-flash", provider_slug="avalai"))
        self.assertTrue(
            model_supports_system_prompt("nvidia/nemotron-3.5-super-vl:free", provider_slug="openrouter")
        )
        # Extensibility: a future model without system-prompt support can be
        # registered cleanly without hardcoding legacy model lists.
        custom_id = "custom/no-system-demo"
        try:
            register_model_profile(
                ModelCapabilityProfile(
                    model_id=custom_id,
                    provider_slug="openrouter",
                    supports_system_prompt=False,
                )
            )
            self.assertFalse(model_supports_system_prompt(custom_id, provider_slug="openrouter"))
        finally:
            MODEL_PROFILES.pop(normalize_model_id(custom_id), None)

    def test_fold_system_into_user_merges_cleanly(self) -> None:
        folded = fold_system_into_user("قوانین علمی گاماس", "متن درس فیزیک")
        self.assertIn("قوانین علمی گاماس", folded)
        self.assertIn("متن درس فیزیک", folded)

    async def test_runtime_rejection_of_system_role_retries_with_folded_user_prompt(self) -> None:
        calls: list[dict] = []

        class _Resp:
            def __init__(self, status: int, payload: dict) -> None:
                self.status = status
                self.headers: dict[str, str] = {}
                self._payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

            async def json(self, content_type=None) -> dict:
                return self._payload

        class _Session:
            def post(self, url: str, **kwargs):
                import copy
                calls.append(copy.deepcopy(kwargs["json"]))
                if len(calls) == 1:
                    return _Resp(
                        400,
                        {
                            "error": {
                                "message": "Developer instruction is not enabled for this model",
                                "type": "invalid_request_error",
                            }
                        },
                    )
                return _Resp(
                    200,
                    {
                        "choices": [
                            {
                                "message": {"content": '{"ok": true}'},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )

        settings = make_settings(
            avalai_api_key="k",
            avalai_model="custom-no-system-model",
        )
        result = await complete_json(
            _Session(),
            settings,
            system="دستور سیستمی",
            user="متن ورودی",
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["messages"][0]["role"], "system")
        self.assertEqual(len(calls[1]["messages"]), 1)
        self.assertEqual(calls[1]["messages"][0]["role"], "user")
        self.assertIn("دستور سیستمی", calls[1]["messages"][0]["content"])
        self.assertIn("متن ورودی", calls[1]["messages"][0]["content"])


class EmphasisAndSchemaTests(unittest.TestCase):
    def test_parse_and_strip_emphasis_spans(self) -> None:
        text = "معیار **eGFR < 30** و **تأکید استاد** مهم است."
        spans = parse_emphasis_spans(text)
        self.assertEqual(
            spans,
            [
                ("معیار ", False),
                ("eGFR < 30", True),
                (" و ", False),
                ("تأکید استاد", True),
                (" مهم است.", False),
            ],
        )
        self.assertEqual(strip_emphasis_markers(text), "معیار eGFR < 30 و تأکید استاد مهم است.")

    def test_scientific_notes_roundtrip_and_closed_schema(self) -> None:
        notes = _rich_scientific_notes()
        restored = StructuredNotes.from_payload(notes.to_payload())
        self.assertEqual(len(restored.practice_questions), 3)
        self.assertEqual(restored.practice_questions[0].kind, "مفهومی")
        self.assertEqual(restored.sections[0].definitions[0].term_en, "Gluconeogenesis")
        self.assertEqual(restored.glossary[0].term_en, "Lactic Acidosis")
        self.assertEqual(restored.sections[0].callouts[0].kind, "تأکید استاد")

        schema = notes_response_schema()
        self.assertIn("practice_questions", schema["properties"])
        self.assertFalse(schema["additionalProperties"])
        def_props = (
            schema["properties"]["sections"]["items"]["properties"]["definitions"]["items"]
            ["properties"]
        )
        self.assertIn("term_en", def_props)
        glossary_props = schema["properties"]["glossary"]["items"]["properties"]
        self.assertIn("term_en", glossary_props)


class MultiFormatScientificRenderTests(unittest.TestCase):
    def test_typst_pdf_renders_cleanly_and_passes_visual_qa(self) -> None:
        notes = _rich_scientific_notes()
        meta = DocumentMeta(reference="GMS-999", source_name="raw-text.txt")
        pdf = build_notes_pdf_typst(notes, meta=meta)
        validate_pdf_artifact(pdf)
        qa_report = inspect_pdf(pdf)
        self.assertTrue(qa_report.ok, qa_report)

        extracted = _extract_pdf_text(pdf)
        self.assertIn("Metformin", extracted)
        self.assertIn("Gluconeogenesis", extracted)
        self.assertIn("BP = CO", extracted)
        self.assertIn("O(n log n)", extracted)
        self.assertIn("تأکید ویژه استاد", extracted)
        self.assertIn("پرسش", extracted)
        # Cover quote is preserved verbatim.
        self.assertIn("ثریا", extracted)

    def test_build_notes_pdf_falls_back_to_reportlab_on_typst_error(self) -> None:
        notes = _rich_scientific_notes()
        with mock.patch(
            "gamas_bot.notes_typst.build_notes_pdf_typst",
            side_effect=RuntimeError("synthetic typst outage"),
        ):
            pdf = build_notes_pdf(notes, engine="typst")
        validate_pdf_artifact(pdf)

    def test_docx_renders_emphasis_teacher_callout_and_practice_questions(self) -> None:
        notes = _rich_scientific_notes()
        meta = DocumentMeta(reference="GMS-999", source_name="raw-text.txt")
        docx_bytes = build_notes_docx(notes, meta=meta)
        validate_docx_artifact(docx_bytes)
        docx_qa = inspect_docx(docx_bytes)
        self.assertTrue(docx_qa.ok, docx_qa)
        doc = Document(io.BytesIO(docx_bytes))
        full_text = "\n".join(p.text for p in doc.paragraphs)
        for table in doc.tables:
            for row in table.rows:
                full_text += "\n" + " | ".join(cell.text for cell in row.cells)
        self.assertIn("تأکید استاد", full_text)
        self.assertIn("Gluconeogenesis", full_text)
        self.assertIn("پرسش‌ها و تمرین‌های درس", full_text)
        self.assertIn("BP = CO × SVR", full_text)
        # No raw ** markers leak into rendered Word text.
        self.assertNotIn("**", full_text)
        # Teacher emphasis highlight fill FFF1BF appears in Word document XML.
        import zipfile

        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            doc_xml = zf.read("word/document.xml").decode("utf-8")
        self.assertIn("FFF1BF", doc_xml)

    def test_html_review_sheet_renders_emphasis_and_practice_questions(self) -> None:
        notes = _rich_scientific_notes()
        units = build_review_units(notes)
        plan_doc = build_review_planning_document(notes, units, duration_minutes=20)
        self.assertIn("پرسش", plan_doc)
        plan = parse_review_plan(_sample_review_plan_payload(notes), notes)
        assert plan is not None
        self.assertGreaterEqual(len(plan.self_test), 3)
        html = render_review_sheet_html(plan)
        self.assertIn("<b><e>Metformin</e></b>", html)
        self.assertIn("<b>تأکید استاد</b>", html)
        self.assertNotIn("**Metformin**", html)


class DirectScientificModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_scientific_mode_preserves_enrichment_tables_and_qa(self) -> None:
        """With note_strict_grounding=False (default), scientific clarifications stay intact."""
        raw_input = "امروز درباره متفورمین حرف زدیم که خط اول دیابت نوع دو هست و زیر سی نباید بدیم."
        enriched = _rich_scientific_notes().to_json()

        class _Resp:
            status = 200
            headers: dict[str, str] = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": enriched}, "finish_reason": "stop"}]}
                ).encode("utf-8")

            async def json(self, content_type=None) -> dict:
                return {"choices": [{"message": {"content": enriched}, "finish_reason": "stop"}]}

        class _Session:
            def post(self, _url: str, **_kwargs):
                return _Resp()

        settings = make_settings(
            avalai_api_key="k",
            note_compile=False,
        )
        self.assertFalse(settings.note_strict_grounding)
        self.assertEqual(settings.note_pdf_engine, "typst")
        notes = await structure_transcript(raw_input, settings, _Session())
        self.assertEqual(notes.title, "فارماکولوژی داروهای قلبی و دیابت")
        self.assertEqual(len(notes.practice_questions), 3)
        self.assertEqual(notes.sections[0].definitions[0].term_en, "Gluconeogenesis")
        self.assertEqual(notes.sections[0].callouts[0].kind, "تأکید استاد")


class RawTextAndOfficeBookletIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "tmp").mkdir(parents=True, exist_ok=True)
        (root / "receipts").mkdir(parents=True, exist_ok=True)
        self.settings = make_settings(
            db_path=root / "bot.sqlite3",
            temp_dir=root / "tmp",
            receipt_dir=root / "receipts",
            session_path=root / "session",
            avalai_api_key="note-key",
            note_compile=False,
            office_enabled=True,
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings, db=self.db, client=mock.MagicMock())

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_booklet_command_prompts_then_accepts_raw_text(self) -> None:
        from tests.test_bot import FakeMessageEvent

        user_id = 7001
        cmd_event = FakeMessageEvent(user_id, "/booklet")
        await self.bot._handle_message(cmd_event)
        self.assertEqual(cmd_event.replies[-1], texts.RAW_TEXT_BOOKLET_PROMPT)
        self.assertIn(user_id, self.bot._pending_raw_text)

        called: list[str] = []

        async def fake_accept(_event, _uid, raw_text):
            called.append(raw_text)

        with mock.patch.object(self.bot, "_accept_raw_text", side_effect=fake_accept):
            msg_event = FakeMessageEvent(
                user_id, "متفورمین خط اول درمان دیابت نوع ۲ است و گلوکونئوژنز را مهار می‌کند."
            )
            await self.bot._handle_message(msg_event)
        self.assertEqual(len(called), 1)
        self.assertIn("متفورمین", called[0])
        self.assertNotIn(user_id, self.bot._pending_raw_text)

    async def test_substantial_raw_text_paste_auto_triggers_scientific_booklet(self) -> None:
        from tests.test_bot import FakeMessageEvent

        user_id = 7002
        long_study_text = (
            "در جلسه امروز درس فارماکولوژی درباره داروی متفورمین صحبت کردیم که خط اول درمان "
            "دیابت نوع دو محسوب می‌شود و با فعال‌سازی مسیر AMPK تولید گلوکز کبدی را کاهش می‌دهد. "
            "استاد تاکید کرد که در بیماران با eGFR کمتر از سی مصرف این دارو به دلیل خطر اسیدوز لاکتیک ممنوع است."
        )
        called: list[str] = []

        async def fake_accept(_event, _uid, raw_text):
            called.append(raw_text)

        with mock.patch.object(self.bot, "_accept_raw_text", side_effect=fake_accept):
            event = FakeMessageEvent(user_id, long_study_text)
            await self.bot._handle_message(event)
        self.assertEqual(called, [long_study_text])

    async def test_raw_text_job_persists_pdf_docx_and_review_html(self) -> None:
        from gamas_bot.jobs import Job
        from gamas_bot.progress import JobProgress
        from tests.test_bot import FakeMessageEvent

        user_id = 7003
        await self.db.ensure_user(user_id, "tester")
        raw_text = (
            "در این جلسه داروی متفورمین به عنوان خط اول درمان دیابت نوع ۲ بررسی شد. "
            "معیار منع مصرف کلیوی eGFR < 30 است و باید پیش از تجویز بررسی شود."
        )
        event = FakeMessageEvent(user_id, raw_text)
        submission_id = await self.db.create_submission(user_id, "text", "raw-text.txt")
        progress = await JobProgress.create(event, submission_id=submission_id)
        job = Job(
            submission_id=submission_id,
            user_id=user_id,
            kind="text",
            filename="raw-text.txt",
            message=event.message,
            is_admin=False,
            duration_hint=0.0,
            meta={"progress": progress, "raw_text": raw_text},
        )
        rich_notes = _rich_scientific_notes()
        plan_json = json.dumps(
            _sample_review_plan_payload(rich_notes),
            ensure_ascii=False,
        )

        class _ReviewResp:
            status = 200
            headers: dict[str, str] = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": plan_json}, "finish_reason": "stop"}]}
                ).encode("utf-8")

            async def json(self, content_type=None) -> dict:
                return {"choices": [{"message": {"content": plan_json}, "finish_reason": "stop"}]}

        class _ReviewSession:
            def post(self, _url: str, **_kwargs):
                return _ReviewResp()

        with mock.patch.object(
            self.bot, "_generate_notes", new=mock.AsyncMock(return_value=rich_notes)
        ), mock.patch.object(
            self.bot, "_http_session", return_value=_ReviewSession()
        ):
            await self.bot._run_job(job)

        pdf_row = await self.db.binary_artifact(submission_id, user_id, "booklet")
        docx_row = await self.db.binary_artifact(submission_id, user_id, "docx")
        html_row = await self.db.binary_artifact(submission_id, user_id, "review_html")
        self.assertIsNotNone(pdf_row)
        self.assertIsNotNone(docx_row)
        self.assertIsNotNone(html_row)
        assert pdf_row is not None and docx_row is not None and html_row is not None
        validate_pdf_artifact(bytes(pdf_row["content"]))
        validate_docx_artifact(bytes(docx_row["content"]))
        self.assertIn("فارماکولوژی", bytes(html_row["content"]).decode("utf-8"))

    async def test_office_format_keyboard_offers_scientific_booklet(self) -> None:
        from tests.test_bot import menu_payloads

        keyboard = self.bot._office_format_keyboard()
        self.assertIn(b"office:fmt:booklet", menu_payloads(keyboard))

    async def test_office_session_booklet_callback_generates_pdf_docx_and_html(self) -> None:
        from tests.test_bot import FakeCallbackEvent, FakeMessageEvent

        user_id = 7004
        await self.db.ensure_user(user_id, "office_user")
        session = self.bot._office_new_session(user_id, FakeMessageEvent(user_id, "start").message)
        assert session is not None
        session.add(
            "داروی متفورمین خط اول درمان دیابت نوع ۲ است و با مهار گلوکونئوژنز کبدی "
            "قند خون را کاهش می‌دهد. در eGFR کمتر از ۳۰ منع مصرف دارد."
        )
        delivered: list[tuple[str, bytes]] = []

        async def fake_send_file(_chat, path, **_kwargs):
            p = Path(path)
            delivered.append((p.name, p.read_bytes()))
            return mock.MagicMock(id=901)

        rich_notes = _rich_scientific_notes()
        plan_json = json.dumps(_sample_review_plan_payload(rich_notes), ensure_ascii=False)

        class _ReviewResp:
            status = 200
            headers: dict[str, str] = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": plan_json}, "finish_reason": "stop"}]}
                ).encode("utf-8")

            async def json(self, content_type=None) -> dict:
                return {"choices": [{"message": {"content": plan_json}, "finish_reason": "stop"}]}

        class _ReviewSession:
            def post(self, _url: str, **_kwargs):
                return _ReviewResp()

        cb = FakeCallbackEvent(user_id, b"office:fmt:booklet")
        with mock.patch(
            "gamas_bot.bot_documents.generate_full_booklet",
            new=mock.AsyncMock(return_value=(rich_notes, False)),
        ), mock.patch.object(
            self.bot, "_send_telegram_file", side_effect=fake_send_file
        ), mock.patch.object(
            self.bot, "_http_session", return_value=_ReviewSession()
        ):
            await self.bot._handle_callback(cb)

        exts = {Path(name).suffix.lower() for name, _ in delivered}
        self.assertEqual(exts, {".pdf", ".docx", ".html"})
        for name, data in delivered:
            if name.endswith(".pdf"):
                validate_pdf_artifact(data)
            elif name.endswith(".docx"):
                validate_docx_artifact(data)
            elif name.endswith(".html"):
                self.assertIn("فارماکولوژی", data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
