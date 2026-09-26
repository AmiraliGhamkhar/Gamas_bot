from __future__ import annotations

import tempfile
import unittest
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
    run_command,
)
from gamas_bot.presentations import (
    PresentationError,
    classify_presentation,
    media_kind,
    prepare_audio,
    read_presentation,
    slides_outline,
)
from gamas_bot.structuring import build_presentation_document, structure_presentation

from support import (
    AUDIO_REL,
    VIDEO_REL,
    build_deck,
    fake_media_bytes,
    make_settings,
    stub_tools,
)


class ClassificationTests(unittest.TestCase):
    def test_extensions_and_mime_types_are_classified(self):
        self.assertEqual(classify_presentation("lecture.pptx", None), "native")
        self.assertEqual(classify_presentation("lecture.PPSX", None), "native")
        self.assertEqual(classify_presentation("old.ppt", None), "legacy")
        self.assertEqual(classify_presentation("deck.odp", None), "legacy")
        self.assertEqual(
            classify_presentation(
                None,
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            "native",
        )
        self.assertEqual(classify_presentation(None, "application/vnd.ms-powerpoint"), "legacy")
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
                    "media": [("media1.m4a", fake_media_bytes(12.0), AUDIO_REL)],
                },
                {
                    "title": "اسلاید دوم",
                    "bullets": ["نکتهٔ سه"],
                    "media": [("media2.wav", fake_media_bytes(8.0), AUDIO_REL)],
                },
            ]
        )
        content = read_presentation(deck, self.root / "media", make_settings())

        self.assertEqual(content.slide_count, 2)
        self.assertEqual([clip.slide_number for clip in content.clips], [1, 2])
        self.assertEqual(
            [Path(clip.part_name).name for clip in content.clips],
            ["media1.m4a", "media2.wav"],
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
        payload = fake_media_bytes(6.0)
        deck = self._deck(
            slides=[
                {"title": "یک", "media": [("shared.m4a", payload, AUDIO_REL)]},
                {"title": "دو", "media": [("shared.m4a", payload, AUDIO_REL)]},
            ],
            orphan_media={"loose.mp3": fake_media_bytes(9.0), "logo.png": b"png"},
        )
        content = read_presentation(deck, self.root / "media", make_settings())
        names = [Path(clip.part_name).name for clip in content.clips]
        self.assertEqual(names, ["shared.m4a", "loose.mp3"])
        self.assertEqual(content.clips[0].slide_number, 1)
        self.assertIsNone(content.clips[1].slide_number)

    def test_video_media_is_skipped_when_disabled(self):
        deck = self._deck(
            slides=[
                {
                    "title": "ویدیو",
                    "media": [
                        ("clip.mp4", fake_media_bytes(30.0), VIDEO_REL),
                        ("voice.m4a", fake_media_bytes(4.0), AUDIO_REL),
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
                        (f"media{i}.m4a", fake_media_bytes(3.0), AUDIO_REL) for i in range(5)
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
            '{"streams": [{"codec_type": "video"}, {"codec_type": "audio", '
            '"codec_name": "aac", "duration": "10.5"}], "format": {"duration": "12.25"}}'
        )
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.duration, 12.25)
        self.assertEqual(info.audio_codec, "aac")

        silent = parse_probe_output('{"streams": [{"codec_type": "video"}], "format": {}}')
        self.assertFalse(silent.has_audio)
        self.assertIsNone(silent.duration)

        with self.assertRaises(MediaToolError):
            parse_probe_output("<<not json>>")

    def test_probe_command_shape(self):
        command = build_probe_command("ffprobe", Path("/tmp/a b.m4a"))
        self.assertEqual(command[0], "ffprobe")
        self.assertIn("-show_streams", command)
        self.assertEqual(command[-1], "/tmp/a b.m4a")

    def test_merge_command_builds_a_valid_concat_graph(self):
        command = build_merge_command(
            "ffmpeg",
            [Path("a.m4a"), Path("b.wav"), Path("c.mp3")],
            Path("out.wav"),
            output_format="wav",
            silence_seconds=0.5,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(command.count("-i"), 3)
        self.assertEqual(graph.count("apad=pad_dur=0.5"), 2)  # not after the last clip
        self.assertIn("[a0][a1][a2]concat=n=3:v=0:a=1[out]", graph)
        self.assertIn("aresample=16000", graph)
        self.assertEqual(command[-1], "out.wav")
        self.assertIn("pcm_s16le", command)

    def test_merge_command_switches_codec_for_opus(self):
        command = build_merge_command(
            "ffmpeg", [Path("a.m4a")], Path("out.ogg"), output_format="opus", silence_seconds=0
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertNotIn("apad", graph)
        self.assertIn("aresample=48000", graph)
        self.assertIn("libopus", command)

    def test_merge_command_requires_inputs(self):
        with self.assertRaises(MediaToolError):
            build_merge_command("ffmpeg", [], Path("out.wav"))

    def test_format_choice_depends_on_estimated_size(self):
        settings = make_settings(presentation_wav_limit_bytes=700_000_000)
        self.assertEqual(choose_merge_format(600, settings), "wav")
        self.assertEqual(choose_merge_format(30_000, settings), "opus")
        self.assertEqual(choose_merge_format(None, settings), "wav")
        self.assertEqual(merge_output_name("x", "opus"), "x.ogg")
        self.assertEqual(merge_output_name("x", "wav"), "x.wav")

    def test_convert_command_uses_private_profile(self):
        command = build_convert_command(
            "soffice", Path("/tmp/deck.ppt"), Path("/tmp/out"), Path("/tmp/profile")
        )
        self.assertIn("--headless", command)
        self.assertIn("--convert-to", command)
        self.assertTrue(
            any(item.startswith("-env:UserInstallation=file://") for item in command)
        )
        self.assertEqual(command[-1], "/tmp/deck.ppt")


class ExternalToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tools = stub_tools(self.root / "bin")
        self.addCleanup(self.temp.cleanup)

    async def test_missing_binary_raises_media_tool_error(self):
        with self.assertRaises(MediaToolError):
            await run_command([str(self.root / "definitely-missing")], timeout=5)

    async def test_prepare_audio_merges_usable_clips_only(self):
        deck = build_deck(
            self.root / "deck.pptx",
            slides=[
                {"title": "یک", "media": [("a.m4a", fake_media_bytes(20.0), AUDIO_REL)]},
                {"title": "دو", "media": [("b.m4a", fake_media_bytes(0.2), AUDIO_REL)]},
                {
                    "title": "سه",
                    "media": [("c.mp4", fake_media_bytes(15.0, has_audio=False), VIDEO_REL)],
                },
                {"title": "چهار", "media": [("d.wav", fake_media_bytes(30.0), AUDIO_REL)]},
            ],
        )
        settings = make_settings(**self.tools, presentation_min_clip_seconds=1.0)
        content = read_presentation(deck, self.root / "media", settings)
        prepared = await prepare_audio(content, self.root / "work", settings)

        assert prepared is not None
        self.assertTrue(prepared.merged)
        self.assertEqual([Path(c.part_name).name for c in prepared.clips], ["a.m4a", "d.wav"])
        self.assertAlmostEqual(prepared.total_duration, 50.0)
        self.assertTrue(prepared.path.exists())
        merged_inputs = prepared.path.read_text(encoding="utf-8").splitlines()[1:]
        self.assertEqual([Path(item).name for item in merged_inputs], ["0001-a.m4a", "0004-d.wav"])
        self.assertEqual(len(prepared.skipped), 2)

    async def test_prepare_audio_returns_none_without_usable_audio(self):
        deck = build_deck(
            self.root / "silent.pptx",
            slides=[{"title": "یک", "bullets": ["فقط متن"]}],
        )
        settings = make_settings(**self.tools)
        content = read_presentation(deck, self.root / "media", settings)
        self.assertIsNone(await prepare_audio(content, self.root / "work", settings))

    async def test_total_duration_cap_is_enforced(self):
        deck = build_deck(
            self.root / "long.pptx",
            slides=[{"title": "یک", "media": [("a.m4a", fake_media_bytes(500.0), AUDIO_REL)]}],
        )
        settings = make_settings(**self.tools, presentation_max_total_duration=60)
        content = read_presentation(deck, self.root / "media", settings)
        with self.assertRaises(PresentationError):
            await prepare_audio(content, self.root / "work", settings)

    async def test_single_clip_skips_ffmpeg_when_unavailable(self):
        deck = build_deck(
            self.root / "one.pptx",
            slides=[{"title": "یک", "media": [("a.m4a", fake_media_bytes(11.0), AUDIO_REL)]}],
        )
        settings = make_settings(
            ffprobe_bin=self.tools["ffprobe_bin"], ffmpeg_bin=str(self.root / "no-ffmpeg")
        )
        content = read_presentation(deck, self.root / "media", settings)
        prepared = await prepare_audio(content, self.root / "work", settings)
        assert prepared is not None
        self.assertFalse(prepared.merged)
        self.assertEqual(prepared.path, content.clips[0].path)

    async def test_missing_ffmpeg_with_several_clips_is_reported(self):
        deck = build_deck(
            self.root / "two.pptx",
            slides=[
                {"title": "یک", "media": [("a.m4a", fake_media_bytes(11.0), AUDIO_REL)]},
                {"title": "دو", "media": [("b.m4a", fake_media_bytes(12.0), AUDIO_REL)]},
            ],
        )
        settings = make_settings(
            ffprobe_bin=self.tools["ffprobe_bin"], ffmpeg_bin=str(self.root / "no-ffmpeg")
        )
        content = read_presentation(deck, self.root / "media", settings)
        with self.assertRaises(PresentationError):
            await prepare_audio(content, self.root / "work", settings)

    async def test_legacy_deck_is_converted_through_libreoffice(self):
        legacy = self.root / "old.ppt"
        build_deck(legacy, slides=[{"title": "قدیمی", "bullets": ["متن"]}])
        settings = make_settings(**self.tools)
        converted = await convert_to_pptx(legacy, self.root / "converted", settings)
        self.assertTrue(converted.exists())
        self.assertEqual(converted.suffix, ".pptx")
        content = read_presentation(converted, self.root / "media", settings)
        self.assertEqual(content.slides[0].title, "قدیمی")

    async def test_conversion_without_libreoffice_is_reported(self):
        legacy = self.root / "old2.ppt"
        legacy.write_bytes(b"legacy")
        settings = make_settings(soffice_bin=str(self.root / "missing-soffice"))
        with self.assertRaises(MediaToolError):
            await convert_to_pptx(legacy, self.root / "converted2", settings)


class PresentationStructuringTests(unittest.IsolatedAsyncioTestCase):
    def test_document_contains_both_sources(self):
        document = build_presentation_document("### اسلاید 1", "متن صدا")
        self.assertIn("## متن اسلایدها", document)
        self.assertIn("## متن پیاده‌سازی‌شدهٔ صدای ارائه", document)

    async def test_outline_is_repeated_for_every_transcript_chunk(self):
        outline = "### اسلاید 1 — مقدمه"
        transcript = "این یک جملهٔ فارسی برای آزمون است. " * 400
        with patch(
            "gamas_bot.structuring._structure_chunk", new=AsyncMock(return_value="جزوه")
        ) as chunker:
            result = await structure_presentation(
                outline, transcript, make_settings(), max_chars=4000
            )
        self.assertGreater(chunker.await_count, 1)
        for call in chunker.await_args_list:
            self.assertIn(outline, call.args[0])
            self.assertIn("محتوای ارائه", call.args[3])
        self.assertIn("## بخش 1", result)

    async def test_slide_only_decks_still_produce_a_booklet(self):
        with patch(
            "gamas_bot.structuring._structure_chunk", new=AsyncMock(return_value="جزوه")
        ) as chunker:
            result = await structure_presentation("### اسلاید 1", "", make_settings())
        self.assertEqual(result, "جزوه")
        self.assertEqual(chunker.await_count, 1)

    async def test_empty_input_is_rejected(self):
        with self.assertRaises(Exception):
            await structure_presentation("", "", make_settings())


if __name__ == "__main__":
    unittest.main()
