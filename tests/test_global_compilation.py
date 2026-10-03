"""Tests for the global-context layer of the note pipeline.

The pipeline used to write every chunk as an independent mini-lecture. These
tests pin the replacement behaviour:

* a single-part lecture stays exactly one call per chunk (no orientation, no
  compilation) — the classic path must not become more expensive;
* a multi-part lecture gets one cheap orientation call, every chunk prompt
  carries the same global context (title, ordered topics, terminology and its
  own position with neighbour windows) and one editorial compilation pass at
  the end;
* the compilation is accepted only when deterministic QA measures do not
  regress; on any error, or when the compiler drops content, the merged notes
  are delivered unchanged;
* the orientation pass is strictly non-fatal.
"""

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from support import fake_session_factory, make_settings

from gamas_bot import structuring as S
from gamas_bot.editorial import (
    CHUNKED_CONTENT_RULES,
    LectureContext,
    build_compile_document,
    build_context_block,
    compact_notes_payload,
    compile_is_better,
    parse_outline,
)
from gamas_bot.qa import NoteQAReport
from gamas_bot.structuring import (
    COMPILE_PROMPT,
    OUTLINE_PROMPT,
    TRANSCRIPT_PROMPT,
    StructuringError,
    merge_structured_notes,
    parse_structured_notes,
    structure_transcript,
)

CHUNK_BUDGET = 900
PREFIX_RE = re.compile(r"^\[[^\]]*\]\n\n")


def notes_json(body: str, *, heading: str = "بخش یکم", summary: str = "") -> str:
    payload = {
        "title": "درس آزمون",
        "sections": [{"heading": heading, "paragraphs": [body]}],
    }
    if summary:
        payload["summary"] = summary
    return json.dumps(payload, ensure_ascii=False)


class FakeNoteProvider:
    """A provider double that answers per pass and records every call.

    It is deliberately prompt-driven rather than call-order-driven: the tests
    must prove which *system prompt* each chunk received, not just how many
    calls happened.
    """

    def __init__(
        self,
        *,
        outline: str | None = None,
        compile_answer: str | None = None,
        compile_summary: str = "خلاصهٔ ویرایش‌شده",
        fail_outline: bool = False,
        fail_compile: bool = False,
    ) -> None:
        self.calls: list[dict] = []
        self.outline = outline
        self.compile_answer = compile_answer
        self.compile_summary = compile_summary
        self.fail_outline = fail_outline
        self.fail_compile = fail_compile

    # -- introspection helpers -------------------------------------------------
    def prompts(self) -> list[str]:
        return [call["prompt"] for call in self.calls]

    def chunk_calls(self) -> list[dict]:
        return [call for call in self.calls if call["prompt"] == TRANSCRIPT_PROMPT]

    def bodies(self) -> list[str]:
        return [PREFIX_RE.sub("", call["document"]) for call in self.chunk_calls()]

    def compile_answer_from_parts(self, *, summary: str | None = None) -> str:
        """A compilation answer that keeps every part, as a real compiler must."""
        return json.dumps(
            {
                "title": "درس آزمون",
                "summary": self.compile_summary if summary is None else summary,
                "sections": [
                    {"heading": "بخش یکم", "paragraphs": self.bodies()}
                ],
            },
            ensure_ascii=False,
        )

    # -- provider --------------------------------------------------------------
    # Deliberately a *synchronous* callable returning the answer text: the
    # suite patches an async function with an AsyncMock, and a sync
    # side_effect is used as the awaited result directly.
    def __call__(
        self,
        chunk,
        settings,
        session,
        prompt=None,
        *,
        system_prompt="",
        reminder="",
        **_kwargs,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "document": chunk,
                "reminder": reminder,
            }
        )
        if prompt == OUTLINE_PROMPT:
            if self.fail_outline:
                raise StructuringError("orientation call failed")
            if self.outline is not None:
                return self.outline
            return json.dumps(
                {"title": "درس آزمون", "topics": ["الف", "ب"]}, ensure_ascii=False
            )
        if prompt == COMPILE_PROMPT:
            if self.fail_compile:
                raise StructuringError("compilation call failed")
            if self.compile_answer is not None:
                return self.compile_answer
            return self.compile_answer_from_parts()
        return notes_json(PREFIX_RE.sub("", chunk))


class GlobalContextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The real ``structure_transcript`` flow against a provider double."""

    async def _run(self, text: str, provider: FakeNoteProvider, **overrides):
        settings = make_settings(**overrides)
        with patch("gamas_bot.structuring._structure_chunk", side_effect=provider), patch(
            "gamas_bot.structuring.TRANSCRIPT_CHUNK_CHARS", CHUNK_BUDGET
        ), patch("gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory):
            return await structure_transcript(text, settings)

    def _two_part_text(self) -> str:
        first = "نخستین جملهٔ درس دربارهٔ الف است. " * 12
        second = "دومین جملهٔ درس دربارهٔ ب است. " * 12
        return first + "\n" + second

    async def test_single_part_lecture_never_pays_for_global_context(self):
        provider = FakeNoteProvider()
        result = await self._run("متن کوتاه یک‌بخشی درس.", provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["prompt"], TRANSCRIPT_PROMPT)
        self.assertNotIn("زمینهٔ کلی درس", provider.calls[0]["system_prompt"])
        self.assertEqual(result.note_mode, "full")

    async def test_multi_part_lecture_gets_outline_context_and_compilation(self):
        parts = self._two_part_text()
        provider = FakeNoteProvider(
            outline=json.dumps(
                {
                    "title": "درس آزمون",
                    "topics": ["موضوع الف", "موضوع ب"],
                    "terminology": ["Metformin"],
                },
                ensure_ascii=False,
            ),
        )
        result = await self._run(parts, provider)

        # one orientation + one call per part + one compilation, and no repair
        self.assertEqual(provider.prompts().count(OUTLINE_PROMPT), 1)
        self.assertEqual(len(provider.chunk_calls()), 2)
        self.assertEqual(provider.prompts().count(COMPILE_PROMPT), 1)

        for index, call in enumerate(provider.chunk_calls(), start=1):
            prompt = call["system_prompt"]
            self.assertIn("زمینهٔ کلی درس", prompt)
            self.assertIn("موضوع الف", prompt)
            self.assertIn("Metformin", prompt)
            self.assertIn(f"بخش {S.to_persian_digits(index)}", prompt)
            self.assertIn(CHUNKED_CONTENT_RULES.splitlines()[0], prompt)
        # the first part must not claim to continue something that does not exist
        self.assertNotIn("پایانی بخش پیشین", provider.chunk_calls()[0]["system_prompt"])
        self.assertIn("آغاز بخش بعدی", provider.chunk_calls()[0]["system_prompt"])
        self.assertIn("پایانی بخش پیشین", provider.chunk_calls()[1]["system_prompt"])
        self.assertNotIn("آغاز بخش بعدی", provider.chunk_calls()[1]["system_prompt"])

        # the compilation was accepted: its summary replaced the merged one
        self.assertEqual(result.summary, "خلاصهٔ ویرایش‌شده")

    async def test_pipeline_hands_the_previous_headings_to_the_next_part(self):
        """The already-written headings ride along into the next part's prompt."""
        provider = FakeNoteProvider()
        await self._run(self._two_part_text(), provider)
        calls = provider.chunk_calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("عنوان‌های بخش پیشین", calls[0]["system_prompt"])
        self.assertIn("عنوان‌های بخش پیشین", calls[1]["system_prompt"])

    async def test_compilation_that_drops_content_is_rejected(self):
        parts = self._two_part_text()
        provider = FakeNoteProvider(
            compile_answer=notes_json("فقط یک جملهٔ کوتاه.", summary="خلاصهٔ ویرایش‌شده")
        )
        result = await self._run(parts, provider)
        self.assertEqual(result.summary, "")
        bodies = [section.paragraphs for section in result.sections]
        self.assertEqual(len(result.sections), 1)
        self.assertEqual(len(bodies[0]), 2)  # both halves of the lecture kept

    async def test_compilation_failure_keeps_the_merged_notes(self):
        parts = self._two_part_text()
        provider = FakeNoteProvider(fail_compile=True)
        result = await self._run(parts, provider)
        self.assertEqual(len(result.sections), 1)
        self.assertEqual(len(result.sections[0].paragraphs), 2)

    async def test_orientation_failure_is_non_fatal(self):
        parts = self._two_part_text()
        provider = FakeNoteProvider(fail_outline=True)
        result = await self._run(parts, provider)
        self.assertEqual(provider.prompts().count(OUTLINE_PROMPT), 1)
        for call in provider.chunk_calls():
            # The topic map is gone (no title, no topic list, no terminology)…
            self.assertNotIn("عنوان درس:", call["system_prompt"])
            self.assertNotIn("موضوع‌های درس به ترتیب", call["system_prompt"])
            self.assertNotIn("اصطلاح‌های کلیدی درس", call["system_prompt"])
        # …but the *positional* context that needs no model call survives, so
        # part two still knows which section the previous part ended with.
        self.assertIn("عنوان‌های بخش پیشین", provider.chunk_calls()[1]["system_prompt"])
        self.assertEqual(len(result.sections[0].paragraphs), 2)
        # the compilation still ran, without the topic map
        self.assertEqual(provider.prompts().count(COMPILE_PROMPT), 1)

    async def test_disabled_global_context_restores_the_plain_pipeline(self):
        provider = FakeNoteProvider()
        result = await self._run(
            self._two_part_text(), provider, note_global_context_enabled=False
        )
        self.assertEqual(len(provider.calls), 2)
        self.assertNotIn(OUTLINE_PROMPT, provider.prompts())
        self.assertNotIn(COMPILE_PROMPT, provider.prompts())
        self.assertEqual(len(result.sections), 1)

    async def test_outline_answer_shaped_like_notes_is_still_usable(self):
        # A model that ignores the outline schema and answers with note JSON
        # must still yield a topic list instead of losing global context.
        context = parse_outline(
            '{"title": "درس", "sections": [{"heading": "همودینامیک"}, '
            '{"heading": "نارسایی قلبی"}]}'
        )
        self.assertIsNotNone(context)
        self.assertEqual(context.topics, ("همودینامیک", "نارسایی قلبی"))

    async def test_outline_that_returns_noise_falls_back_without_context(self):
        provider = FakeNoteProvider(outline="متأسفم، نمی‌توانم.")
        result = await self._run(self._two_part_text(), provider)
        for call in provider.chunk_calls():
            self.assertNotIn("موضوع‌های درس به ترتیب", call["system_prompt"])
            self.assertNotIn("اصطلاح‌های کلیدی درس", call["system_prompt"])
        self.assertTrue(result.sections)


class MergeCoherenceTests(unittest.TestCase):
    """The merge is what makes many chunk results one booklet."""

    def notes(self, payload: str):
        return parse_structured_notes(payload)

    def test_halves_of_one_section_join_into_a_single_section(self):
        first = self.notes(
            '{"title": "t", "sections": [{"heading": "قلب", "paragraphs": ["پاراگراف نخست."], '
            '"definitions": [{"term": "CO", "definition": "برون‌ده قلبی"}]}]}'
        )
        second = self.notes(
            '{"title": "t", "sections": [{"heading": "قلب (ادامه)", "paragraphs": ["پاراگراف دوم."], '
            '"callouts": [{"kind": "هشدار", "text": "هشدار تازه"}]}]}'
        )
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.sections), 1)
        section = merged.sections[0]
        self.assertEqual(section.paragraphs, ("پاراگراف نخست.", "پاراگراف دوم."))
        self.assertEqual(len(section.definitions), 1)
        self.assertEqual(len(section.callouts), 1)

    def test_a_section_that_only_restates_an_earlier_one_is_dropped(self):
        original = self.notes(
            '{"title": "t", "sections": [{"heading": "تشخیص", "paragraphs": ["متن تشخیص."], '
            '"key_points": ["نکتهٔ تشخیص"]}]}'
        )
        tail = self.notes(
            '{"title": "t", "sections": [{"heading": "سایر", "paragraphs": ["متن پایانی."]}, '
            '{"heading": "تشخیص", "paragraphs": ["متن تشخیص."], "key_points": ["نکتهٔ تشخیص"]}]}'
        )
        merged = merge_structured_notes([original, tail])
        headings = [section.heading for section in merged.sections]
        self.assertEqual(headings, ["تشخیص", "سایر"])
        self.assertEqual(merged.sections[0].key_points, ("نکتهٔ تشخیص",))

    def test_distinct_content_under_a_repeated_heading_is_never_dropped(self):
        first = self.notes(
            '{"title": "t", "sections": [{"heading": "قلب", "paragraphs": ["الف یک."]}]}'
        )
        second = self.notes(
            '{"title": "t", "sections": [{"heading": "قلب", "paragraphs": ["الف دو."]}]}'
        )
        merged = merge_structured_notes([first, second])
        self.assertEqual(len(merged.sections), 1)
        self.assertEqual(merged.sections[0].paragraphs, ("الف یک.", "الف دو."))

    def test_a_second_different_table_becomes_labelled_bullets(self):
        first = self.notes(
            '{"title": "t", "sections": [{"heading": "مقایسه", '
            '"table": {"headers": ["الف", "ب"], "rows": [["۱", "۲"]]}}]}'
        )
        second = self.notes(
            '{"title": "t", "sections": [{"heading": "مقایسه", '
            '"table": {"headers": ["ج", "د"], "rows": [["۳", "۴"]]}}]}'
        )
        merged = merge_structured_notes([first, second])
        self.assertEqual(merged.sections[0].table.rows, [["۱", "۲"]])
        self.assertEqual(merged.sections[0].bullets, ("ج: ۳؛ د: ۴",))

    def test_summaries_are_unioned_not_replaced(self):
        first = self.notes('{"title": "t", "summary": "الف. ب.", "sections": [{"heading": "۱"}]}')
        second = self.notes('{"title": "t", "summary": "ب. ج.", "sections": [{"heading": "۲"}]}')
        merged = merge_structured_notes([first, second])
        self.assertEqual(merged.summary, "الف. ب. ج.")


class PromptAndDocumentTests(unittest.TestCase):
    """The pure parts of the layer: context block, payload, acceptance gate."""

    def test_context_block_carries_the_previous_part_headings(self):
        """A split topic must be able to reuse the heading already written."""
        block = build_context_block(
            LectureContext(title="درس", topics=("موضوع الف", "موضوع ب")),
            index=2,
            total=2,
            previous_headings=("موضوع الف",),
        )
        self.assertIn("عنوان‌های بخش پیشین", block)
        self.assertIn("«موضوع الف»", block)
        # The rule the model needs: reuse the exact wording, no «ادامه» suffix.
        self.assertIn("عیناً همان عنوان", block)
        self.assertIn("ادامه", block)

    def test_context_block_without_previous_headings_stays_quiet(self):
        block = build_context_block(LectureContext(title="درس"), index=1, total=2)
        self.assertNotIn("عنوان‌های بخش پیشین", block)

    def test_context_block_states_position_and_neighbours(self):
        context = LectureContext(
            title="فیزیولوژی",
            topics=("قلب", "کلیه"),
            terminology=("Cardiac Output",),
        )
        block = build_context_block(
            context, index=2, total=3, previous_tail="...پایان بخش", next_head="آغاز..."
        )
        self.assertIn("عنوان درس: فیزیولوژی", block)
        self.assertIn("(۱) قلب", block)
        self.assertIn("(۲) کلیه", block)
        self.assertIn("Cardiac Output", block)
        self.assertIn("بخش ۲ از ۳", block)
        self.assertIn("...پایان بخش", block)
        self.assertIn("آغاز...", block)
        self.assertIn("دوباره جزوه نکنید", block)

    def test_compact_payload_omits_empty_fields(self):
        notes = parse_structured_notes(
            '{"title": "د", "sections": [{"heading": "ب", "paragraphs": ["متن"]}]}'
        )
        payload = compact_notes_payload(notes)
        self.assertEqual(payload["sections"], [{"heading": "ب", "paragraphs": ["متن"]}])
        self.assertNotIn("summary", payload)
        self.assertNotIn("table", payload["sections"][0])

    def test_compile_document_contains_outline_notes_and_instruction(self):
        notes = parse_structured_notes(
            '{"title": "د", "sections": [{"heading": "ب", "paragraphs": ["متن"]}]}'
        )
        document = build_compile_document(
            LectureContext(title="درس", topics=("ب",)), notes
        )
        self.assertIn("زمینهٔ کلی درس", document)
        self.assertIn("جزوهٔ فعلی", document)
        self.assertIn("هیچ مطلبی را حذف نکنید", document)

    def _report(self, *, notes_chars: int, semantic: float, coverage: float) -> NoteQAReport:
        return NoteQAReport(
            source_numbers=10,
            preserved_numbers=int(10 * coverage),
            notes_text_chars=notes_chars,
            source_chars=1000,
            total_units=10,
            covered_units=int(10 * semantic),
        )

    def test_compile_acceptance_gate(self):
        before = self._report(notes_chars=1000, semantic=1.0, coverage=1.0)
        accepted, reason = compile_is_better(
            before, self._report(notes_chars=1000, semantic=1.0, coverage=1.0),
            before_chars=1000, after_chars=1000,
        )
        self.assertTrue(accepted, reason)

        rejected, reason = compile_is_better(
            before, self._report(notes_chars=900, semantic=0.5, coverage=1.0),
            before_chars=1000, after_chars=900,
        )
        self.assertFalse(rejected)
        self.assertIn("semantic", reason)

        rejected, reason = compile_is_better(
            before, self._report(notes_chars=500, semantic=1.0, coverage=1.0),
            before_chars=1000, after_chars=500,
        )
        self.assertFalse(rejected)
        self.assertIn("kept only", reason)

        rejected, reason = compile_is_better(
            before, self._report(notes_chars=1000, semantic=1.0, coverage=0.8),
            before_chars=1000, after_chars=1000,
        )
        self.assertFalse(rejected)
        self.assertIn("signal coverage", reason)


if __name__ == "__main__":
    unittest.main()
