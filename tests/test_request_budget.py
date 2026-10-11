"""Offline request preflight, source splitting, and quota separation."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from support import fake_session_factory, make_settings, sample_notes_json
from test_ai_platform import PlatformCase, _Response, _ScriptedSession, _notes_payload

from gamas_bot.ai.adapters import NoteRequest
from gamas_bot.ai.models import ModelInfo
from gamas_bot.ai.request_budget import UNKNOWN_CONTEXT_TOKENS, check_request_budget
from gamas_bot.ai.routing import job_session_scope
from gamas_bot.structuring import (
    COMPILE_PROMPT, OUTLINE_PROMPT, NoteInputTooLarge, ProviderHTTPError,
    TRANSCRIPT_PROMPT, _structure_chunk,
    _repair_notes_if_needed, _structure_chunk_once, _notes_for_part,
    merge_structured_notes, parse_structured_notes, structure_presentation, structure_transcript,
)
from gamas_bot.qa import run_note_qa


def request(text: str, *, schema: bool = False) -> NoteRequest:
    body = {"model": "x", "messages": [{"role": "system", "content": "S"},
                                        {"role": "user", "content": text}]}
    if schema:
        body["response_format"] = {"json_schema": {"schema": {"description": "توضیح " * 300}}}
    return NoteRequest(method="POST", url="https://example.test/chat/completions", headers={}, json_body=body)


class BudgetMathTests(unittest.TestCase):
    def test_counts_persian_latin_and_final_schema_not_just_source_chars(self):
        model = ModelInfo(provider="openai_compatible", model_id="x", context_window=1300, max_output_tokens=256)
        text = "تعریف HbA1c: 6.5% و ۲۰ mg/dL " * 24
        plain = check_request_budget(request(text), model, output_tokens=256)
        schema = check_request_budget(request(text, schema=True), model, output_tokens=256)
        self.assertGreater(schema.input_tokens, plain.input_tokens)
        self.assertTrue(plain.fits)
        self.assertFalse(schema.fits)
        self.assertEqual(schema.reason, "input_exceeds_context")

    def test_unknown_window_is_finite_and_input_only_does_not_consume_output_window(self):
        unknown = check_request_budget(
            request("تعریف " * 10000), ModelInfo(provider="gateway", model_id="x"), output_tokens=256
        )
        self.assertEqual(unknown.context_limit, UNKNOWN_CONTEXT_TOKENS)
        self.assertFalse(unknown.fits)
        known = ModelInfo(provider="gemini", model_id="x", context_window=500, max_output_tokens=400)
        combined = check_request_budget(request("سلام"), known, output_tokens=400)
        separate = check_request_budget(request("سلام"), known, output_tokens=400, input_only_window=True)
        self.assertFalse(combined.fits)
        self.assertTrue(separate.fits)
        self.assertEqual(check_request_budget(request("x"), known, output_tokens=401).reason, "output_limit")


class RoutedPreflightTests(PlatformCase):
    async def _nara(self, window=140):
        self.settings = replace(self.settings, note_api_max_output_tokens=128)
        self.router.settings = self.settings
        self.manager.settings = self.settings
        self.models.settings = self.settings
        await self.db.ai_routes_replace("notes", "chunk_structuring", [
            {"provider": "nara", "enabled": 1, "free_only": 1},
        ], admin_id=None)
        key_id = await self.manager.add_credential(
            service="notes", provider="nara", label="free-test", secret="sk-test-notes-1234",
            model="agnes-2.5-flash", admin_id=1,
        )
        await self.manager.set_billing_attestation(key_id, "free", 1)
        await self.models.apply_discovery("nara", [ModelInfo(
            provider="nara", model_id="agnes-2.5-flash", free_status="free_plan",
            context_window=window, max_output_tokens=128,
        )])
        return await self._session_plan()

    async def test_local_rejection_sends_no_http_and_does_not_reserve_quota(self):
        job = await self._nara()
        session = _ScriptedSession()
        with job_session_scope(job):
            with self.assertRaises(NoteInputTooLarge):
                await _structure_chunk("SOURCE_MARKER_1234", self.settings, session, system_prompt="S")
        self.assertEqual(session.requests, [])
        self.assertFalse(self.router._budget_reservations)
        self.assertFalse(await self.db.ai_usage_recent(limit=5))
        events = await self.db.ai_events_list(limit=20)
        self.assertNotIn("SOURCE_MARKER_1234", json.dumps(events))
        self.assertNotIn("sk-test-notes-1234", json.dumps(events))

    async def test_smaller_primary_fails_preflight_then_larger_fallback_answers(self):
        await self._nara(window=140)
        await self.db.ai_routes_replace("notes", "chunk_structuring", [
            {"provider": "nara", "enabled": 1, "free_only": 1},
            {"provider": "openrouter", "enabled": 1, "free_only": 1},
        ], admin_id=None)
        key_id = await self.manager.add_credential(
            service="notes", provider="openrouter", label="fallback", secret="sk-or-free-12345",
            model="example/lecture:free", admin_id=1,
        )
        await self.manager.set_billing_attestation(key_id, "free", 1)
        await self.models.apply_discovery("openrouter", [ModelInfo(
            provider="openrouter", model_id="example/lecture:free", free_status="free_plan",
            context_window=5000, max_output_tokens=128,
        )])
        session = _ScriptedSession()
        session.add("openrouter.ai", _Response(200, _notes_payload(sample_notes_json())))
        with job_session_scope(await self._session_plan()):
            result = await _structure_chunk("توضیح HbA1c 6.5%", self.settings, session, system_prompt="S")
        self.assertEqual(result, sample_notes_json())
        self.assertEqual(len(session.requests), 1)
        self.assertIn("openrouter.ai", session.requests[0]["url"])

    async def test_smaller_fallback_after_primary_502_can_split_without_confusing_quota(self):
        await self._nara(window=5000)
        self.settings = replace(self.settings, note_api_retries=0)
        self.router.settings = self.settings
        await self.db.ai_routes_replace("notes", "chunk_structuring", [
            {"provider": "nara", "enabled": 1, "free_only": 1},
            {"provider": "openrouter", "enabled": 1, "free_only": 1},
        ], admin_id=None)
        key_id = await self.manager.add_credential(
            service="notes", provider="openrouter", label="small", secret="sk-or-free-12345",
            model="example/lecture:free", admin_id=1,
        )
        await self.manager.set_billing_attestation(key_id, "free", 1)
        await self.models.apply_discovery("openrouter", [ModelInfo(
            provider="openrouter", model_id="example/lecture:free", free_status="free_plan",
            context_window=140, max_output_tokens=128,
        )])
        session = _ScriptedSession()
        session.add("router.bynara.id", _Response(502, {"error": {"message": "offline"}}))
        with job_session_scope(await self._session_plan()):
            with self.assertRaises(NoteInputTooLarge):
                await _structure_chunk("تعریف HbA1c 6.5%", self.settings, session, system_prompt="S")
        self.assertEqual(len(session.requests), 1)
        self.assertIn("router.bynara.id", session.requests[0]["url"])

    async def test_routed_oversize_splits_source_and_posts_only_fitting_parts(self):
        job = await self._nara(window=500)
        source = "تعریف HbA1c برابر 6.5% mg/dL است. " * 45
        session = _ScriptedSession()
        session.add("router.bynara.id", _Response(200, _notes_payload(sample_notes_json())))
        with job_session_scope(job):
            await _notes_for_part(
                source, self.settings, session, "متن خام:\n", system_prompt="S", label="part",
                source=source, input_split_budget=[8], split_budget=[4],
            )
        self.assertGreater(len(session.requests), 1)
        pieces = [item["json"]["messages"][-1]["content"].removeprefix("متن خام:\n")
                  for item in session.requests]
        self.assertEqual(" ".join(" ".join(pieces).split()), " ".join(source.split()))
        self.assertTrue(all(len(piece) < len(source) for piece in pieces))

    async def test_legacy_preflight_before_http(self):
        settings = make_settings(note_api_provider="openai_compatible", note_api_model="unknown-gateway-model",
                                 note_api_max_output_tokens=128, note_api_retries=0)
        session = _ScriptedSession()
        with patch("gamas_bot.ai.request_budget.UNKNOWN_CONTEXT_TOKENS", 140):
            with self.assertRaises(NoteInputTooLarge):
                await _structure_chunk_once("SOURCE_MARKER_1234", settings, session, system_prompt="S")
        self.assertEqual(session.requests, [])


class SourceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_split_is_lossless_and_bounded(self):
        source = "ALFA " + ("تعریف HbA1c برابر 6.5% و واحد mg/dL است. " * 38) + "BRAVO"
        accepted = []
        attempts = []

        async def provider(document, *_args, **_kwargs):
            attempts.append(document)
            if len(document) > 850:
                raise NoteInputTooLarge()
            accepted.append(document)
            return json.dumps({"title": "جزوه", "sections": [
                {"heading": "تعریف", "paragraphs": [document]}
            ]}, ensure_ascii=False)

        with patch("gamas_bot.structuring._structure_chunk", side_effect=provider), patch(
            "gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory
        ):
            settings = make_settings(note_repair_enabled=False, note_global_context_enabled=False)
            notes = await structure_transcript(source, settings)
        self.assertGreater(len(attempts), len(accepted))
        self.assertLessEqual(len(accepted), 9)  # at most MAX_INPUT_SPLITS + 1
        self.assertEqual(" ".join(" ".join(accepted).split()), " ".join(source.split()))
        self.assertIn("ALFA", accepted[0])
        self.assertIn("BRAVO", accepted[-1])
        report = run_note_qa(notes, [source])
        self.assertEqual(report.missing_numbers, ())
        self.assertEqual(report.missing_terms, ())

    async def test_many_chunks_each_receive_their_own_bounded_input_allowance(self):
        source = "\n\n".join(
            f"بخش {index}: " + ("توضیح علمی HbA1c و مثال در کلاس. " * 22)
            for index in range(10)
        )
        accepted = []

        async def provider(document, *_args, **_kwargs):
            if len(document) > 650:
                raise NoteInputTooLarge()
            accepted.append(document)
            return sample_notes_json()

        with patch("gamas_bot.structuring._structure_chunk", side_effect=provider), patch(
            "gamas_bot.structuring.TRANSCRIPT_CHUNK_CHARS", 1200
        ), patch("gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory):
            await structure_transcript(source, make_settings(
                note_global_context_enabled=False, note_repair_enabled=False,
            ))
        self.assertGreater(len(accepted), 8)
        pieces = [item.split("]\n\n", 1)[-1] for item in accepted]
        self.assertEqual(" ".join(" ".join(pieces).split()), " ".join(source.split()))

    async def test_qa_diagnostics_do_not_echo_source_signals(self):
        sparse = parse_structured_notes(sample_notes_json())
        source = "تعریف HbA1c همراه با 777 mg/dL در کلاس بررسی شد. " * 30
        with self.assertLogs("gamas_bot.qa", level="WARNING") as captured:
            report = run_note_qa(sparse, [source])
        self.assertTrue(report.missing_numbers)
        self.assertNotIn("777", " ".join(captured.output))
        self.assertNotIn("mg/dL", " ".join(captured.output))

    async def test_429_never_splits_and_irreducible_input_terminates(self):
        calls = []

        async def fail(document, *_args, **_kwargs):
            calls.append(document)
            raise ProviderHTTPError("quota", status=429)

        with patch("gamas_bot.structuring._structure_chunk", side_effect=fail):
            with self.assertRaises(ProviderHTTPError):
                await _notes_for_part("الف " * 200, make_settings(), object(), TRANSCRIPT_PROMPT,
                                      system_prompt="S", label="test", input_split_budget=[8])
        self.assertEqual(len(calls), 1)
        with patch("gamas_bot.structuring._structure_chunk", side_effect=NoteInputTooLarge()):
            with self.assertRaises(NoteInputTooLarge):
                await _notes_for_part("الف", make_settings(), object(), TRANSCRIPT_PROMPT,
                                      system_prompt="S", label="test", input_split_budget=[8])

    async def test_optional_outline_and_compile_keep_all_first_pass_notes(self):
        text = ("ALFA تعریف HbA1c 6.5% mg/dL است. " * 16) + "\n\n" + (
            "BRAVO مراحل مثال و مقایسه در درس توضیح داده شده‌اند. " * 16
        )
        calls = []

        async def provider(document, _settings, _session, prompt=TRANSCRIPT_PROMPT, **_kwargs):
            calls.append(prompt)
            if prompt in {OUTLINE_PROMPT, COMPILE_PROMPT}:
                raise NoteInputTooLarge()
            return json.dumps({"title": "جزوه", "sections": [
                {"heading": "درس", "paragraphs": [document]}
            ]}, ensure_ascii=False)

        with patch("gamas_bot.structuring._structure_chunk", side_effect=provider), patch(
            "gamas_bot.structuring.TRANSCRIPT_CHUNK_CHARS", 700
        ), patch("gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory):
            notes = await structure_transcript(text, make_settings(note_repair_enabled=False))
        self.assertIn(OUTLINE_PROMPT, calls)
        self.assertIn(COMPILE_PROMPT, calls)
        joined = " ".join(" ".join(p for section in notes.sections for p in section.paragraphs).split())
        self.assertIn("ALFA", joined)
        self.assertIn("BRAVO", joined)
        self.assertIn("6.5%", joined)

    async def test_repair_uses_each_chunks_draft_not_the_entire_booklet(self):
        sources = [
            "تعریف HbA1c و عدد 6.5% با واحد mg/dL توضیح داده شد. " * 18,
            "مقایسه دوز 250 mg و مقدار 120 mmHg در این مثال آمده است. " * 18,
        ]
        markers = ("جزوهپیشینالف", "جزوهپیشینب")
        originals = [parse_structured_notes(json.dumps({
            "title": "درس", "sections": [{"heading": "بخش", "paragraphs": [marker]}],
        }, ensure_ascii=False)) for marker in markers]
        calls = []

        async def provider(document, *_args, **_kwargs):
            calls.append(document)
            return originals[len(calls) - 1]

        with patch("gamas_bot.structuring._structured_notes_for", side_effect=provider):
            await _repair_notes_if_needed(
                merge_structured_notes(originals), sources, make_settings(), object(),
                note_mode="full", system_prompt="S", prompt=TRANSCRIPT_PROMPT,
                label="test", originals=originals,
            )
        self.assertEqual(len(calls), 2)
        self.assertIn(markers[0], calls[0])
        self.assertNotIn(markers[1], calls[0])
        self.assertIn(markers[1], calls[1])
        self.assertNotIn(markers[0], calls[1])

    async def test_presentation_split_repeats_outline_without_splitting_it(self):
        outline = "### اسلاید ۱ — تعریف HbA1c"
        transcript = "تعریف HbA1c برابر 6.5% و واحد mg/dL است. " * 43
        accepted = []

        async def provider(document, *_args, **_kwargs):
            if len(document) > 1050:
                raise NoteInputTooLarge()
            accepted.append(document)
            return sample_notes_json("جزوه")

        with patch("gamas_bot.structuring._structure_chunk", side_effect=provider), patch(
            "gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory
        ):
            await structure_presentation(outline, transcript, make_settings(
                note_repair_enabled=False, note_global_context_enabled=False,
            ))
        self.assertGreater(len(accepted), 1)
        self.assertTrue(all(item.count(outline) == 1 for item in accepted))
        pieces = [item.split("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n", 1)[-1] for item in accepted]
        self.assertEqual(" ".join(" ".join(pieces).split()), " ".join(transcript.split()))
