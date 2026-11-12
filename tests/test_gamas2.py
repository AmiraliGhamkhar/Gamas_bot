"""Gamas 2.0 acceptance tests: credits, diarization, media, artifacts, Studio.

These cover the new invariants rather than re-testing the existing pipeline:
exact integer credit arithmetic, honest speaker turns, capability-aware STT
routing, mandatory adaptive preprocessing with stereo preservation, the
central submission artifact registry, the Mini App's new screens, and the
offline DER/JER/WER/CER evaluator.
"""

from __future__ import annotations

import array
import io
import json
import math
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import av

from gamas_bot import billing, config
from gamas_bot.artifacts import (
    KIND_BOOKLET,
    KIND_DOCX,
    KIND_FLASHCARDS,
    KIND_KEYPOINTS,
    KIND_QUIZ,
    KIND_TRANSCRIPT,
    KIND_TXT,
    callback_for,
    delivery_markup,
    parse_callback,
)
from gamas_bot.db import Database
from gamas_bot.diarization_eval import (
    character_error_rate,
    evaluate,
    optimal_label_map,
    score_diarization,
    word_error_rate,
)
from gamas_bot.media import (
    MediaInfo,
    build_extract_command,
    build_merge_command,
    choose_merge_format,
    parse_probe_output,
)
from gamas_bot.media_worker import (
    STT_PREPROCESS_FILTERS,
    _transcode,
    adaptive_filters,
)
from gamas_bot.mini_app import (
    MiniAppData,
    create_app,
    booklet_docx_bytes,
    leaderboard_payload,
    resolve_leaderboard_window,
)
from gamas_bot.notes import StructuredNotes
from gamas_bot.stt import PROVIDERS, order_specs_for_diarization
from gamas_bot.transcript import (
    Paragraph,
    RawTranscript,
    SpeakerTurn,
    TranscriptDocument,
    Word,
    build_document,
    build_speaker_turns,
    mark_overlaps,
    overlap_spans,
    resolve_speakers,
    speaker_name_from_label,
)
from tests.support import make_settings
from tests.test_mini_app import BOT_TOKEN, NOW, credentials

RATE = 16_000


# ---------------------------------------------------------------- credits ----

class CreditArithmeticTests(unittest.TestCase):
    def test_the_unit_is_exact_and_integer(self):
        self.assertEqual(billing.seconds_to_credits(10_800), 100_000)
        self.assertEqual(billing.credits_to_seconds(100_000), 10_800)
        self.assertEqual(billing.seconds_to_credits(1), 10)
        self.assertEqual(billing.seconds_to_credits(10_801), 100_010)
        self.assertEqual(billing.credits_per_hour(3), 100_000)

    def test_no_floating_point_drift_at_the_boundary(self):
        # 100 000 * (10 800 / 100 000) must be exactly 10 800 in integer math.
        self.assertEqual(Fraction(100_000, 10_800) * 10_800, 100_000)
        for seconds in (0, 1, 2, 3, 10, 3599, 3600, 10_799, 10_800):
            with self.subTest(seconds=seconds):
                credits = billing.seconds_to_credits(seconds)
                self.assertIsInstance(credits, int)
                self.assertLessEqual(billing.credits_to_seconds(credits), seconds)

    def test_bad_values_degrade_to_zero(self):
        for value in (None, -5, float("nan"), float("inf"), True, "bad"):
            with self.subTest(value=value):
                self.assertEqual(billing.seconds_to_credits(value), 0)  # type: ignore[arg-type]
                self.assertEqual(billing.credits_to_seconds(value), 0)  # type: ignore[arg-type]

    def test_price_preview_never_changes_the_catalogue(self):
        before = [(p.code, p.price_toman) for p in billing.plan_catalog()]
        preview = billing.price_change_preview(65)
        self.assertEqual(preview["live_price_toman_per_package"], 65_000)
        self.assertEqual(preview["proposed_price_toman_per_package"], 65)
        self.assertTrue(preview["major_discrepancy"])
        self.assertFalse(preview["activated"])
        self.assertEqual(
            [(p.code, p.price_toman) for p in billing.plan_catalog()], before
        )

    def test_confirmed_default_is_active_and_literal_65_is_diagnosed(self):
        settings = make_settings(credit_price_per_100k_toman=65)
        self.assertTrue(make_settings().v2_tariff_activated)
        messages = config.check(settings)
        self.assertTrue(any("CREDIT_PRICE_PER_100K_TOMAN=65" in item for item in messages))
        # The confirmed 65,000-Toman default must not be treated as a
        # discrepancy or an unactivated preview tariff.
        self.assertFalse(
            any("CREDIT_PRICE_PER_100K_TOMAN" in item for item in config.check(make_settings()))
        )


# ------------------------------------------------------------ speaker turns --

class SpeakerTurnTests(unittest.TestCase):
    @staticmethod
    def words():
        return [
            Word("سلام", 0.0, 0.5, "S1"),
            Word("دوستان", 0.6, 1.0, "S1"),
            Word("بله", 1.1, 1.5, "S2"),
            Word("استاد", 1.6, 2.0, "S2"),
        ]

    def test_word_confidence_overlap_and_stable_id(self):
        word = Word("سلام", 0.0, 0.5, "0", 0.91, True)
        self.assertEqual(word.confidence, 0.91)
        self.assertTrue(word.overlaps)
        self.assertEqual(word.speaker_id, "speaker_0")
        self.assertEqual(Word("x", None, None, "").speaker_id, "speaker_unknown")

    def test_custom_names_win_and_unknown_stays_unknown(self):
        self.assertEqual(speaker_name_from_label("0", {"0": "استاد"}), "استاد")
        self.assertEqual(speaker_name_from_label("0"), "گویندهٔ ۱")
        self.assertIsNone(speaker_name_from_label(None))
        self.assertIsNone(speaker_name_from_label("UU"))

    def test_turns_are_chronological_and_keep_confidences(self):
        words, count = resolve_speakers(self.words())
        self.assertEqual(count, 1)  # tiny clusters are noise, not speakers
        long_words = [
            Word(f"w{i}", i * 0.1, i * 0.1 + 0.09, "0", 0.8)
            for i in range(20)
        ] + [
            Word(f"v{i}", 2.0 + i * 0.1, 2.0 + i * 0.1 + 0.09, "1", 0.6)
            for i in range(20)
        ]
        turns = build_speaker_turns(long_words, custom_names={"0": "استاد"})
        self.assertEqual([turn.speaker_label for turn in turns], ["0", "1"])
        self.assertEqual(turns[0].display_name, "استاد")
        self.assertAlmostEqual(float(turns[0].confidence or 0), 0.8)
        self.assertEqual(turns[0].speaker_id, "speaker_0")

    def test_json_roundtrip_and_corruption_is_safe(self):
        document = TranscriptDocument(
            (
                Paragraph("سلام", 0.0, 1.0, "0"),
                Paragraph("بله", 1.0, 2.0, "1"),
            ),
            speakers=2,
            speaker_names=(("0", "استاد"),),
        )
        parsed = TranscriptDocument.turns_from_json(document.turns_to_json())
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].display_name, "استاد")
        self.assertEqual(parsed[0].speaker_label, "0")
        self.assertEqual(TranscriptDocument.turns_from_json("{broken"), [])
        self.assertEqual(TranscriptDocument.turns_from_json(None), [])

    def test_overlap_detection_is_bounded_and_flagged(self):
        words = [
            Word("a", 0.0, 1.0, "0"),
            Word("b", 0.5, 1.5, "1"),
            Word("c", 2.0, 2.5, "0"),
        ]
        marked = mark_overlaps(words)
        self.assertTrue(marked[0].overlaps)
        self.assertTrue(marked[1].overlaps)
        self.assertFalse(marked[2].overlaps)
        self.assertEqual(overlap_spans(words), [(0.5, 1.0)])

    def test_build_document_records_overlap_spans(self):
        raw = RawTranscript(
            words=[
                Word("a", 0.0, 1.0, "0"),
                Word("b", 0.5, 1.5, "1"),
            ],
            text="a b",
            engine="speechmatics",
            speakers=2,
        )
        document = build_document(raw)
        self.assertEqual(document.overlap_count, 1)
        self.assertTrue(document.diarization_attempted)

    def test_unknown_attribution_is_explicit_in_turns(self):
        turn = SpeakerTurn("speaker_unknown", 0.0, 1.0, "…")
        self.assertIsNone(turn.speaker_label)
        self.assertIsNone(turn.display_name)


# --------------------------------------------------------------- STT routing --

class SttCapabilityTests(unittest.TestCase):
    def test_diarizing_providers_are_preferred_without_waiting(self):
        specs = [
            PROVIDERS["openai_compatible"],
            PROVIDERS["deepgram"],
            PROVIDERS["speechmatics"],
        ]
        ordered = order_specs_for_diarization(specs, diarization_enabled=True)
        self.assertEqual([s.slug for s in ordered], ["deepgram", "speechmatics", "openai_compatible"])
        self.assertEqual(
            [s.slug for s in order_specs_for_diarization(specs, diarization_enabled=False)],
            ["openai_compatible", "deepgram", "speechmatics"],
        )

    def test_no_diarizing_provider_keeps_the_order(self):
        specs = [PROVIDERS["openai_compatible"]]
        self.assertEqual(
            order_specs_for_diarization(specs, diarization_enabled=True), specs
        )

    def test_capability_flags_are_documented(self):
        self.assertTrue(PROVIDERS["speechmatics"].supports_diarization)
        self.assertTrue(PROVIDERS["deepgram"].supports_diarization)
        self.assertFalse(PROVIDERS["openai_compatible"].supports_diarization)


# ------------------------------------------------------------------- media ---

def _tone(path: Path, seconds: float = 1.0, *, stereo: bool = False) -> None:
    layout = "stereo" if stereo else "mono"
    total = int(RATE * seconds)
    with av.open(str(path), "w", format="wav") as container:
        stream = container.add_stream("pcm_s16le", rate=RATE)
        stream.layout = layout
        frame = av.AudioFrame(format="s16", layout=layout, samples=total)
        frame.sample_rate = RATE
        samples = array.array(
            "h",
            [
                int(3000 * math.sin(2 * math.pi * 200 * i / RATE))
                for i in range(total)
            ],
        )
        if stereo:
            frame.planes[0].update(samples.tobytes() + samples.tobytes())
        else:
            frame.planes[0].update(samples.tobytes())
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


class AdaptivePreprocessTests(unittest.TestCase):
    def test_commands_carry_the_new_flags_before_the_separator(self):
        command = build_extract_command(
            Path("a.mp3"), Path("b.wav"),
            preprocess=True, adaptive=True, stereo=True,
        )
        for flag in ("--preprocess", "--adaptive", "--stereo"):
            self.assertIn(flag, command)
            self.assertLess(command.index(flag), command.index("--"))
        merge = build_merge_command(
            [Path("a.wav")], Path("m.wav"),
            preprocess=True, adaptive=True, stereo=True,
        )
        self.assertIn("--stereo", merge)

    def test_stereo_doubles_the_wav_budget(self):
        settings = make_settings()
        mono = choose_merge_format(
            3600.0, settings, max_output_bytes=10**12
        )
        # With a budget that only fits a mono WAV, stereo must fall back to Opus.
        budget = 3600.0 * 32_000 * 1.5
        forced = choose_merge_format(
            3600.0, settings, max_output_bytes=int(budget), stereo=True
        )
        self.assertEqual(mono, "wav")
        self.assertEqual(forced, "opus")

    def test_probe_reports_channels(self):
        info = parse_probe_output(
            json.dumps({"has_audio": True, "duration": 1.0,
                        "audio_codec": "pcm_s16le", "channels": 2})
        )
        self.assertIsInstance(info, MediaInfo)
        self.assertEqual(info.channels, 2)

    def test_adaptive_chain_is_a_subset_of_the_documented_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "a.wav"
            _tone(source)
            chain = adaptive_filters([source])
            self.assertEqual(chain[0], STT_PREPROCESS_FILTERS[0])
            self.assertEqual(chain[-1], STT_PREPROCESS_FILTERS[-1])
            self.assertTrue(set(chain) <= set(STT_PREPROCESS_FILTERS))

    def test_stereo_transcode_preserves_two_channels_and_sample_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "in.wav"
            output = root / "out.wav"
            _tone(source, stereo=True)
            summary = _transcode(
                [source], output, rate=RATE, codec="pcm_s16le",
                filters=STT_PREPROCESS_FILTERS, layout="stereo",
            )
            self.assertEqual(summary["layout"], "stereo")
            self.assertEqual(summary["source_samples"], summary["output_samples"])
            with av.open(str(output)) as container:
                self.assertEqual(container.streams.audio[0].channels, 2)


# --------------------------------------------------------------- evaluator ---

class EvaluationTests(unittest.TestCase):
    def test_wer_and_cer(self):
        self.assertEqual(word_error_rate("سلام دوستان", "سلام دوستان"), 0.0)
        self.assertGreater(word_error_rate("سلام دوستان امروز", "سلام دوستان"), 0.0)
        self.assertEqual(character_error_rate("abc", "abc"), 0.0)
        self.assertGreater(character_error_rate("abc", "abd"), 0.0)

    def test_label_alignment_and_der(self):
        ref = [
            {"speaker_label": "A", "start": 0.0, "end": 3.0},
            {"speaker_label": "B", "start": 3.0, "end": 5.0},
        ]
        hyp = [
            {"speaker_label": "h1", "start": 0.0, "end": 3.0},
            {"speaker_label": "h0", "start": 3.0, "end": 5.0},
        ]
        mapping = optimal_label_map(ref, hyp)
        self.assertEqual(mapping, {"h1": "A", "h0": "B"})
        scores = score_diarization(ref, hyp)
        self.assertAlmostEqual(scores.der, 0.0)
        self.assertAlmostEqual(scores.jer, 0.0)

    def test_a_missed_speaker_is_an_error_not_a_guess(self):
        ref = [{"speaker_label": "A", "start": 0.0, "end": 4.0}]
        hyp = [{"speaker_label": "h0", "start": 0.0, "end": 2.0}]
        scores = score_diarization(ref, hyp)
        self.assertGreater(scores.der, 0.0)
        self.assertGreater(scores.missed_frames, 0)

    def test_evaluate_reports_none_instead_of_fabricating_metrics(self):
        result = evaluate({"turns": []}, {"turns": [], "text": "x"})
        self.assertIsNone(result["diarization"])
        self.assertIsNone(result["wer"])
        self.assertIsNone(result["cer"])


# ------------------------------------------------------- delivery / markup ---

class DeliveryMarkupTests(unittest.TestCase):
    def _payloads(self, buttons):
        from tests.test_bot import _button_data

        values = []
        for row in buttons:
            for button in row:
                data = _button_data(button)
                values.append(data.decode() if isinstance(data, bytes) else str(button.text))
        return values

    def test_every_artifact_has_an_independent_button(self):
        buttons = delivery_markup(
            reference="GMS-7", speakers=2, has_booklet=True,
            kinds={KIND_BOOKLET, KIND_DOCX, KIND_TRANSCRIPT, KIND_TXT,
                   KIND_KEYPOINTS, KIND_QUIZ, KIND_FLASHCARDS},
            item_id=11, mini_app_url="https://example.com/app",
        )
        payloads = self._payloads(buttons)
        for kind in ("booklet", "docx", "transcript", "txt", "keypoints"):
            self.assertIn(f"art:GMS-7:{kind}", payloads)
        self.assertIn("quiz:new:11", payloads)
        self.assertIn("fc:ai:11", payloads)
        self.assertTrue(any("ویرایش" in str(p) for p in payloads))

    def test_learning_buttons_are_hidden_when_generation_is_not_configured(self):
        buttons = delivery_markup(
            reference="GMS-7",
            speakers=0,
            has_booklet=True,
            kinds={KIND_BOOKLET, KIND_QUIZ, KIND_FLASHCARDS},
            item_id=11,
            learning_available=False,
        )
        payloads = self._payloads(buttons)
        self.assertNotIn("quiz:new:11", payloads)
        self.assertNotIn("fc:ai:11", payloads)

    def test_no_dead_buttons_when_an_artifact_is_missing(self):
        buttons = delivery_markup(
            reference="GMS-7", speakers=1, has_booklet=False,
            kinds={KIND_TRANSCRIPT, KIND_TXT},
        )
        payloads = self._payloads(buttons)
        self.assertNotIn("art:GMS-7:booklet", payloads)
        self.assertFalse(any("quiz" in p for p in payloads))
        self.assertEqual(parse_callback("art:GMS-7:txt"), ("GMS-7", "txt"))
        self.assertEqual(callback_for("GMS-7", "txt"), b"art:GMS-7:txt")

    def test_delivery_screen_mentions_edit_and_learning(self):
        from gamas_bot import texts

        body = texts.delivery_screen(
            reference="GMS-7", speakers=2, has_booklet=True, has_keypoints=True
        )
        self.assertIn("ویرایش", body)
        self.assertIn("آزمون", body)

    def test_delivery_screen_explains_unavailable_learning_without_offering_it(self):
        from gamas_bot import texts

        body = texts.delivery_screen(
            reference="GMS-7",
            speakers=0,
            has_booklet=True,
            learning_available=False,
        )
        self.assertNotIn(texts.DELIVERY_ROW_LEARNING, body)
        self.assertIn(texts.LEARNING_GENERATION_UNAVAILABLE, body)

    def test_booklet_appendix_is_source_derived(self):
        notes = StructuredNotes(
            title="جزوه", summary="خلاصه",
            speakers=("استاد — ۱۲۰ واژه",),
            timeline=("[۰۰:۳۰] مقدمه",),
        )
        payload = notes.to_payload()
        self.assertEqual(payload["speakers"], ["استاد — ۱۲۰ واژه"])
        self.assertEqual(payload["timeline"], ["[۰۰:۳۰] مقدمه"])
        restored = StructuredNotes.from_payload(payload)
        self.assertEqual(restored.speakers, notes.speakers)
        self.assertIn("گویندگان", notes.to_markdown())


# ------------------------------------------------------------- Mini App 2.0 --

class MiniAppV2Tests(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple] = []
        self.data = MiniAppData(
            leaderboard=lambda limit: [{"first_name": "الف", "quiz_points": 90}][:limit],
            rank=lambda _u: {"rank": 1, "points": 90},
            booklets=lambda _u, limit: [{"id": 1, "title": "جزوه", "editable": True}],
            booklet=lambda _u, item_id: (
                {"id": item_id, "title": "جزوهٔ من", "content_text": "# سرفصل\n\nمتن جزوه", "editable": True}
                if item_id == 1 else None
            ),
            save_booklet=lambda *_a: True,
            profile=lambda _u: {
                "first_name": "نگار", "credits": 100_000, "unlimited": False,
                "quiz_points": 40, "library_count": 1, "submission_count": 2,
                "artifact_count": 5,
            },
            leaderboard_window=lambda limit, window: (
                [{"first_name": "هفتگی", "quiz_points": 20}][:limit] if window == "weekly" else []
            ),
            rank_window=lambda _u, window: {"rank": 2, "points": 20},
            revisions=lambda _u, _i, limit: [
                {"id": 3, "created_at": "2026-11-10T10:00:00", "chars": 12, "source": "mini_app"}
            ][:limit],
            restore_revision=lambda _u, _i, revision_id: revision_id == 3,
            speaker_turns=lambda _u, _s: [
                {"speaker_id": "speaker_0", "speaker_label": "0",
                 "display_name": "گویندهٔ ۱", "start": 0.0, "end": 2.0,
                 "text": "سلام", "overlaps": False}
            ],
            rename_speaker=lambda _u, _s, label, name: bool(label and name),
        )
        self.app = create_app(data=self.data, bot_token=BOT_TOKEN, now=lambda: NOW)

    def _request(self, path, body, method="POST"):
        raw = json.dumps(body).encode()
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        chunks = self.app(
            {
                "PATH_INFO": path, "REQUEST_METHOD": method,
                "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
            },
            start_response,
        )
        return captured, b"".join(chunks)

    def test_home_shows_only_the_callers_own_summary(self):
        captured, body = self._request("/api/home", {"initData": credentials()})
        payload = json.loads(body)
        self.assertIn("200", captured["status"])
        self.assertEqual(payload["credits"], 100_000)
        self.assertEqual(payload["tabs"], ["home", "leaderboard", "studio"])
        self.assertNotIn("password", payload)
        cancelled, _ = self._request("/api/home", {"initData": ""})
        self.assertIn("401", cancelled["status"])

    def test_weekly_leaderboard_uses_the_window(self):
        captured, body = self._request(
            "/api/leaderboard", {"initData": credentials(), "window": "weekly"}
        )
        payload = json.loads(body)
        self.assertEqual(payload["window"], "weekly")
        self.assertEqual(payload["rows"][0]["name"], "هفتگی")
        self.assertEqual(resolve_leaderboard_window("nonsense"), "all")
        self.assertEqual(leaderboard_payload(self.data, 42, window="all")["rows"][0]["name"], "الف")

    def test_revision_history_and_restore_are_authenticated(self):
        captured, body = self._request(
            "/api/revisions", {"initData": credentials(), "id": 1}
        )
        self.assertEqual(json.loads(body)["items"][0]["id"], 3)
        captured, _ = self._request(
            "/api/revision", {"initData": "", "id": 1, "revision_id": 3}, "PUT"
        )
        self.assertIn("401", captured["status"])
        captured, body = self._request(
            "/api/revision", {"initData": credentials(), "id": 1, "revision_id": 3}, "PUT"
        )
        self.assertTrue(json.loads(body)["ok"])

    def test_docx_export_is_a_real_word_file_for_the_owner_only(self):
        captured, body = self._request(
            "/api/booklet/export", {"initData": credentials(), "id": 1}
        )
        self.assertIn("200", captured["status"])
        self.assertTrue(body.startswith(b"PK"))
        self.assertIn("wordprocessingml.document", captured["headers"]["Content-Type"])
        self.assertIn("attachment", captured["headers"]["Content-Disposition"])
        denied, _ = self._request(
            "/api/booklet/export", {"initData": "", "id": 1}
        )
        self.assertIn("401", denied["status"])
        missing, _ = self._request(
            "/api/booklet/export", {"initData": credentials(), "id": 999}
        )
        self.assertIn("404", missing["status"])

    def test_docx_bytes_helper_is_pure(self):
        payload = booklet_docx_bytes(self.data, 42, 1)
        self.assertTrue(payload.startswith(b"PK"))

    def test_speaker_names_are_explicit_and_owner_scoped(self):
        captured, body = self._request(
            "/api/speakers", {"initData": credentials(), "submission_id": 12}
        )
        payload = json.loads(body)
        self.assertIn("200", captured["status"])
        self.assertEqual(payload["labels"], ["0"])
        self.assertEqual(payload["turns"][0]["display_name"], "گویندهٔ ۱")
        self.assertFalse(payload["unknown_attribution"])
        captured, body = self._request(
            "/api/speakers",
            {"initData": credentials(), "submission_id": 12, "label": "0", "name": "استاد"},
            "PUT",
        )
        self.assertEqual(json.loads(body)["name"], "استاد")
        denied, _ = self._request("/api/speakers", {"initData": "", "submission_id": 12})
        self.assertIn("401", denied["status"])


# --------------------------------------------------------- DB / migration ---

class CentralSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "g2.sqlite3")
        await self.db.connect()
        await self.db.ensure_user(42)

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_migration_026_tables_exist(self):
        rows = await self.db.db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {str(row[0]) for row in rows}
        self.assertTrue(
            {"submission_artifacts", "submission_binary_artifacts",
             "booklet_revisions", "quiz_point_events", "speaker_names",
             "diarization_reference_turns"} <= names
        )

    async def test_independent_artifacts_and_speaker_names(self):
        submission = await self.db.create_submission(42, "audio", "a.mp3")
        await self.db.save_transcription(
            submission, "speechmatics", "سلام",
            json.dumps({"key_points": ["نکته"]}),
            speaker_turns_json='[{"speaker_id":"speaker_0","text":"سلام"}]',
            diarization_attempted=True, overlap_count=2,
        )
        row = await self.db.transcription(submission)
        assert row is not None
        self.assertTrue(row["diarization_attempted"])
        self.assertEqual(row["overlap_count"], 2)
        self.assertIn("speaker_0", str(row["speaker_turns_json"]))

        for kind in ("booklet", "transcript", "txt", "keypoints", "quiz", "flashcards"):
            await self.db.artifact_upsert(submission, 42, kind, reference="GMS-1")
        kinds = await self.db.artifact_kinds(submission)
        self.assertEqual(kinds, {"booklet", "transcript", "txt", "keypoints", "quiz", "flashcards"})
        # A second registration updates in place instead of duplicating a button.
        await self.db.artifact_upsert(submission, 42, "booklet", available=False)
        self.assertNotIn("booklet", await self.db.artifact_kinds(submission))
        self.assertEqual(len(await self.db.artifact_list(submission)), 6)

        await self.db.set_speaker_name(submission, "0", "استاد")
        self.assertEqual(await self.db.speaker_names(submission), {"0": "استاد"})

    async def test_persisted_booklet_bytes_are_owner_scoped_and_idempotent(self):
        submission = await self.db.create_submission(42, "audio", "a.mp3")
        await self.db.binary_artifact_upsert(
            submission,
            42,
            "booklet",
            filename="notes.pdf",
            content_type="application/pdf",
            content=b"%PDF-first",
        )
        await self.db.binary_artifact_upsert(
            submission,
            42,
            "docx",
            filename="notes.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=b"PK-second",
        )
        pdf = await self.db.binary_artifact(submission, 42, "booklet")
        assert pdf is not None
        self.assertEqual(pdf["content"], b"%PDF-first")
        self.assertEqual(await self.db.binary_artifact(submission, 999, "booklet"), None)
        self.assertEqual(
            await self.db.binary_artifact_kinds(submission, 42), {"booklet", "docx"}
        )
        self.assertTrue({"booklet", "docx"} <= await self.db.artifact_kinds(submission))

    async def test_transcript_rows_degrade_gracefully_before_migration_026(self):
        """A bot binary may run against a schema whose 026 is not applied yet."""
        for column in (
            "speaker_turns_json",
            "diarization_attempted",
            "overlap_count",
        ):
            await self.db.db.execute(
                f"ALTER TABLE transcriptions DROP COLUMN {column}"
            )
        await self.db.db.commit()
        submission = await self.db.create_submission(42, "audio", "old.mp3")
        await self.db.save_transcription(
            submission, "deepgram", "متن ساده", "{}",
            speaker_turns_json='[{"text":"متن"}]',
            diarization_attempted=True, overlap_count=3,
        )
        row = await self.db.transcription(submission)
        assert row is not None
        self.assertEqual(row["engine"], "deepgram")
        self.assertEqual(row["text"], "متن ساده")
        self.assertIsNone(row["speaker_turns_json"])
        self.assertFalse(row["diarization_attempted"])
        self.assertEqual(row["overlap_count"], 0)


if __name__ == "__main__":
    unittest.main()
