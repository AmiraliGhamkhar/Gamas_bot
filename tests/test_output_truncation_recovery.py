"""Output-token exhaustion: distinct, bounded, and source-preserving.

A provider that stops at its output-token budget has not failed the request and
has not failed the lecture: the answer simply did not fit. The confirmed defect
was that this condition was indistinguishable from "the model is down", so a part
whose notes needed more output tokens than the configured budget lost *all* of its
notes — while the JSON-repair retry could only spend the same budget again on the
identical request.

These tests pin the replacement contract:

* output-token exhaustion is raised as its own condition, not as a generic
  provider error;
* the oversized part is re-requested as two smaller parts cut from the same text
  — never as the identical request — and the two answers are merged additively;
* the recovery is bounded (a job may spend only so many extra calls), and it
  never applies to a failure that is not an output-budget problem (a 502, a 429
  or an invalid request must not be answered by splitting the input);
* when the text cannot be split usefully, the failure is reported instead of
  silently dropped, so the transcript fallback still delivers the lecture.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from support import fake_session_factory, make_settings

from gamas_bot.structuring import (
    MIN_TRUNCATION_PART_CHARS,
    NoteOutputTruncated,
    StructuringError,
    _provider_response,
    split_document_for_truncation,
    structure_transcript,
)

#: One chunk per job: wide enough that the whole lecture is a single part, which
#: is the case the recovery has to handle.
CHUNK_BUDGET = 4000
#: Documents at or above this length answer with an exhausted output budget,
#: while each half of the lecture stays below it.
TRUNCATE_ABOVE = 900


def notes_json(marker: str) -> str:
    return json.dumps(
        {
            "title": "درس آزمون",
            "sections": [{"heading": marker, "paragraphs": [f"محتوای {marker}"]}],
        },
        ensure_ascii=False,
    )


class TruncatingProvider:
    """Answers normally below ``TRUNCATE_ABOVE`` characters, truncates above it."""

    def __init__(self) -> None:
        self.documents: list[str] = []

    def __call__(self, chunk, settings, session, prompt=None, *, system_prompt="", reminder="", **_kwargs):
        self.documents.append(chunk)
        if len(chunk) >= TRUNCATE_ABOVE:
            raise NoteOutputTruncated("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
        # The answer names the lecture markers it actually saw, so a test can
        # prove which part of the source each answer covered.
        markers = [marker for marker in ("ALFA", "BRAVO") if marker in chunk]
        return notes_json("+".join(markers) or "بخش")


class AlwaysTruncating(TruncatingProvider):
    """Truncates every request, to pin the bounds of the recovery."""

    def __call__(self, chunk, settings, session, prompt=None, *, system_prompt="", reminder="", **_kwargs):
        self.documents.append(chunk)
        raise NoteOutputTruncated("truncated")


def _long_lecture() -> str:
    """One part of the lecture, distinctive at both ends.

    Sized so the whole part exceeds the (test-local) truncation threshold while
    each half fits under it — the situation the recovery exists for.
    """
    middle = " ".join(
        f"جملهٔ شمارهٔ {index} دربارهٔ موضوع درس و تعریف و مثال آن توضیح می‌دهد."
        for index in range(20)
    )
    return f"ALFA {middle} BRAVO"


class TruncationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, text: str, provider, *, note_repair_enabled: bool = False):
        # The QA repair pass is a second, optional consumer of the provider, so
        # the counting tests switch it off; its own truncation behaviour is
        # pinned by test_a_failing_repair_pass_keeps_the_split_recovered_notes.
        settings = make_settings(note_repair_enabled=note_repair_enabled)
        with patch("gamas_bot.structuring._structure_chunk", side_effect=provider), patch(
            "gamas_bot.structuring.TRANSCRIPT_CHUNK_CHARS", CHUNK_BUDGET
        ), patch("gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory):
            return await structure_transcript(text, settings)

    async def test_truncated_part_is_re_requested_as_two_smaller_parts(self):
        provider = TruncatingProvider()
        result = await self._run(_long_lecture(), provider)
        # The oversized call, then one call per half: never the identical
        # request twice.
        self.assertEqual(len(provider.documents), 3)
        self.assertGreaterEqual(len(provider.documents[0]), TRUNCATE_ABOVE)
        self.assertLess(len(provider.documents[1]), TRUNCATE_ABOVE)
        self.assertLess(len(provider.documents[2]), TRUNCATE_ABOVE)
        # Both ends of the lecture survived into the notes.
        payload = json.dumps(result.to_payload(), ensure_ascii=False)
        self.assertIn("ALFA", payload)
        self.assertIn("BRAVO", payload)

    async def test_the_split_keeps_every_word_of_the_source(self):
        provider = TruncatingProvider()
        original = _long_lecture()
        await self._run(original, provider)
        # The two re-requested parts are the same text, in order, with the
        # position prefix stripped; only the whitespace at the seam is normalised.
        first = provider.documents[1].split("]\n\n", 1)[-1]
        second = provider.documents[2]
        self.assertEqual(" ".join(f"{first} {second}".split()), " ".join(original.split()))

    async def test_a_failure_that_is_not_an_output_budget_problem_is_never_split(self):
        calls: list[str] = []

        def failing(chunk, settings, session, prompt=None, *, system_prompt="", reminder="", **_kwargs):
            calls.append(chunk)
            raise StructuringError("سرویس gemini خطای HTTP 502 داد.")

        with self.assertRaises(StructuringError):
            await self._run(_long_lecture(), failing)
        self.assertEqual(len(calls), 1)

    async def test_a_failing_repair_pass_keeps_the_split_recovered_notes(self):
        """A repair that also truncates must not cost the recovered notes."""

        class RepairTruncatingProvider(TruncatingProvider):
            def __call__(self, chunk, settings, session, prompt=None, *, system_prompt="", reminder="", **_kwargs):
                if reminder:
                    self.documents.append(chunk)
                    raise NoteOutputTruncated("repair answer hit the output budget")
                return super().__call__(
                    chunk, settings, session, prompt,
                    system_prompt=system_prompt, reminder=reminder, **_kwargs
                )

        provider = RepairTruncatingProvider()
        result = await self._run(_long_lecture(), provider, note_repair_enabled=True)
        payload = json.dumps(result.to_payload(), ensure_ascii=False)
        self.assertIn("ALFA", payload)
        self.assertIn("BRAVO", payload)

    async def test_the_recovery_is_bounded_by_the_job_split_budget(self):
        provider = TruncatingProvider()
        with patch("gamas_bot.structuring.MAX_TRUNCATION_SPLITS", 0):
            with self.assertRaises(NoteOutputTruncated):
                await self._run(_long_lecture(), provider)
        self.assertEqual(len(provider.documents), 1)

    async def test_the_split_budget_bounds_the_extra_calls_a_job_may_spend(self):
        """A part that truncates again is re-split only while budget remains."""
        provider = AlwaysTruncating()
        with patch("gamas_bot.structuring.MAX_TRUNCATION_SPLITS", 1):
            with self.assertRaises(NoteOutputTruncated):
                await self._run(_long_lecture(), provider)
        # The oversized call, then the first half. With the budget spent the
        # condition is reported instead of buying more provider calls.
        self.assertEqual(len(provider.documents), 2)

    async def test_an_unsplittable_part_fails_loudly_instead_of_dropping_content(self):
        provider = TruncatingProvider()
        text = ("کلمه درسی " * 200).strip()
        with patch("gamas_bot.structuring.MIN_TRUNCATION_PART_CHARS", 10_000):
            with self.assertRaises(NoteOutputTruncated):
                await self._run(text, provider)
        self.assertEqual(len(provider.documents), 1)

    async def test_a_part_that_fits_still_costs_one_call(self):
        provider = TruncatingProvider()
        await self._run("متن کوتاه درس دربارهٔ الف.", provider)
        self.assertEqual(len(provider.documents), 1)


class TruncationConditionTests(unittest.TestCase):
    """The condition itself: distinct, and reported for every provider shape."""

    def test_every_provider_truncation_signal_raises_the_distinct_condition(self):
        cases = {
            "openai_compatible": {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
            "gemini": {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]},
            "anthropic": {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]},
        }
        for provider, payload in cases.items():
            with self.subTest(provider=provider):
                with self.assertRaises(NoteOutputTruncated) as caught:
                    _provider_response(payload, provider)
                # Existing callers catch StructuringError; the distinction is an
                # addition, never a break.
                self.assertIsInstance(caught.exception, StructuringError)


class SplitterTests(unittest.TestCase):
    def test_a_split_keeps_every_word_and_returns_two_usable_halves(self):
        text = " ".join(f"واژهٔ {index}" for index in range(400))
        halves = split_document_for_truncation(text)
        self.assertIsNotNone(halves)
        before, after = halves
        self.assertEqual(" ".join(f"{before} {after}".split()), " ".join(text.split()))
        self.assertGreaterEqual(len(before), MIN_TRUNCATION_PART_CHARS)
        self.assertGreaterEqual(len(after), MIN_TRUNCATION_PART_CHARS)

    def test_a_short_document_is_not_split(self):
        self.assertIsNone(split_document_for_truncation("جملهٔ کوتاه درس."))
        self.assertIsNone(split_document_for_truncation(""))

    def test_the_cut_prefers_a_paragraph_boundary(self):
        first = "الف " * 300
        second = "ب " * 300
        halves = split_document_for_truncation(f"{first}\n\n{second}")
        self.assertIsNotNone(halves)
        self.assertEqual(halves[1].strip(), second.strip())

    def test_numerical_medical_content_is_never_corrupted_by_the_cut(self):
        text = ("دوز دارو ۵ میلی‌گرم است. " * 60) + ("مقدار آزمایشگاهی 7.2 است. " * 60)
        halves = split_document_for_truncation(text)
        self.assertIsNotNone(halves)
        joined = " ".join(f"{halves[0]} {halves[1]}".split())
        self.assertEqual(joined, " ".join(text.split()))
        self.assertIn("7.2", joined)
        self.assertIn("۵ میلی‌گرم", joined)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
