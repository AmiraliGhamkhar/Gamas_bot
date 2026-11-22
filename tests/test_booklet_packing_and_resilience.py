"""Regression tests for multi-topic packing, 262K/65K budgets, quota & failover."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from unittest.mock import patch

from gamas_bot import llm, llm_quota, notes as n
from gamas_bot.booklet_verification import verify_and_repair
from gamas_bot.config import (
    DEFAULT_NOTE_CONTEXT_TOKENS,
    DEFAULT_NOTE_MAX_OUTPUT_TOKENS,
    DEFAULT_NOTE_PACK_TARGET_CHARS,
    check,
    from_env,
)
from gamas_bot.document_model import NormalizedDocument, SourceBlock, plan_from_document
from gamas_bot.office_notes import _normalize_office_source, generate_full_booklet, office_source_chunks
from gamas_bot.semantic_segments import SemanticSource, build_semantic_topics, pack_semantic_units
from scripts.booklet_regression_bench import lesson
from tests.support import make_settings
from tests.test_notes import FakeSession, _FakeResponse


def _shipped_settings(tmp_path, **extra):
    env = {
        "TELEGRAM_BOT_TOKEN": "123:synthetic",
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "synthetic-hash",
        "ADMIN_IDS": "1",
        "SUBSCRIPTION_REQUIRED": "false",
        "AVALAI_API_KEY": "avalai-synthetic",
        "GEMINI_API_KEY": "gemini-synthetic",
        "OPENROUTER_API_KEY": "openrouter-synthetic",
        "DB_PATH": str(tmp_path / "bot.sqlite"),
        "NOTE_TTFB_TIMEOUT_SECONDS": "0",
        **{k: str(v) for k, v in extra.items()},
    }
    with patch.dict("os.environ", env, clear=True):
        return from_env(env_file=str(tmp_path / "missing.env"))


def test_shipped_defaults_pass_config_check_without_timeout_warning(tmp_path):
    settings = _shipped_settings(tmp_path)
    assert settings.note_context_tokens == DEFAULT_NOTE_CONTEXT_TOKENS == 262144
    assert settings.note_max_output_tokens == DEFAULT_NOTE_MAX_OUTPUT_TOKENS == 65536
    assert settings.note_pack_target_chars == DEFAULT_NOTE_PACK_TARGET_CHARS == 20000
    problems = check(settings)
    assert not any("will time out on a slow gateway" in item for item in problems), problems


def test_multipage_pdf_packs_into_two_requests_and_passes_verification(tmp_path):
    # 20 pages * ~1,400 chars = ~28,000 chars -> packed into 2 requests at 20k target.
    pages = [
        SourceBlock(
            "",
            "page",
            f"[صفحهٔ {i}]\nتوضیح علمی دربارهٔ ساختار سلول و انتقال مواد از غشا. " * 22 + "\n",
            0,
            0,
            page_number=i,
        )
        for i in range(1, 21)
    ]
    document = NormalizedDocument.from_blocks(pages, source_type="pdf", title="زیست‌شناسی")
    assert 24_000 <= len(document.text) <= 35_000
    settings = _shipped_settings(tmp_path)
    calls: list[str] = []

    async def provider(_session, _settings, *, user, **_kwargs):
        calls.append(user)
        return {
            "title": "زیست‌شناسی",
            "sections": [
                {
                    "heading": f"بخش بسته {len(calls)}",
                    "paragraphs": ["توضیح علمی دربارهٔ ساختار سلول و انتقال مواد از غشا."],
                }
            ],
            "final_review": ["جمع‌بندی ساختار سلول و انتقال مواد."],
        }

    with patch.object(n, "complete_json", side_effect=provider):
        notes = asyncio.run(n.structure_document(document, settings, object()))

    assert 1 <= len(calls) <= 2
    assert len(notes.input_ranges) == len(calls)
    assert "".join(document.text[r.start_offset:r.end_offset] for r in notes.input_ranges) == document.text
    assert notes.verification_report is not None
    assert "missing_topic" not in {issue.code for issue in notes.verification_report.issues}
    assert "topic_order" not in {issue.code for issue in notes.verification_report.issues}
    assert "broken_reference" not in {issue.code for issue in notes.verification_report.issues}
    assert notes.verification_report.passed


def test_30_slide_pptx_packs_into_one_request_and_keeps_all_slide_locators(tmp_path):
    slides = [
        SourceBlock(
            "",
            "slide",
            f"[اسلاید {i}]\nتوضیح مفاهیم پایهٔ مدارهای الکتریکی و قانون اهم در این درس. " * 8 + "\n",
            0,
            0,
            slide_number=i,
        )
        for i in range(1, 31)
    ]
    document = NormalizedDocument.from_blocks(slides, source_type="pptx", title="مدار الکتریکی")
    assert 14_000 <= len(document.text) <= 20_000
    settings = _shipped_settings(tmp_path)
    calls = 0

    async def provider(_session, _settings, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "title": "مدار الکتریکی",
            "sections": [
                {
                    "heading": "مفاهیم پایهٔ مدارهای الکتریکی",
                    "paragraphs": ["توضیح مفاهیم پایهٔ مدارهای الکتریکی و قانون اهم در این درس."],
                }
            ],
            "final_review": ["جمع‌بندی قانون اهم."],
        }

    with patch.object(n, "complete_json", side_effect=provider):
        notes = asyncio.run(n.structure_document(document, settings, object()))

    assert calls == 1
    assert notes.verification_report is not None and notes.verification_report.passed
    assert len(notes.sections[0].source_locators) == 30
    assert any("1" in loc or "۱" in loc for loc in notes.sections[0].source_locators)
    assert any("30" in loc or "۳۰" in loc for loc in notes.sections[0].source_locators)


def test_50k_char_source_packs_into_three_requests_with_full_coverage(tmp_path):
    text = lesson(240)
    assert 48_000 <= len(text) <= 55_000
    document = NormalizedDocument.from_source_text(text)
    settings = _shipped_settings(tmp_path)
    calls = 0

    async def provider(_session, _settings, *, user, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "title": "چرخهٔ آب و اندازه‌گیری",
            "sections": [
                {
                    "heading": f"بخش {calls}",
                    "paragraphs": [
                        "اندازه‌گیری در شرایط یکسان امکان مقایسهٔ مشاهده‌ها را فراهم می‌کند."
                    ],
                }
            ],
            "final_review": ["جمع‌بندی آزمایش."],
        }

    with patch.object(n, "complete_json", side_effect=provider):
        notes = asyncio.run(n.structure_document(document, settings, object()))

    assert calls == 3
    assert "".join(document.text[r.start_offset:r.end_offset] for r in notes.input_ranges) == document.text


def test_unknown_small_output_model_caps_packing_below_20k(tmp_path):
    pages = [
        SourceBlock("", "page", f"[صفحهٔ {i}]\n" + ("توضیح علمی دربارهٔ فیزیک. " * 60) + "\n", 0, 0, page_number=i)
        for i in range(1, 11)
    ]
    document = NormalizedDocument.from_blocks(pages, source_type="pdf", title="فیزیک")
    settings = _shipped_settings(
        tmp_path,
        NOTE_PROVIDERS="avalai",
        AVALAI_MODEL="custom-unregistered-4k-model",
        NOTE_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS="4096",
    )
    sent_lengths: list[int] = []

    async def provider(_session, _settings, *, user, **_kwargs):
        sent_lengths.append(len(user))
        return {
            "title": "فیزیک",
            "sections": [{"heading": "فیزیک", "paragraphs": ["توضیح علمی دربارهٔ فیزیک."]}],
            "final_review": ["خلاصهٔ فیزیک."],
        }

    with patch.object(n, "complete_json", side_effect=provider):
        notes = asyncio.run(n.structure_document(document, settings, object()))

    # 4096 * 1.5 * 0.65 = 3993 chars cap -> must split into more than 2 calls instead of 1 giant 15k call.
    assert len(sent_lengths) >= 4
    assert all(r.end_offset - r.start_offset <= 4_000 for r in notes.input_ranges)


def test_split_topic_sections_merge_when_heading_matches():
    first = n.NoteSection(
        heading="فصل اول",
        paragraphs=("پاراگراف اول.",),
        topic_id="t001",
        section_id="t001:0:20000:1",
        source_block_ids=("b1",),
    )
    second = n.NoteSection(
        heading="ادامهٔ فصل اول",
        paragraphs=("پاراگراف دوم.",),
        topic_id="t001",
        section_id="t001:20000:35000:1",
        source_block_ids=("b2",),
    )
    merged = n.merge_structured_notes([
        n.StructuredNotes(title="درس", sections=(first,)),
        n.StructuredNotes(title="درس", sections=(second,)),
    ])
    assert len(merged.sections) == 1
    assert merged.sections[0].paragraphs == ("پاراگراف اول.", "پاراگراف دوم.")
    assert merged.sections[0].section_id == "t001:0:35000:1"
    assert merged.sections[0].source_block_ids == ("b1", "b2")


def test_quota_releases_unused_retry_slot_and_sends_full_65536_output_budget(tmp_path):
    settings = _shipped_settings(
        tmp_path,
        NOTE_PROVIDERS="openrouter",
        OPENROUTER_RPD_LIMIT="15",
        OPENROUTER_RPM_LIMIT="100",
    )
    session = FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                },
            )
            for _ in range(15)
        ]
    )

    async def run_all():
        for _ in range(15):
            assert await llm.complete_json(
                session, settings, system="S", user="U", task="BOOKLET_GENERATION"
            ) == {"ok": True}

    asyncio.run(run_all())
    assert len(session.calls) == 15
    assert session.calls[0]["json"]["max_tokens"] == 65536
    with sqlite3.connect(settings.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM llm_quota_requests WHERE provider='openrouter'"
        ).fetchone()[0]
    assert count == 15


def test_quota_rpm_wait_recovers_on_last_route_instead_of_failing(tmp_path):
    db_path = tmp_path / "rpm_wait.sqlite"
    budget = llm_quota.ProviderBudget(rpm=1, max_in_flight=1)
    held = llm_quota.reserve(str(db_path), "avalai", budget, 100, attempts=1)
    llm_quota.finish(held, actual_tokens=100, succeeded=True, attempts_used=1)
    # Age the row so it expires in ~0.12s.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE llm_quota_requests SET started=?", (time.time() - 59.88,))
        conn.commit()
    settings = _shipped_settings(
        tmp_path,
        DB_PATH=str(db_path),
        NOTE_PROVIDERS="avalai",
        AVALAI_RPM_LIMIT="1",
        LLM_SLOT_WAIT_TIMEOUT_SECONDS="5",
    )
    session = FakeSession([
        _FakeResponse(200, {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]})
    ])
    result = asyncio.run(llm.complete_json(session, settings, system="S", user="U", task="BOOKLET_GENERATION"))
    assert result == {"ok": True}


def test_invalid_booklet_payload_fails_over_to_next_provider_instead_of_fallback(tmp_path):
    settings = _shipped_settings(tmp_path, NOTE_PROVIDERS="avalai,openrouter")
    bad_avalai = _FakeResponse(
        200,
        {"choices": [{"message": {"content": json.dumps({"title": "خالی", "sections": []})}, "finish_reason": "stop"}]},
    )
    good_openrouter = _FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title": "درس فیزیک",
                                "sections": [
                                    {"heading": "قانون اول", "paragraphs": ["جسم ساکن در حالت سکون باقی می‌ماند."]}
                                ],
                                "final_review": ["مرور قانون اول نیوتن."],
                            },
                            ensure_ascii=False,
                        )
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )
    session = FakeSession([bad_avalai, good_openrouter])
    notes = asyncio.run(
        n.structure_document("# قانون اول\nجسم ساکن در حالت سکون باقی می‌ماند.", settings, session)
    )
    assert len(session.calls) == 2
    assert "avalai" in session.calls[0]["url"]
    assert "openrouter" in session.calls[1]["url"]
    assert notes.quality_state != n.QUALITY_RECOVERED
    assert notes.sections[0].heading == "قانون اول"


def test_direct_mode_verification_preserves_persian_number_words_and_structural_labels():
    source_text = (
        "# روش‌های حل معادله\n"
        "در این درس دو روش اصلی برای حل معادله بررسی می‌شود و دبی جریان ۴۸ میلی‌لیتر بر دقیقه است."
    )
    document = NormalizedDocument.from_source_text(source_text)
    plan = plan_from_document(document)
    topic = plan.topics[0]
    section = n.NoteSection(
        heading="روش‌های حل معادله",
        paragraphs=(
            "مثال ۱: در این درس ۲ روش اصلی برای حل معادله بررسی می‌شود.",
            "دبی جریان در این حالت 48 ml گزارش شده است.",
        ),
        topic_id=topic.id,
        parent_topic_id=topic.parent_id,
        section_id=f"{topic.id}:0:{len(document.text)}:1",
        source_block_ids=topic.unit_ids,
    )
    draft = n.StructuredNotes(title="روش‌های حل معادله", sections=(section,), final_review=("جمع‌بندی",))
    writer = n.SectionWriter(make_settings(note_strict_grounding=False, note_compile=False), object())
    verified = asyncio.run(verify_and_repair(draft, document, plan, writer, allow_repair=False))
    assert verified.quality_state == n.QUALITY_VERIFIED
    assert verified.sections[0].paragraphs == section.paragraphs
    assert verified.verification_report is not None
    issue_codes = {issue.code for issue in verified.verification_report.issues}
    assert not (issue_codes & {"number_mismatch", "unit_mismatch", "removed_unverified_content", "source_node_fallback"})

    # Meanwhile a fabricated unit-bearing measurement must still be caught and withheld.
    bad_section = n.NoteSection(
        heading="روش‌های حل معادله",
        paragraphs=("دوز تجویزی 999 mg است.",),
        topic_id=topic.id,
        parent_topic_id=topic.parent_id,
        section_id=f"{topic.id}:0:{len(document.text)}:1",
        source_block_ids=topic.unit_ids,
    )
    bad_draft = n.StructuredNotes(title="روش‌های حل معادله", sections=(bad_section,), final_review=("جمع‌بندی",))
    bad_verified = asyncio.run(verify_and_repair(bad_draft, document, plan, writer, allow_repair=False))
    assert bad_verified.verification_report is not None
    assert any(
        issue.code in ("number_mismatch", "removed_unverified_content", "source_node_fallback")
        for issue in bad_verified.verification_report.issues
    )
    assert "999" not in "\n".join(p for s in bad_verified.sections for p in s.paragraphs)


def test_office_unlocated_source_splits_blocks_and_surfaces_recovered_state(tmp_path):
    raw = "# فصل اول\nمتن پاراگراف اول.\n\n# فصل دوم\nمتن پاراگراف دوم."
    chunks = office_source_chunks(raw)
    normalized = _normalize_office_source(chunks, "درس", "txt", None)
    assert len(normalized.source_blocks) >= 2

    # When all routes fail in direct mode, generate_full_booklet must report fallback=True.
    settings = _shipped_settings(tmp_path, NOTE_PROVIDERS="avalai")
    session = FakeSession([_FakeResponse(503, {"error": {"message": "down"}})] * 4)
    with patch.object(llm, "_retry_delay_seconds", return_value=0):
        booklet, used_fallback = asyncio.run(
            generate_full_booklet(chunks, title="درس", settings=settings, session=session)
        )
    assert used_fallback is True
    assert booklet.quality_state == n.QUALITY_RECOVERED
    # Deterministic fallback must not duplicate raw '##' inside body paragraphs.
    for section in booklet.sections:
        for paragraph in section.paragraphs:
            assert not paragraph.startswith("## ")
            assert not paragraph.startswith("# ")


def test_pack_semantic_units_preserves_exact_source_offsets():
    pages = [
        SourceBlock("", "page", f"Page {i} body content.\n", 0, 0, page_number=i)
        for i in range(1, 11)
    ]
    document = NormalizedDocument.from_blocks(pages, source_type="pdf")
    plan = plan_from_document(document)
    units = build_semantic_topics(document, plan)
    source = SemanticSource(document)
    packed = pack_semantic_units(source, units, target_chars=100)
    assert 2 <= len(packed) < len(units)
    assert "".join(source.body(u) for u in packed) == document.text
    assert tuple(tid for u in packed for tid in u.covered_topic_ids) == tuple(u.topic_id for u in units)
