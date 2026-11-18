"""Interactive HTML review sheet: plan, grounding, rendering, validation, storage.

Every fixture is synthetic and non-sensitive. No test reaches a provider: the
one planning call is replaced by a fake ``complete_json`` that returns a
hand-written payload, so grounding and validation are what is under test.
"""

from __future__ import annotations

import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gamas_bot import artifacts, texts
from gamas_bot.artifact_validation import (
    MAX_REVIEW_HTML_BYTES,
    inspect_review_html,
    validate_review_html_artifact,
)
from gamas_bot.config import Settings
from gamas_bot.db import MIGRATIONS_DIR, Database
from gamas_bot.db_core import BINARY_ARTIFACT_KINDS
from gamas_bot.llm import LLMError
from gamas_bot.notes import (
    QUALITY_DEGRADED,
    QUALITY_RECOVERED,
    QUALITY_REPAIRED,
    NoteCallout,
    NoteDefinition,
    NoteSection,
    NoteTable,
    StructuredNotes,
)
from gamas_bot.review_sheet import (
    DEFAULT_REVIEW_MINUTES,
    MAX_PLANNING_CHARS,
    REVIEW_PLAN_TASK,
    ComparisonPair,
    ReviewSheetError,
    ExamTrap,
    ReviewPlanError,
    ReviewQuestion,
    ReviewSection,
    ReviewSheetPlan,
    allocate_minutes,
    build_review_units,
    generate_review_sheet,
    grounded_evidence,
    parse_review_plan,
    plan_review_sheet,
)
from gamas_bot.review_sheet_html import (
    ReviewSheetRenderError,
    render_review_sheet_html,
    review_sheet_filename,
)
from tests.support import make_settings


def sample_notes(title: str = "فارماکوکینتیک پایه") -> StructuredNotes:
    """Five source sections covering definitions, bullets, tables, steps and a formula."""
    return StructuredNotes(
        title=title,
        sections=(
            NoteSection(
                heading="مقدمه بر فارماکوکینتیک",
                paragraphs=("جذب دارو یعنی ورود دارو به گردش خون از محل تجویز.",),
                bullets=(
                    "سه فاز اصلی شامل جذب، توزیع و دفع است.",
                    "دفع کلیه شامل ترشح و بازجذب است.",
                ),
                definitions=(
                    NoteDefinition(
                        term="فارماکوکینتیک",
                        definition="مطالعه سرنوشت دارو در بدن",
                        term_en="Pharmacokinetics",
                    ),
                ),
            ),
            NoteSection(
                heading="زیست‌فراهمی",
                bullets=(
                    "زیست‌فراهمی نسبت مقدار جذب‌شده است.",
                    "برای تزریق وریدی زیست‌فراهمی ۱۰۰ درصد است.",
                ),
                callouts=(NoteCallout(kind="warning", text="نسبت وریدی و خوراکی را اشتباه نگیرید."),),
                table=NoteTable(
                    headers=["روش", "زیست‌فراهمی"],
                    rows=[["وریدی", "۱۰۰٪"], ["خوراکی", "کمتر"]],
                    caption="جدول مقایسه",
                ),
            ),
            NoteSection(
                heading="توزیع دارو",
                paragraphs=("حجم توزیع نشان می‌دهد دارو تا چه حد در بافت‌ها پخش می‌شود.",),
                steps=("گام اول: اندازه‌گیری غلظت پلاسما.", "گام دوم: محاسبه حجم توزیع."),
            ),
            NoteSection(
                heading="دفع دارو",
                key_points=("نیمه‌عمر حاصل تقسیم حجم توزیع بر کلیرانس است.",),
                formulas=("t1/2 = 0.693 Vd / CL",),
            ),
            NoteSection(
                heading="کلیرانس",
                bullets=("کلیرانس حجم پلاسمایی است که در واحد زمان پاکسازی می‌شود.",),
            ),
        ),
    )


def sample_payload() -> dict:
    """A grounded planning response for :func:`sample_notes` (references checked below)."""
    return {
        "sections": [
            {
                "heading": "اصول جذب و توزیع", "nav_label": "جذب و توزیع", "role": "مفاهیم پایه",
                "weight": 3, "icon": "📘", "color": "green",
                "units": ["s1.p1", "s1.b1", "s1.b2", "s1.d1", "s3.p1", "s3.st1"],
                "important": ["s1.b1"],
                "traps": [
                    {
                        "explanation": "سه فاز اصلی جذب، توزیع و دفع است.",
                        "evidence": ["s1.b1"],
                    },
                ],
            },
            {
                "heading": "زیست‌فراهمی", "nav_label": "زیست‌فراهمی", "role": "مقایسه",
                "weight": 2, "icon": "🧪", "color": "amber",
                "units": ["s2.b1", "s2.b2", "s2.c1", "s2.tb1"], "important": ["s2.b2"], "traps": [],
            },
            {
                "heading": "دفع و نیمه‌عمر", "nav_label": "دفع", "role": "فرمول",
                "weight": 2, "icon": "⚠️", "color": "blue",
                "units": ["s4.k1", "s4.f1"], "important": [], "traps": [],
            },
            {
                "heading": "کلیرانس", "nav_label": "کلیرانس", "role": "تعریف",
                "weight": 1, "icon": "📗", "color": "purple",
                "units": ["s5.b1"], "important": [], "traps": [],
            },
        ],
        "self_test": [
            {
                "question": "سه فاز اصلی دارو در بدن کدام‌اند؟",
                "answer": "جذب، توزیع و دفع.",
                "evidence": ["s1.b1"],
            },
            {
                "question": "زیست‌فراهمی چیست؟",
                "answer": "نسبت مقدار جذب‌شده است.",
                "evidence": ["s2.b1"],
            },
            {
                "question": "نیمه‌عمر از چه چیزی به دست می‌آید؟",
                "answer": "حاصل تقسیم حجم توزیع بر کلیرانس است.",
                "evidence": ["s4.k1"],
            },
        ],
        "frequent_pairs": [
            {
                "left": "جذب",
                "right": "توزیع",
                "distinction": "جذب ورود دارو به گردش خون است؛ توزیع پخش آن در بافت‌هاست.",
                "evidence": ["s1.p1", "s3.p1"],
            },
        ],
    }


def sample_plan(duration: int = DEFAULT_REVIEW_MINUTES) -> ReviewSheetPlan:
    plan = parse_review_plan(sample_payload(), sample_notes(), duration_minutes=duration)
    assert plan is not None
    return plan


class ReviewPlanTests(unittest.TestCase):
    def test_units_are_stable_addressable_and_cover_each_content_kind(self):
        units = build_review_units(sample_notes())
        refs = [unit.ref for unit in units]
        self.assertEqual(refs, [unit.ref for unit in build_review_units(sample_notes())])
        self.assertEqual(len(refs), len(set(refs)))
        for expected in ("s1.p1", "s1.b2", "s1.d1", "s2.c1", "s2.tb1", "s3.st2", "s4.k1", "s4.f1", "s5.b1"):
            self.assertIn(expected, refs)

    def test_minutes_are_exact_integers_for_many_weight_sets(self):
        rng = random.Random(7)
        for _ in range(300):
            count = rng.randint(1, 11)
            total = rng.randint(count, 480)
            weights = [rng.randint(-2, 15) for _ in range(count)]
            minutes = allocate_minutes(weights, total)
            self.assertEqual(sum(minutes), total)
            self.assertTrue(all(isinstance(value, int) and value >= 1 for value in minutes))

    def test_minutes_reject_fewer_minutes_than_sections(self):
        with self.assertRaises(ReviewPlanError):
            allocate_minutes([1, 1, 1, 1], 3)

    def test_default_plan_totals_thirty_minutes_with_a_one_minute_self_test(self):
        plan = sample_plan()
        self.assertEqual(plan.duration_minutes, 30)
        self.assertEqual(plan.total_minutes, 30)
        self.assertEqual(plan.self_test_minutes, 1)
        self.assertEqual(len(plan.sections), 4)
        self.assertEqual(sum(section.minutes for section in plan.sections), 29)

    def test_a_custom_duration_is_honoured_exactly(self):
        plan = sample_plan(duration=45)
        self.assertEqual(plan.total_minutes, 45)

    def test_duration_bounds_are_enforced(self):
        for value in (4, 481):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_review_plan(sample_payload(), sample_notes(), duration_minutes=value)

    def test_unknown_and_duplicate_references_are_dropped(self):
        payload = sample_payload()
        payload["sections"][0]["units"] = ["s9.p9", "s1.p1", "s1.p1", "s1.b1", "s1.b2", "s1.d1", "s3.p1", "s3.st1"]
        plan = parse_review_plan(payload, sample_notes())
        assert plan is not None
        first = plan.sections[0].units
        self.assertEqual([unit.ref for unit in first], ["s1.p1", "s1.b1", "s1.b2", "s1.d1", "s3.p1", "s3.st1"])
        # The same unit is never shown in two sections.
        all_refs = [unit.ref for section in plan.sections for unit in section.units]
        self.assertEqual(len(all_refs), len(set(all_refs)))

    def test_unsupported_trap_question_and_pair_are_rejected_and_counted(self):
        payload = sample_payload()
        payload["sections"][0]["traps"].append(
            {"explanation": "این تله بدون پشتوانه است و عدد ۴۲۰ دارد.", "evidence": ["s1.b1"]}
        )
        payload["sections"][0]["traps"].append(
            {"explanation": "سه فاز اصلی جذب، توزیع و دفع است.", "evidence": []}
        )
        payload["self_test"].append(
            {"question": "عدد ۹۹۹ چیست؟", "answer": "۹۹۹ است.", "evidence": ["s1.b1"]}
        )
        payload["frequent_pairs"].append(
            {"left": "الف", "right": "ب", "distinction": "تفاوت کاملاً ساختگی", "evidence": ["s9.p1"]}
        )
        plan = parse_review_plan(payload, sample_notes())
        assert plan is not None
        self.assertEqual(len(plan.sections[0].exam_traps), 1)
        self.assertEqual(len(plan.self_test), 3)
        self.assertEqual(len(plan.frequent_pairs), 1)
        self.assertEqual(plan.rejected_items, 4)

    def test_numbers_must_appear_in_the_cited_evidence(self):
        catalog = {unit.ref: unit for unit in build_review_units(sample_notes())}
        self.assertIsNotNone(grounded_evidence(["زیست‌فراهمی ۱۰۰ درصد است"], ["s2.b2"], catalog))
        self.assertIsNotNone(grounded_evidence(["زیست‌فراهمی 100 درصد است"], ["s2.b2"], catalog))
        self.assertIsNone(grounded_evidence(["زیست‌فراهمی ۵۰ درصد است"], ["s2.b2"], catalog))
        self.assertIsNone(grounded_evidence(["زیست‌فراهمی ۱۰۰ درصد است"], ["s9.zz"], catalog))

    def test_too_few_grounded_sections_yields_no_sheet(self):
        payload = sample_payload()
        payload["sections"] = payload["sections"][:3]
        self.assertIsNone(parse_review_plan(payload, sample_notes()))

    def test_too_few_grounded_self_test_questions_yields_no_sheet(self):
        payload = sample_payload()
        payload["self_test"] = payload["self_test"][:2]
        self.assertIsNone(parse_review_plan(payload, sample_notes()))

    def test_invalid_icon_and_colour_fall_back_to_safe_defaults(self):
        payload = sample_payload()
        payload["sections"][0].update({"icon": "<script>", "color": "neon"})
        plan = parse_review_plan(payload, sample_notes())
        assert plan is not None
        self.assertEqual(plan.sections[0].icon, "📘")
        self.assertIn(plan.sections[0].color, {"green", "purple", "blue", "amber", "turquoise", "red", "pink"})
        self.assertEqual(plan.sections[0].background, f"bg-{plan.sections[0].color}")

    def test_section_count_is_capped_at_eleven_educational_sections(self):
        notes = StructuredNotes(
            title="سیزده بخش",
            sections=tuple(
                NoteSection(heading=f"بخش {n}", paragraphs=("دفع کلیه شامل ترشح و بازجذب است.",))
                for n in range(1, 14)
            ),
        )
        payload = {
            "sections": [
                {"heading": f"بخش {n}", "nav_label": f"بخش {n}", "role": "مرور", "weight": 1,
                 "icon": "📘", "color": "green", "units": [f"s{n}.p1"], "important": [], "traps": []}
                for n in range(1, 14)
            ],
            "self_test": [
                {"question": "دفع کلیه چیست؟", "answer": "ترشح و بازجذب است.", "evidence": ["s1.p1"]}
                for _ in range(3)
            ],
            "frequent_pairs": [],
        }
        plan = parse_review_plan(payload, notes)
        assert plan is not None
        self.assertEqual(len(plan.sections), 11)
        self.assertEqual(plan.total_minutes, 30)
        self.assertGreaterEqual(plan.omitted_units, 2)

    def test_a_non_object_payload_is_structurally_rejected(self):
        with self.assertRaises(ReviewPlanError):
            parse_review_plan(["not", "an", "object"], sample_notes())

    def test_a_booklet_with_no_content_yields_no_units_and_no_sheet(self):
        self.assertIsNone(parse_review_plan(sample_payload(), StructuredNotes(title="خالی")))


class PlanningCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_planning_call_uses_the_review_task_without_content_filtering(self):
        settings = make_settings()
        notes = sample_notes()
        complete = mock.AsyncMock(return_value=sample_payload())
        with mock.patch("gamas_bot.review_sheet.complete_json", new=complete):
            plan = await plan_review_sheet(notes, settings, session=mock.sentinel.session)
        self.assertIsNotNone(plan)
        complete.assert_awaited_once()
        kwargs = complete.await_args.kwargs
        self.assertEqual(kwargs["task"], REVIEW_PLAN_TASK)
        self.assertEqual(REVIEW_PLAN_TASK, "BOOKLET_REVIEW_PLAN")
        self.assertIn("s1.p1 | جذب دارو", kwargs["user"])
        self.assertIn("JSON", kwargs["system"])

    async def test_provider_errors_propagate_instead_of_producing_a_plan(self):
        with mock.patch(
            "gamas_bot.review_sheet.complete_json",
            new=mock.AsyncMock(side_effect=LLMError("provider down")),
        ), self.assertRaises(LLMError):
            await plan_review_sheet(sample_notes(), make_settings(), session=mock.sentinel.session)

    async def test_an_oversized_booklet_is_not_sent_to_a_provider(self):
        complete = mock.AsyncMock(return_value=sample_payload())
        with mock.patch("gamas_bot.review_sheet.complete_json", new=complete), mock.patch(
            "gamas_bot.review_sheet.MAX_PLANNING_CHARS", 10
        ):
            self.assertIsNone(await plan_review_sheet(sample_notes(), make_settings(), session=mock.sentinel.session))
        complete.assert_not_awaited()
        self.assertGreater(MAX_PLANNING_CHARS, 10_000)

    async def test_a_degraded_booklet_never_reaches_the_planner(self):
        complete = mock.AsyncMock(return_value=sample_payload())
        degraded = replace(sample_notes(), quality_state=QUALITY_DEGRADED)
        with mock.patch("gamas_bot.review_sheet.complete_json", new=complete), mock.patch.object(
            Settings, "note_configured", new_callable=mock.PropertyMock, return_value=True
        ):
            result = await generate_review_sheet(degraded, make_settings(), mock.sentinel.session, reference="GMS-1")
        self.assertIsNone(result)
        complete.assert_not_awaited()

    async def test_a_recovered_source_fallback_never_reaches_the_planner(self):
        # RECOVERED is the deterministic source-preserving fallback, not an AI booklet.
        complete = mock.AsyncMock(return_value=sample_payload())
        recovered = replace(sample_notes(), quality_state=QUALITY_RECOVERED)
        with mock.patch("gamas_bot.review_sheet.complete_json", new=complete), mock.patch.object(
            Settings, "note_configured", new_callable=mock.PropertyMock, return_value=True
        ):
            result = await generate_review_sheet(recovered, make_settings(), mock.sentinel.session, reference="GMS-1")
        self.assertIsNone(result)
        complete.assert_not_awaited()

    async def test_no_provider_or_no_session_skips_without_a_call(self):
        complete = mock.AsyncMock(return_value=sample_payload())
        with mock.patch("gamas_bot.review_sheet.complete_json", new=complete), mock.patch.object(
            Settings, "note_configured", new_callable=mock.PropertyMock, return_value=False
        ):
            self.assertIsNone(
                await generate_review_sheet(sample_notes(), make_settings(), mock.sentinel.session, reference="GMS-1")
            )
        self.assertIsNone(
            await generate_review_sheet(sample_notes(), make_settings(), None, reference="GMS-1")
        )
        complete.assert_not_awaited()

    async def test_generate_announces_each_stage_and_returns_a_validated_file(self):
        stages: list[str] = []

        async def announce(label: str) -> None:
            stages.append(label)

        with mock.patch(
            "gamas_bot.review_sheet.complete_json", new=mock.AsyncMock(return_value=sample_payload())
        ), mock.patch.object(
            Settings, "note_configured", new_callable=mock.PropertyMock, return_value=True
        ):
            result = await generate_review_sheet(
                sample_notes(), make_settings(), mock.sentinel.session,
                reference="GMS-7", announce=announce,
            )
        assert result is not None
        self.assertEqual(
            stages, [texts.REVIEW_STAGE_PLANNING, texts.REVIEW_STAGE_RENDERING, texts.REVIEW_STAGE_VALIDATING]
        )
        self.assertTrue(result.filename.endswith("GMS-7.html"))
        self.assertEqual(inspect_review_html(result.content).errors, ())
        self.assertEqual(result.section_count, 5)

    async def test_generate_raises_when_the_rendered_sheet_fails_validation(self):
        broken = "<!DOCTYPE html><html><body>not a sheet</body></html>".encode("utf-8")
        with mock.patch(
            "gamas_bot.review_sheet.complete_json", new=mock.AsyncMock(return_value=sample_payload())
        ), mock.patch.object(
            Settings, "note_configured", new_callable=mock.PropertyMock, return_value=True
        ), mock.patch(
            "gamas_bot.review_sheet_html.render_review_sheet_html",
            return_value=broken.decode("utf-8"),
        ), self.assertRaises(ReviewSheetError) as caught:
            await generate_review_sheet(sample_notes(), make_settings(), mock.sentinel.session, reference="GMS-1")
        self.assertIn("review", str(caught.exception).lower())


class RenderTests(unittest.TestCase):
    def test_rendered_sheet_is_self_contained_and_passes_validation(self):
        html = render_review_sheet_html(sample_plan())
        self.assertEqual(inspect_review_html(html.encode("utf-8")).errors, ())
        self.assertNotRegex(html, r"""(?:src|href)\s*=\s*["']?https?:""")
        self.assertNotIn("<link", html)
        self.assertEqual(html.count("data:font/ttf;base64,"), 2)

    def test_hostile_text_is_escaped_in_every_position(self):
        hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>&\"'"
        plan = ReviewSheetPlan(
            title=hostile,
            duration_minutes=30,
            sections=(
                ReviewSection(
                    source_section=1,
                    heading=hostile,
                    nav_label=hostile,
                    educational_role=hostile,
                    minutes=sample_plan().sections[0].minutes,
                    icon=hostile,
                    color="green",
                    background="bg-green",
                    units=sample_plan().sections[0].units,
                    important_refs=(),
                    exam_traps=(ExamTrap(hostile, ("s1.b1",)),),
                ),
                *sample_plan().sections[1:],
            ),
            self_test=(ReviewQuestion(hostile, hostile, ("s1.b1",)),) * 3,
            frequent_pairs=(ComparisonPair(hostile, hostile, hostile, ("s1.b1",)),),
        )
        html = render_review_sheet_html(plan)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertEqual(html.count("<script>"), 1)
        self.assertEqual(inspect_review_html(html.encode("utf-8")).errors, ())

    def test_fonts_are_embedded_and_missing_fonts_fail_the_render(self):
        with mock.patch("gamas_bot.review_sheet_html.find_font", return_value=None), self.assertRaises(
            ReviewSheetRenderError
        ):
            render_review_sheet_html(sample_plan())

    def test_visible_numbers_are_persian_and_timing_attributes_are_latin(self):
        html = render_review_sheet_html(sample_plan())
        self.assertIn("۳۰ دقیقه", html)
        self.assertNotRegex(html, r">[^<]*\b30 دقیقه")
        durations = [int(value) for value in re.findall(r'data-t="(\d+)"', html)]
        self.assertEqual(sum(durations), 30)
        self.assertTrue(all(re.fullmatch(r"\d+", str(value)) for value in durations))

    def test_quality_label_is_shown_for_a_repaired_booklet(self):
        plan = replace(sample_plan(), quality_state=QUALITY_REPAIRED)
        self.assertIn("اصلاح‌شده", render_review_sheet_html(plan))

    def test_filename_is_persian_html_and_sanitised(self):
        name = review_sheet_filename("جزوه / ناامن:*?", "GMS-9")
        self.assertTrue(name.endswith(" - GMS-9.html"))
        self.assertNotIn("/", name)
        self.assertNotIn("?", name)
        self.assertIn("جزوه مرور تعاملی", name)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.html = render_review_sheet_html(sample_plan())

    def assertRejected(self, mutated: str, expected: str):
        report = inspect_review_html(mutated.encode("utf-8"))
        self.assertTrue(
            any(expected in error for error in report.errors),
            f"expected {expected!r} in {report.errors!r}",
        )

    def test_the_supplied_linter_passes_a_rendered_sheet(self):
        # Spec section 11's checker is the authoritative template check; run it on real output.
        linter = Path(__file__).resolve().parents[1] / "scripts" / "lint_review_sheet.py"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheet.html"
            path.write_text(self.html, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(linter), str(path), "30"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        self.assertIn("RESULT: PASS", result.stdout, result.stdout + result.stderr)

    def test_the_rendered_sheet_loads_nothing_from_the_network(self):
        self.assertIsNone(re.search(r"https?://|//fonts\.|<link\b", self.html))

    def test_the_untouched_sheet_is_accepted(self):
        report = inspect_review_html(self.html.encode("utf-8"))
        self.assertEqual(report.errors, ())
        self.assertEqual(report.section_count, 5)
        self.assertEqual(report.duration_minutes, 30)

    def test_structural_and_safety_violations_are_hard_errors(self):
        cases = {
            "external image": (
                self.html.replace("</main>", '<img src="https://evil.example/x.png"></main>'),
                "tag <img> is not allowed",
            ),
            "external script": (
                self.html.replace("<script>", '<script src="https://evil.example/a.js">', 1),
                "attribute 'src' is not allowed",
            ),
            "event handler": (
                self.html.replace('<h1>', '<h1 onclick="x()">', 1),
                "attribute 'onclick' is not allowed",
            ),
            "inline style attribute": (
                self.html.replace('<h1>', '<h1 style="color:red">', 1),
                "attribute 'style' is not allowed",
            ),
            "iframe": (
                self.html.replace("</main>", '<iframe src="about:blank"></iframe></main>'),
                "tag <iframe> is not allowed",
            ),
            "missing content security policy": (
                re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', "", self.html),
                "missing Content-Security-Policy",
            ),
            "tampered script no longer matches its hash": (
                self.html.replace("<script>", "<script>var __tampered=1;", 1),
                "does not match its Content-Security-Policy hash",
            ),
            "external font or url in css": (
                self.html.replace("</style>", "@import url(https://fonts.example/x.css);</style>", 1),
                "stylesheet references an external resource",
            ),
            "minutes do not add up": (
                self.html.replace('data-t="11"', 'data-t="12"', 1),
                "must add up exactly",
            ),
            "persian digits in a timing attribute": (
                self.html.replace('data-t="1"', 'data-t="۱"', 1),
                "must be an integer with Latin digits",
            ),
            "self-test not last": (
                self.html.replace('class="sec quiz"', 'class="sec"', 1),
                "the final section must be the self-test",
            ),
            "dangling navigation link": (
                self.html.replace('href="#top"', 'href="#missing"', 1),
                "points to a missing section",
            ),
            "unknown css class": (
                self.html.replace("<h1>", '<h1 class="evil">', 1),
                "unexpected class names",
            ),
            "too few self-test questions": (
                re.sub(r"<details>.*?</details>", "", self.html, count=1, flags=re.S),
                "at least 3 questions",
            ),
        }
        for name, (mutated, expected) in cases.items():
            with self.subTest(case=name):
                self.assertRejected(mutated, expected)

    def test_a_document_without_the_fa_rtl_envelope_is_rejected(self):
        self.assertRejected(self.html.replace('lang="fa" dir="rtl"', 'lang="en"', 1), "root element")

    def test_encoding_and_size_limits_are_enforced(self):
        self.assertTrue(inspect_review_html(b"").errors)
        self.assertTrue(inspect_review_html(b"\xff\xfe\x00bad").errors)
        self.assertTrue(inspect_review_html(b"x" * (MAX_REVIEW_HTML_BYTES + 1)).errors)
        with self.assertRaises(ValueError):
            validate_review_html_artifact(b"<html></html>")

    def test_latin_digits_in_copied_booklet_text_are_only_a_warning(self):
        # The renderer converts digits to Persian, so a Latin digit must be injected to test the rule.
        mutated = self.html.replace("</main>", "<p>Dose 42 mg</p></main>", 1)
        report = inspect_review_html(mutated.encode("utf-8"))
        self.assertEqual(report.errors, ())
        self.assertTrue(any("Latin digits" in warning for warning in report.warnings))


class ArtifactContractTests(unittest.TestCase):
    def test_kind_is_a_binary_artifact_with_a_short_callback(self):
        self.assertEqual(artifacts.KIND_REVIEW_HTML, "review_html")
        self.assertIn("review_html", BINARY_ARTIFACT_KINDS)
        payload = artifacts.callback_for("GMS-123456", artifacts.KIND_REVIEW_HTML)
        self.assertLessEqual(len(payload), 64)
        self.assertEqual(artifacts.parse_callback(payload.decode("ascii")), ("GMS-123456", "review_html"))

    def test_delivery_markup_offers_the_button_only_when_the_file_exists(self):
        without = artifacts.delivery_markup(reference="GMS-1", speakers=0, has_booklet=True, kinds={"booklet"})
        with_html = artifacts.delivery_markup(
            reference="GMS-1", speakers=0, has_booklet=True, kinds={"booklet", "review_html"}
        )
        labels_without = [button.text for row in without for button in row if hasattr(button, "text")]
        labels_with = [button.text for row in with_html for button in row if hasattr(button, "text")]
        self.assertNotIn(texts.BTN_GET_REVIEW_HTML, labels_without)
        self.assertIn(texts.BTN_GET_REVIEW_HTML, labels_with)
        self.assertEqual(texts.BTN_GET_REVIEW_HTML, "🌐 دریافت جزوه مرور تعاملی HTML")

    def test_the_results_screen_names_the_html_file_only_when_it_exists(self):
        kwargs = {
            "reference": "GMS-1", "speakers": None, "has_booklet": True, "has_keypoints": False,
            "learning_available": True, "has_transcript": True, "has_txt": True, "has_docx": True,
        }
        self.assertNotIn(texts.DELIVERY_ROW_REVIEW_HTML, texts.delivery_screen(**kwargs))
        self.assertIn(texts.DELIVERY_ROW_REVIEW_HTML, texts.delivery_screen(has_review_html=True, **kwargs))


class StorageMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _partial_migrations(self) -> Path:
        """A copy of the v3 directory that stops at migration 032 (the pre-HTML schema)."""
        target = self.root / "migrations-032"
        target.mkdir()
        for path in MIGRATIONS_DIR.glob("*.sql"):
            if path.name < "033_":
                shutil.copy(path, target / path.name)
        return target

    async def test_fresh_database_accepts_and_scopes_review_html_to_its_owner(self):
        db = Database(self.root / "fresh.sqlite3")
        await db.connect()
        try:
            await db.ensure_user(11)
            await db.ensure_user(22)
            submission = await db.create_submission(11, "audio", "a.wav")
            await db.binary_artifact_upsert(
                submission, 11, "review_html", filename="جزوه.html",
                content_type="text/html", content=b"<!DOCTYPE html>x",
            )
            stored = await db.binary_artifact(submission, 11, "review_html")
            assert stored is not None
            self.assertEqual(bytes(stored["content"]), b"<!DOCTYPE html>x")
            self.assertIsNone(await db.binary_artifact(submission, 22, "review_html"))
            self.assertIn("review_html", await db.binary_artifact_kinds(submission, 11))
            self.assertEqual(await db.binary_artifact_kinds(submission, 22), set())
            with self.assertRaises(ValueError):
                await db.binary_artifact_upsert(
                    submission, 11, "unknown", filename="x", content_type="x", content=b"x",
                )
        finally:
            await db.close()

    async def test_a_populated_pre_033_database_upgrades_without_losing_rows(self):
        path = self.root / "populated.sqlite3"
        with mock.patch("gamas_bot.db.MIGRATIONS_DIR", self._partial_migrations()):
            old = Database(path)
            await old.connect()
            await old.ensure_user(11)
            first = await old.create_submission(11, "audio", "a.wav")
            second = await old.create_submission(11, "audio", "b.wav")
            await old.binary_artifact_upsert(
                first, 11, "booklet", filename="a.pdf", content_type="application/pdf",
                content=b"%PDF-one", revision_id=7,
            )
            await old.binary_artifact_upsert(
                first, 11, "docx", filename="a.docx", content_type="application/octet-stream",
                content=b"PK-two",
            )
            await old.binary_artifact_upsert(
                second, 11, "booklet", filename="b.pdf", content_type="application/pdf",
                content=b"%PDF-three",
            )
            await old.binary_artifact_delivery_started(first, 11, "booklet")
            await old.binary_artifact_delivery_finished(first, 11, "booklet", succeeded=True)
            before = await self._snapshot(old.db)
            await old.close()

        upgraded = Database(path)
        await upgraded.connect()
        try:
            after = await self._snapshot(upgraded.db)
            self.assertEqual(before, after)
            # Ids keep advancing from the old high-water mark; nothing is reused.
            await upgraded.binary_artifact_upsert(
                second, 11, "review_html", filename="b.html",
                content_type="text/html", content=b"<!DOCTYPE html>new",
            )
            new_id = (await upgraded.db.execute_fetchall(
                "SELECT binary_artifact_id FROM submission_binary_artifacts WHERE kind = 'review_html'"
            ))[0][0]
            self.assertGreater(int(new_id), max(int(row[0]) for row in before))
            with self.assertRaises(sqlite3.IntegrityError):
                await upgraded.db.execute(
                    "INSERT INTO submission_binary_artifacts (submission_id, user_id, kind, filename, "
                    "content_type, content, sha256, created_at, updated_at) "
                    "VALUES (?, 11, 'bogus', 'x', 'x', x'00', 'x', 'x', 'x')",
                    (first,),
                )
            self.assertEqual(await upgraded.db.execute_fetchall("PRAGMA foreign_key_check"), [])
        finally:
            await upgraded.close()

    async def test_replaying_the_migrations_is_a_no_op(self):
        path = self.root / "replay.sqlite3"
        first = Database(path)
        await first.connect()
        await first.ensure_user(11)
        submission = await first.create_submission(11, "audio", "a.wav")
        await first.binary_artifact_upsert(
            submission, 11, "review_html", filename="r.html", content_type="text/html", content=b"<html>",
        )
        await first.close()
        second = Database(path)
        await second.connect()
        try:
            applied = await second.db.execute_fetchall("SELECT COUNT(*) FROM schema_migrations")
            self.assertEqual(int(applied[0][0]), len(list(MIGRATIONS_DIR.glob("*.sql"))))
            stored = await second.binary_artifact(submission, 11, "review_html")
            assert stored is not None
            self.assertEqual(bytes(stored["content"]), b"<html>")
        finally:
            await second.close()

    @staticmethod
    async def _snapshot(conn) -> list[tuple]:
        rows = await conn.execute_fetchall(
            "SELECT binary_artifact_id, submission_id, user_id, kind, filename, content, sha256, "
            "revision_id, delivery_attempts, delivery_successes, delivery_failures "
            "FROM submission_binary_artifacts ORDER BY binary_artifact_id"
        )
        return [tuple(row) for row in rows]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
