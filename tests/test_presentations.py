from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gamas_bot.media import (
    MediaToolError,
    build_convert_command,
    build_merge_command,
    build_probe_command,
    choose_merge_format,
    convert_to_pptx,
    merge_output_name,
    parse_probe_output,
)
from gamas_bot.presentations import (
    PresentationError,
    classify_presentation,
    media_kind,
    prepare_audio,
    read_presentation,
    slides_outline,
)
from gamas_bot.structuring import (
    StructuringError,
    build_presentation_document,
    structure_presentation,
)

from support import (
    AUDIO_REL,
    FIXTURES_DIR,
    VIDEO_REL,
    build_deck,
    make_settings,
    sample_notes_json,
    video_bytes,
    wav_bytes,
)


class ClassificationTests(unittest.TestCase):
    def test_extensions_and_mime_types_are_classified(self):
        self.assertEqual(classify_presentation("lecture.pptx", None), "native")
        self.assertEqual(classify_presentation("lecture.PPSX", None), "native")
        self.assertEqual(classify_presentation("old.ppt", None), "legacy")
        self.assertEqual(classify_presentation("deck.pps", None), "legacy")
        self.assertEqual(classify_presentation("deck.odp", None), "unsupported")
        self.assertEqual(classify_presentation("deck.otp", None), "unsupported")
        self.assertEqual(
            classify_presentation(
                None,
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            "native",
        )
        self.assertEqual(classify_presentation(None, "application/vnd.ms-powerpoint"), "legacy")
        self.assertEqual(
            classify_presentation(None, "application/vnd.oasis.opendocument.presentation"),
            "unsupported",
        )
        self.assertIsNone(classify_presentation("notes.pdf", "application/pdf"))
        self.assertIsNone(classify_presentation("song.mp3", "audio/mpeg"))

    def test_media_kind_detection(self):
        self.assertEqual(media_kind("ppt/media/media1.m4a"), "audio")
        self.assertEqual(media_kind("ppt/media/clip.MP4"), "video")
        self.assertIsNone(media_kind("ppt/media/image1.png"))


class DeckReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def _deck(self, **kwargs) -> Path:
        return build_deck(self.root / "deck.pptx", **kwargs)

    def test_clips_follow_slide_order_and_slide_text_is_read(self):
        deck = self._deck(
            slides=[
                {
                    "title": "اسلاید اول",
                    "bullets": ["نکتهٔ یک", "نکتهٔ دو"],
                    "notes": "یادداشت گوینده",
                    "media": [("media1.wav", wav_bytes(12.0), AUDIO_REL)],
                },
                {
                    "title": "اسلاید دوم",
                    "bullets": ["نکتهٔ سه"],
                    "media": [("media2.wav", wav_bytes(8.0), AUDIO_REL)],
                },
            ]
        )
        content = read_presentation(deck, self.root / "media", make_settings())

        self.assertEqual(content.slide_count, 2)
        self.assertEqual([clip.slide_number for clip in content.clips], [1, 2])
        self.assertEqual(
            [Path(clip.part_name).name for clip in content.clips],
            ["media1.wav", "media2.wav"],
        )
        self.assertTrue(all(clip.path.exists() for clip in content.clips))
        self.assertEqual(content.slides[0].title, "اسلاید اول")
        self.assertIn("نکتهٔ دو", content.slides[0].body)
        self.assertEqual(content.slides[0].notes, "یادداشت گوینده")

        outline = slides_outline(content.slides)
        self.assertIn("### اسلاید 1 — اسلاید اول", outline)
        self.assertIn("- نکتهٔ سه", outline)
        self.assertIn("یادداشت گوینده", outline)

    def test_repeated_media_is_deduplicated_and_orphans_come_last(self):
        payload = wav_bytes(6.0)
        deck = self._deck(
            slides=[
                {"title": "یک", "media": [("shared.wav", payload, AUDIO_REL)]},
                {"title": "دو", "media": [("shared.wav", payload, AUDIO_REL)]},
            ],
            orphan_media={"loose.wav": wav_bytes(9.0), "logo.png": b"png"},
        )
        content = read_presentation(deck, self.root / "media", make_settings())
        names = [Path(clip.part_name).name for clip in content.clips]
        self.assertEqual(names, ["shared.wav", "loose.wav"])
        self.assertEqual(content.clips[0].slide_number, 1)
        self.assertIsNone(content.clips[1].slide_number)

    def test_video_media_is_skipped_when_disabled(self):
        deck = self._deck(
            slides=[
                {
                    "title": "ویدیو",
                    "media": [
                        ("clip.mp4", video_bytes(duration=1.0), VIDEO_REL),
                        ("voice.wav", wav_bytes(4.0), AUDIO_REL),
                    ],
                }
            ]
        )
        settings = make_settings(presentation_include_video_audio=False)
        content = read_presentation(deck, self.root / "media", settings)
        self.assertEqual([c.kind for c in content.clips], ["audio"])
        self.assertTrue(any("ویدیو" in item for item in content.skipped))

        enabled = read_presentation(deck, self.root / "media2", make_settings())
        self.assertEqual(sorted(c.kind for c in enabled.clips), ["audio", "video"])

    def test_slide_text_can_be_disabled(self):
        deck = self._deck(slides=[{"title": "عنوان", "bullets": ["متن"]}])
        content = read_presentation(
            deck, self.root / "media", make_settings(presentation_include_slide_text=False)
        )
        self.assertEqual(content.slides, ())

    def test_non_zip_and_oversized_archives_are_rejected(self):
        broken = self.root / "broken.pptx"
        broken.write_bytes(b"not a zip file")
        with self.assertRaises(PresentationError):
            read_presentation(broken, self.root / "media", make_settings())

        deck = self._deck(slides=[{"title": "یک", "bullets": ["متن"]}])
        with self.assertRaises(PresentationError):
            read_presentation(
                deck, self.root / "media", make_settings(presentation_max_unpacked_bytes=1024)
            )

    def test_clip_limit_is_enforced(self):
        deck = self._deck(
            slides=[
                {
                    "title": "چند صدا",
                    "media": [
                        (f"media{i}.wav", wav_bytes(3.0), AUDIO_REL) for i in range(5)
                    ],
                }
            ]
        )
        content = read_presentation(
            deck, self.root / "media", make_settings(presentation_max_clips=2)
        )
        self.assertEqual(len(content.clips), 2)
        self.assertTrue(any("بیش از حد مجاز" in item for item in content.skipped))


class MediaCommandTests(unittest.TestCase):
    def test_probe_output_parsing(self):
        info = parse_probe_output(
            json.dumps(
                {"has_audio": True, "duration": 12.25, "audio_codec": "aac"}
            )
        )
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.duration, 12.25)
        self.assertEqual(info.audio_codec, "aac")

        silent = parse_probe_output(
            '{"has_audio": false, "duration": null, "audio_codec": null}'
        )
        self.assertFalse(silent.has_audio)
        self.assertIsNone(silent.duration)

        self.assertEqual(parse_probe_output("{}").has_audio, False)

    def test_unusable_durations_degrade_instead_of_failing(self):
        for duration in ("nan", "inf", "-inf", -5, "garbage", False):
            with self.subTest(duration=duration):
                info = parse_probe_output(
                    json.dumps({"has_audio": True, "duration": duration})
                )
                self.assertIsNone(info.duration)
        # Non-string codec values never reach .lower() unconverted.
        info = parse_probe_output('{"has_audio": true, "audio_codec": 42}')
        self.assertIsNone(info.audio_codec)

    def test_malformed_probe_output_is_reported_as_media_error(self):
        for payload in ("<<not json>>", "[]", "null", '{"has_audio": 5}'):
            with self.subTest(payload=payload), self.assertRaises(MediaToolError):
                parse_probe_output(payload)

    def test_probe_command_targets_the_python_worker(self):
        command = build_probe_command(Path("/tmp/a b.m4a"))
        self.assertEqual(command[0], sys.executable)
        self.assertIn("gamas_bot.media_worker", command)
        self.assertEqual(command[-2:], ["--", "/tmp/a b.m4a"])

    def test_merge_command_builds_expected_arguments(self):
        command = build_merge_command(
            [Path("a.m4a"), Path("b.wav"), Path("c.mp3")],
            Path("out.wav"),
            output_format="wav",
            silence_seconds=0.5,
        )
        self.assertEqual(command.count("--"), 1)
        self.assertEqual(command[command.index("--format") + 1], "wav")
        self.assertEqual(command[command.index("--silence") + 1], "0.5")
        self.assertEqual(command[command.index("--") + 1:], ["a.m4a", "b.wav", "c.mp3"])

    def test_merge_command_switches_codec_for_opus(self):
        command = build_merge_command(
            [Path("a.m4a")], Path("out.ogg"), output_format="opus", silence_seconds=0
        )
        self.assertEqual(command[command.index("--format") + 1], "opus")
        self.assertEqual(command[command.index("--output") + 1], "out.ogg")

    def test_merge_command_requires_inputs(self):
        with self.assertRaises(MediaToolError):
            build_merge_command([], Path("out.wav"))

    def test_format_choice_depends_on_estimated_size(self):
        settings = make_settings(presentation_wav_limit_bytes=700_000_000)
        self.assertEqual(choose_merge_format(600, settings), "wav")
        self.assertEqual(choose_merge_format(30_000, settings), "opus")
        self.assertEqual(choose_merge_format(None, settings), "wav")
        self.assertEqual(merge_output_name("x", "opus"), "x.ogg")
        self.assertEqual(merge_output_name("x", "wav"), "x.wav")

    def test_convert_command_uses_the_python_worker_and_size_limit(self):
        command = build_convert_command(
            Path("/tmp/deck.ppt"), Path("/tmp/out/deck.pptx"), max_input_bytes=1234
        )
        self.assertEqual(command[0], sys.executable)
        self.assertIn("gamas_bot.media_worker", command)
        self.assertEqual(command[command.index("--max-input-bytes") + 1], "1234")
        self.assertEqual(command[-2:], ["--", "/tmp/deck.ppt"])
        for argument in command:
            self.assertNotIn(Path(argument).name, {"ffmpeg", "ffprobe", "soffice"})


class PrepareAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    async def test_prepare_audio_merges_usable_clips_only(self):
        deck = build_deck(
            self.root / "deck.pptx",
            slides=[
                {"title": "یک", "media": [("a.wav", wav_bytes(20.0), AUDIO_REL)]},
                {"title": "دو", "media": [("b.wav", wav_bytes(0.2), AUDIO_REL)]},
                {
                    "title": "سه",
                    "media": [
                        ("c.mp4", video_bytes(duration=1.0, with_audio=False), VIDEO_REL)
                    ],
                },
                {"title": "چهار", "media": [("d.wav", wav_bytes(30.0), AUDIO_REL)]},
            ],
        )
        settings = make_settings(presentation_min_clip_seconds=1.0)
        content = read_presentation(deck, self.root / "media", settings)
        prepared = await prepare_audio(content, self.root / "work", settings)

        assert prepared is not None
        self.assertTrue(prepared.merged)
        self.assertEqual([Path(c.part_name).name for c in prepared.clips], ["a.wav", "d.wav"])
        self.assertAlmostEqual(prepared.total_duration, 50.0)
        self.assertTrue(prepared.path.exists())
        with wave.open(str(prepared.path), "rb") as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnchannels(), 1)
            # 50 s of narration plus one 0.5 s silence pad between two clips.
            self.assertAlmostEqual(audio.getnframes() / 16000, 50.5, places=2)
        self.assertEqual(len(prepared.skipped), 2)

    async def test_prepare_audio_returns_none_without_usable_audio(self):
        deck = build_deck(
            self.root / "silent.pptx",
            slides=[{"title": "یک", "bullets": ["فقط متن"]}],
        )
        content = read_presentation(deck, self.root / "media", make_settings())
        self.assertIsNone(await prepare_audio(content, self.root / "work", make_settings()))

    async def test_total_duration_cap_is_enforced(self):
        deck = build_deck(
            self.root / "long.pptx",
            slides=[{"title": "یک", "media": [("a.wav", wav_bytes(500.0), AUDIO_REL)]}],
        )
        settings = make_settings(presentation_max_total_duration=60)
        content = read_presentation(deck, self.root / "media", settings)
        with self.assertRaises(PresentationError):
            await prepare_audio(content, self.root / "work", settings)

    async def test_single_clip_is_merged_without_extra_silence(self):
        deck = build_deck(
            self.root / "one.pptx",
            slides=[{"title": "یک", "media": [("a.wav", wav_bytes(11.0), AUDIO_REL)]}],
        )
        content = read_presentation(deck, self.root / "media", make_settings())
        prepared = await prepare_audio(content, self.root / "work", make_settings())
        assert prepared is not None
        self.assertTrue(prepared.merged)
        self.assertEqual(prepared.path.name, "presentation-audio.wav")
        with wave.open(str(prepared.path), "rb") as audio:
            self.assertAlmostEqual(audio.getnframes() / 16000, 11.0, places=2)

    async def test_unreadable_clip_is_skipped_with_a_reason(self):
        deck = build_deck(
            self.root / "bad.pptx",
            slides=[
                {"title": "یک", "media": [("broken.wav", b"not a wav file", AUDIO_REL)]},
                {"title": "دو", "media": [("good.wav", wav_bytes(4.0), AUDIO_REL)]},
            ],
        )
        content = read_presentation(deck, self.root / "media", make_settings())
        reasons: list[str] = []
        prepared = await prepare_audio(
            content, self.root / "work", make_settings(), skipped_out=reasons
        )
        assert prepared is not None
        self.assertEqual(len(prepared.clips), 1)
        self.assertTrue(any("بررسی فایل ناموفق" in item for item in reasons))


class LegacyConversionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    async def test_legacy_deck_is_converted_through_ppt2pptx(self):
        fixture = FIXTURES_DIR / "visual_minimal.ppt"
        legacy = self.root / "old.ppt"
        shutil.copyfile(fixture, legacy)
        settings = make_settings()
        converted = await convert_to_pptx(legacy, self.root / "converted", settings)
        self.assertTrue(converted.exists())
        self.assertEqual(converted.suffix, ".pptx")
        content = read_presentation(converted, self.root / "media", settings)
        self.assertEqual(content.slide_count, 2)

    async def test_legacy_slideshow_and_template_variants_convert(self):
        for suffix in (".pps", ".pot"):
            with self.subTest(suffix=suffix):
                legacy = self.root / f"deck{suffix}"
                shutil.copyfile(FIXTURES_DIR / "visual_minimal.ppt", legacy)
                converted = await convert_to_pptx(
                    legacy, self.root / f"out{suffix}", make_settings()
                )
                self.assertEqual(converted.suffix, ".pptx")
                self.assertGreater(converted.stat().st_size, 0)

    async def test_embedded_media_deck_converts_with_diagnostics(self):
        legacy = self.root / "video.ppt"
        shutil.copyfile(FIXTURES_DIR / "visual_video.ppt", legacy)
        with self.assertLogs("gamas_bot.media", level="INFO") as logs:
            converted = await convert_to_pptx(legacy, self.root / "out", make_settings())
        self.assertTrue(converted.exists())
        # ppt2pptx reports lossy legacy features instead of faking fidelity.
        self.assertTrue(any("MEDIA_ACTION_OMITTED" in line for line in logs.output))

    async def test_conversion_failure_is_reported(self):
        legacy = self.root / "garbage.ppt"
        legacy.write_bytes(b"this is not a compound file")
        with self.assertRaises(MediaToolError) as caught:
            await convert_to_pptx(legacy, self.root / "converted", make_settings())
        self.assertIn("ناموفق", str(caught.exception))

    async def test_odp_decks_are_rejected_explicitly(self):
        legacy = self.root / "deck.odp"
        legacy.write_bytes(b"PK\x03\x04 fake odp")
        with self.assertRaises(MediaToolError) as caught:
            await convert_to_pptx(legacy, self.root / "converted", make_settings())
        self.assertIn("ODP/OTP", str(caught.exception))


class PresentationStructuringTests(unittest.IsolatedAsyncioTestCase):
    def test_document_contains_both_sources(self):
        document = build_presentation_document("### اسلاید 1", "متن صدا")
        self.assertIn("## متن اسلایدها", document)
        self.assertIn("## متن پیاده‌سازی‌شدهٔ صدای ارائه", document)

    async def test_outline_is_repeated_for_every_transcript_chunk(self):
        outline = "### اسلاید 1 — مقدمه"
        transcript = "این یک جملهٔ فارسی برای آزمون است. " * 400
        with patch(
            "gamas_bot.structuring._structure_chunk",
            new=AsyncMock(return_value=sample_notes_json()),
        ) as chunker:
            result = await structure_presentation(
                outline,
                transcript,
                make_settings(note_global_context_enabled=False),
                max_chars=4000,
            )
        self.assertGreater(chunker.await_count, 1)
        for call in chunker.await_args_list:
            self.assertIn(outline, call.args[0])
            self.assertIn("محتوای ارائه", call.args[3])
        # Every chunk returned the same single section; adjacent sections with
        # one logical heading now join into a single section instead of
        # stacking once per chunk, and the content itself is kept exactly once.
        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].paragraphs, ("متن بخش نخست",))
        self.assertEqual(result.sections[0].bullets, ("نکتهٔ یک",))
        self.assertEqual(len(result.sections[0].callouts), 1)

    async def test_slide_only_decks_still_produce_a_booklet(self):
        with patch(
            "gamas_bot.structuring._structure_chunk",
            new=AsyncMock(return_value=sample_notes_json("جزوهٔ اسلایدها")),
        ) as chunker:
            result = await structure_presentation("### اسلاید 1", "", make_settings())
        self.assertEqual(result.title, "جزوهٔ اسلایدها")
        self.assertEqual(chunker.await_count, 1)

    async def test_empty_input_is_rejected(self):
        with self.assertRaises(StructuringError):
            await structure_presentation("", "", make_settings())


if __name__ == "__main__":
    unittest.main()
