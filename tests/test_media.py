"""Real (unfaked) tests for the media layer: probing, extraction and merging.

Every other media test fakes ``probe_media`` / ``extract_audio_track`` at the
bot seam, so the code that actually decodes audio — ``media_worker.py`` and the
PowerPoint reader in ``presentations.py`` — had no direct coverage. These tests
drive the real worker subprocess and PyAV against files built by
``tests.support`` (WAV/MP4/WebM/MP3) and the committed legacy ``.ppt`` decks,
so a regression in extraction from a video or a deck is caught here.
"""

from __future__ import annotations

import functools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot import media_worker
from gamas_bot.bot import GamasBot
from gamas_bot.media import (
    MediaInfo,
    MediaToolError,
    check_media_worker,
    choose_merge_format,
    convert_to_pptx,
    extract_audio_track,
    merge_audio_tracks,
    probe_media,
    probe_media_many,
)
from gamas_bot.presentations import prepare_audio, read_presentation, slides_outline
from tests.support import (
    AUDIO_REL,
    FIXTURES_DIR,
    VIDEO_REL,
    build_deck,
    make_settings,
    mp3_bytes,
    video_bytes,
    wav_bytes,
)

# Generated once per process: encoding a clip is the slow part of these tests.
_wav = functools.lru_cache(maxsize=None)(
    lambda duration: wav_bytes(duration=duration)
)
_video = functools.lru_cache(maxsize=None)(
    lambda duration, with_audio: video_bytes(duration=duration, with_audio=with_audio)
)
_mp3 = functools.lru_cache(maxsize=None)(lambda duration: mp3_bytes(duration))


class _AudioPipelineStub:
    """Just enough surface for ``GamasBot._prepare_audio`` (it reads settings only)."""

    def __init__(self, settings):
        self.settings = settings


class MediaTempCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self._folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def write(self, name: str, payload: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(payload)
        return path


class ProbeTests(MediaTempCase):
    async def test_probe_reports_duration_and_codec_for_audio(self):
        path = self.write("a.wav", _wav(1.0))
        info = await probe_media(path, self.settings)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.audio_codec, "pcm_s16le")
        self.assertIsNotNone(info.duration)
        self.assertAlmostEqual(info.duration, 1.0, delta=0.2)

    async def test_probe_detects_a_video_audio_track(self):
        path = self.write("v.mp4", _video(1.0, True))
        info = await probe_media(path, self.settings)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.audio_codec, "aac")

    async def test_probe_reports_no_audio_for_a_silent_video(self):
        path = self.write("silent.mp4", _video(1.0, False))
        info = await probe_media(path, self.settings)
        self.assertFalse(info.has_audio)
        self.assertIsNone(info.audio_codec)

    async def test_batched_probe_matches_the_reported_files(self):
        audio = self.write("a.wav", _wav(1.0))
        video = self.write("v.mp4", _video(1.0, True))
        silent = self.write("silent.mp4", _video(1.0, False))
        reports = await probe_media_many([audio, video, silent], self.settings)
        self.assertEqual({str(audio), str(video), str(silent)}, set(reports))
        self.assertTrue(reports[str(audio)].has_audio)
        self.assertTrue(reports[str(video)].has_audio)
        self.assertFalse(reports[str(silent)].has_audio)


class ExtractTests(MediaTempCase):
    async def test_extract_pulls_a_mono_wav_track_from_video(self):
        source = self.write("v.mp4", _video(1.0, True))
        out = await extract_audio_track(source, self.tmp / "out", self.settings)
        self.assertEqual(out.suffix, ".wav")
        info = await probe_media(out, self.settings)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.audio_codec, "pcm_s16le")
        self.assertAlmostEqual(info.duration, 1.0, delta=0.3)

    async def test_extract_uses_opus_when_wav_would_exceed_the_limit(self):
        source = self.write("v.mp4", _video(1.0, True))
        settings = make_settings(presentation_wav_limit_bytes=1024)
        out = await extract_audio_track(
            source, self.tmp / "out", settings, total_duration=600.0
        )
        self.assertEqual(out.suffix, ".ogg")
        info = await probe_media(out, settings)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.audio_codec, "opus")

    async def test_merge_concatenates_with_padding_silence(self):
        first = self.write("a.wav", _wav(1.0))
        second = self.write("b.wav", _wav(1.0))
        settings = make_settings(presentation_silence_seconds=0.5)
        out = await merge_audio_tracks(
            [first, second], self.tmp / "merged", settings,
            total_duration=2.0, stem="m",
        )
        info = await probe_media(out, self.settings)
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.duration, 2.5, delta=0.3)


class FormatChoiceTests(unittest.TestCase):
    def test_choose_merge_format_switches_to_opus_above_the_limit(self):
        settings = make_settings(presentation_wav_limit_bytes=32000)
        self.assertEqual(choose_merge_format(None, settings), "wav")
        self.assertEqual(choose_merge_format(0.5, settings), "wav")
        self.assertEqual(choose_merge_format(10.0, settings), "opus")


class PrepareAudioRoutingTests(MediaTempCase):
    """The bot must forward the measured duration to extraction."""

    async def test_prepare_audio_forwards_the_probed_duration(self):
        captured: dict = {}

        async def fake_probe(_path, _settings):
            return MediaInfo(True, 123.5, "aac")

        async def fake_extract(source, _workdir, _settings, *, total_duration=None, stem="audio"):
            captured["total_duration"] = total_duration
            return source

        stub = _AudioPipelineStub(self.settings)
        with mock.patch("gamas_bot.bot.probe_media", new=fake_probe), mock.patch(
            "gamas_bot.bot.extract_audio_track", new=fake_extract
        ):
            result = await GamasBot._prepare_audio(
                stub, Path("v.mp4"), self.tmp, "v.mp4", "video"
            )
        self.assertEqual(captured["total_duration"], 123.5)
        self.assertEqual(result, Path("v.mp4"))

    async def test_passthrough_audio_is_not_transcoded(self):
        async def fake_probe(_path, _settings):
            return MediaInfo(True, 10.0, "mp3")

        async def fail_extract(*_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("passthrough audio must not be extracted")

        stub = _AudioPipelineStub(self.settings)
        source = self.write("a.mp3", _mp3(0.5))
        with mock.patch("gamas_bot.bot.probe_media", new=fake_probe), mock.patch(
            "gamas_bot.bot.extract_audio_track", new=fail_extract
        ):
            result = await GamasBot._prepare_audio(
                stub, source, self.tmp, "a.mp3", "audio"
            )
        self.assertEqual(result, source)


class PresentationTests(MediaTempCase):
    def deck_with_media(self) -> Path:
        return build_deck(
            self.tmp / "deck.pptx",
            slides=[
                {"title": "One", "notes": "n", "media": [("a.wav", _wav(1.5), AUDIO_REL)]},
                {"title": "Two", "media": [("v.mp4", _video(1.0, True), VIDEO_REL)]},
                {"title": "Three", "media": [("silent.mp4", _video(1.0, False), VIDEO_REL)]},
            ],
            orphan_media={"orphan.mp3": _mp3(0.8)},
        )

    async def test_deck_extracts_and_merges_audio_and_video_narration(self):
        deck = self.deck_with_media()
        content = read_presentation(deck, self.tmp / "media", self.settings)
        self.assertEqual(
            [(clip.slide_number, clip.kind) for clip in content.clips],
            [(1, "audio"), (2, "video"), (3, "video"), (None, "audio")],
        )
        prepared = await prepare_audio(content, self.tmp / "work", self.settings)
        self.assertIsNotNone(prepared)
        # The silent video is dropped; the other three clips are merged.
        self.assertEqual(len(prepared.clips), 3)
        self.assertTrue(
            any("بدون شاخهٔ صوتی" in reason for reason in prepared.skipped)
        )
        info = await probe_media(prepared.path, self.settings)
        self.assertTrue(info.has_audio)
        # 1.5 + 1.0 + 0.8 seconds plus two 0.25 s paddings.
        self.assertAlmostEqual(info.duration, 4.05, delta=0.3)

    async def test_video_audio_is_dropped_when_disabled(self):
        deck = self.deck_with_media()
        settings = make_settings(presentation_include_video_audio=False)
        content = read_presentation(deck, self.tmp / "media", settings)
        # Video clips are dropped; plain audio narration is still kept.
        self.assertFalse(any(clip.kind == "video" for clip in content.clips))
        self.assertTrue(any(clip.kind == "audio" for clip in content.clips))
        self.assertTrue(
            any("ویدیو نادیده گرفته شد" in reason for reason in content.skipped)
        )
        self.assertIsNotNone(await prepare_audio(content, self.tmp / "work", settings))

    async def test_too_short_clip_is_skipped_not_merged(self):
        deck = build_deck(
            self.tmp / "short.pptx",
            slides=[{"title": "S", "media": [("s.wav", _wav(0.2), AUDIO_REL)]}],
        )
        content = read_presentation(deck, self.tmp / "media", self.settings)
        skipped: list[str] = []
        prepared = await prepare_audio(
            content, self.tmp / "work", self.settings, skipped_out=skipped
        )
        self.assertIsNone(prepared)
        # The Persian reason carries ZWNJ; the file name is the stable anchor.
        self.assertEqual(len(skipped), 1, skipped)
        self.assertIn("s.wav", skipped[0])

    async def test_text_only_deck_has_no_audio_but_keeps_slides(self):
        deck = build_deck(
            self.tmp / "text.pptx",
            slides=[{"title": "Only", "bullets": ["x"]}],
        )
        content = read_presentation(deck, self.tmp / "media", self.settings)
        self.assertEqual(content.clips, ())
        self.assertTrue(content.slides)
        self.assertIsNone(await prepare_audio(content, self.tmp / "work", self.settings))

    def test_outline_contains_slide_text_and_speaker_notes(self):
        deck = build_deck(
            self.tmp / "notes.pptx",
            slides=[{
                "title": "موضوع ارائه",
                "bullets": ["نکتهٔ روی اسلاید"],
                "notes": "توضیح تکمیلی گوینده",
            }],
        )
        content = read_presentation(deck, self.tmp / "media", self.settings)
        outline = slides_outline(content.slides)
        self.assertIn("موضوع ارائه", outline)
        self.assertIn("نکتهٔ روی اسلاید", outline)
        self.assertIn("یادداشت گوینده: توضیح تکمیلی گوینده", outline)


class LegacyConversionTests(MediaTempCase):
    async def test_bundled_legacy_deck_converts_and_reads_back(self):
        source = FIXTURES_DIR / "visual_minimal.ppt"
        converted = await convert_to_pptx(source, self.tmp / "converted", self.settings)
        self.assertEqual(converted.suffix, ".pptx")
        self.assertGreater(converted.stat().st_size, 0)
        content = read_presentation(converted, self.tmp / "media", self.settings)
        self.assertGreaterEqual(content.slide_count, 1)

    async def test_odp_is_refused_with_a_clear_error(self):
        source = self.write("deck.odp", b"not really an odp")
        with self.assertRaises(MediaToolError):
            await convert_to_pptx(source, self.tmp / "out", self.settings)


class WorkerDependencyTests(MediaTempCase):
    async def test_worker_check_reports_both_dependencies(self):
        payload = json.loads(await check_media_worker())
        self.assertIn("av", payload)
        self.assertIn("ppt2pptx", payload)

    def test_configure_logging_survives_a_missing_av(self):
        with mock.patch.dict(sys.modules, {"av": None}):
            media_worker._configure_logging()  # must not raise

    def test_require_av_reports_a_friendly_error(self):
        with mock.patch.dict(sys.modules, {"av": None}):
            with self.assertRaises(media_worker.WorkerError) as caught:
                media_worker._require_av()
        self.assertIn("media dependencies are missing", str(caught.exception))

    def test_check_reports_a_friendly_error_when_av_is_missing(self):
        with mock.patch.dict(sys.modules, {"av": None}):
            with self.assertRaises(media_worker.WorkerError):
                media_worker._check()


if __name__ == "__main__":
    unittest.main()
