"""Real media-worker smoke tests; no Telegram, provider or system-binary deps.

Every test here runs the actual PyAV/ppt2pptx worker, because the suite must
prove the bot works without any ``ffmpeg``/``ffprobe``/``soffice`` binaries on
the host.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from gamas_bot.media import (
    MediaToolError,
    build_extract_command,
    build_merge_command,
    build_probe_command,
    check_media_worker,
    extract_audio_track,
    merge_audio_tracks,
    probe_media,
    run_command,
    worker_command,
)
from gamas_bot.media_worker import INPUT_PROTOCOL_OPTIONS
from support import make_settings, mp3_bytes, video_bytes, wav_bytes

# The suite must not require system media binaries anywhere.
BANNED_TOOLS = ("ffmpeg", "ffprobe", "soffice")


class CommandLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_drains_full_output_pipes(self):
        script = "import sys,time; sys.stdout.write('x'*200000); sys.stdout.flush(); time.sleep(10)"
        with self.assertRaises(MediaToolError):
            await asyncio.wait_for(run_command([sys.executable, "-c", script], timeout=0.2), 5)

    async def test_cancelled_command_terminates(self):
        task = asyncio.create_task(run_command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=20))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    async def test_timeout_kills_launcher_children(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "child-survived"
            child = f"import pathlib,time; time.sleep(0.6); pathlib.Path({str(marker)!r}).touch()"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            with self.assertRaises(MediaToolError):
                await asyncio.wait_for(run_command([sys.executable, "-c", parent], timeout=0.3), 5)
            await asyncio.sleep(0.7)
            self.assertFalse(marker.exists())


class WorkerCommandTests(unittest.TestCase):
    """Command vectors are built by pure functions and never call media CLIs."""

    def _assert_is_worker(self, command: list[str]):
        self.assertEqual(command[0], sys.executable)
        self.assertIn("gamas_bot.media_worker", command)
        for argument in command:
            name = Path(argument).name.lower()
            for banned in BANNED_TOOLS:
                self.assertNotEqual(name, banned, f"{banned} must never be invoked")

    def test_probe_command_targets_the_python_worker(self):
        command = build_probe_command(Path("/tmp/a b.m4a"))
        self._assert_is_worker(command)
        self.assertEqual(command[-2:], ["--", "/tmp/a b.m4a"])

    def test_merge_command_lists_inputs_after_options(self):
        command = build_merge_command(
            [Path("a.mp4"), Path("b.m4a")], Path("out.wav"),
            output_format="opus", silence_seconds=0.5,
        )
        self._assert_is_worker(command)
        self.assertEqual(command[command.index("--format") + 1], "opus")
        self.assertEqual(command[command.index("--silence") + 1], "0.5")
        self.assertEqual(command[command.index("--") + 1:], ["a.mp4", "b.m4a"])

    def test_extract_command_targets_the_python_worker(self):
        command = build_extract_command(Path("in.mkv"), Path("out.wav"), output_format="wav")
        self._assert_is_worker(command)
        self.assertIn("--output", command)
        self.assertEqual(command[-2:], ["--", "in.mkv"])

    def test_empty_merge_input_list_is_rejected(self):
        with self.assertRaises(MediaToolError):
            build_merge_command([], Path("out.wav"))

    def test_inputs_are_restricted_to_local_protocols(self):
        # The whitelist now lives inside the worker: every av.open() input is
        # opened with exactly these options.
        self.assertEqual(INPUT_PROTOCOL_OPTIONS, {"protocol_whitelist": "file,pipe"})

    def test_worker_module_is_the_only_media_entrypoint(self):
        self.assertEqual(worker_command("check")[1:3], ["-m", "gamas_bot.media_worker"])


class WorkerSelfCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_reports_installed_media_packages(self):
        summary = json.loads(await check_media_worker())
        self.assertIn("av", summary)
        self.assertIn("ppt2pptx", summary)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, payload: bytes, name: str) -> "object":
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / name
            source.write_bytes(payload)
            return await probe_media(source, make_settings())

    async def test_probe_reads_real_wav(self):
        info = await self._probe(wav_bytes(45.0, rate=44100, channels=2), "a.wav")
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.duration or 0, 45.0, places=2)
        self.assertEqual(info.audio_codec, "pcm_s16le")

    async def test_probe_reads_real_mp3_with_canonical_codec_name(self):
        info = await self._probe(mp3_bytes(3.0), "a.mp3")
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.duration or 0, 3.0, places=2)
        # ffprobe-style canonical name, not the mp3float decoder name.
        self.assertEqual(info.audio_codec, "mp3")

    async def test_probe_detects_video_with_and_without_audio(self):
        with_audio = await self._probe(video_bytes(duration=1.0, with_audio=True), "v.mp4")
        self.assertTrue(with_audio.has_audio)
        self.assertEqual(with_audio.audio_codec, "aac")

        silent = await self._probe(video_bytes(duration=1.0, with_audio=False), "s.mp4")
        self.assertFalse(silent.has_audio)
        self.assertIsNone(silent.audio_codec)

    async def test_probe_rejects_corrupt_files(self):
        with self.assertRaises(MediaToolError):
            await self._probe(b"definitely not media data" * 10, "bad.mp3")

    async def test_probe_failure_leaves_no_output_for_downstream_use(self):
        # A missing path must fail like the old ffprobe call did.
        with self.assertRaises(MediaToolError):
            await probe_media(Path("/nonexistent/missing.wav"), make_settings())


class RealMediaWorkerTests(unittest.IsolatedAsyncioTestCase):
    def write_wav(self, path: Path, rate: int, channels: int):
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(b"\0\0" * rate * channels)

    async def test_merge_resamples_and_adds_expected_silence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs = [root / "one.wav", root / "two.wav"]
            self.write_wav(inputs[0], 22050, 2)
            self.write_wav(inputs[1], 44100, 1)
            output = await merge_audio_tracks(
                inputs, root / "out", make_settings(), total_duration=2.5
            )
            with wave.open(str(output), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnchannels(), 1)
                self.assertAlmostEqual(audio.getnframes() / 16000, 2.5, places=2)

    async def test_merge_encodes_opus_when_wav_would_exceed_the_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs = [root / "one.wav", root / "two.wav"]
            self.write_wav(inputs[0], 22050, 2)
            self.write_wav(inputs[1], 44100, 1)
            settings = make_settings(presentation_wav_limit_bytes=100)
            output = await merge_audio_tracks(
                inputs, root / "out", settings, total_duration=2.5
            )
            self.assertEqual(output.suffix, ".ogg")
            info = await probe_media(output, settings)
            self.assertTrue(info.has_audio)
            self.assertEqual(info.audio_codec, "opus")
            self.assertAlmostEqual(info.duration or 0, 2.5, delta=0.1)

    async def test_extract_audio_supports_wav_and_opus(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.wav"
            self.write_wav(source, 22050, 2)
            for limit, suffix in ((700_000_000, ".wav"), (100, ".ogg")):
                settings = make_settings(presentation_wav_limit_bytes=limit)
                output = await extract_audio_track(
                    source, root / str(limit), settings, total_duration=1
                )
                self.assertEqual(output.suffix, suffix)
                self.assertGreater(output.stat().st_size, 0)

    async def test_extract_takes_the_first_audio_track_from_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "lecture.mp4"
            source.write_bytes(video_bytes(duration=2.0, with_audio=True))
            output = await extract_audio_track(
                source, root / "out", make_settings(), stem="extracted-audio"
            )
            with wave.open(str(output), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnchannels(), 1)
                self.assertAlmostEqual(audio.getnframes() / 16000, 2.0, delta=0.1)

    async def test_silent_video_cannot_be_extracted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "silent.mp4"
            source.write_bytes(video_bytes(duration=1.0, with_audio=False))
            with self.assertRaises(MediaToolError):
                await extract_audio_track(source, root / "out", make_settings())

    async def test_network_playlist_is_rejected_without_fetching_url(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            playlist = root / "input.m3u8"
            playlist.write_text(
                "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n"
                "http://127.0.0.1:9/private.ts\n#EXT-X-ENDLIST\n"
            )
            with self.assertRaises(MediaToolError) as caught:
                await extract_audio_track(
                    playlist, root / "out", make_settings()
                )
            # The worker refuses the http protocol before any fetch is made.
            self.assertIn("not on whitelist", str(caught.exception))

    async def test_failed_operation_reports_a_bounded_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            corrupt = root / "corrupt.mp3"
            corrupt.write_bytes(b"not a real mp3 file")
            with self.assertRaises(MediaToolError) as caught:
                await extract_audio_track(corrupt, root / "out", make_settings())
            self.assertLess(len(str(caught.exception)), 600)

    async def test_timeout_is_enforced_for_worker_operations(self):
        # A worker that never finishes is killed when the media timeout expires.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.wav"
            self.write_wav(source, 16000, 1)
            settings = make_settings(media_timeout=0)
            with self.assertRaises(MediaToolError) as caught:
                await extract_audio_track(source, root / "out", settings)
            self.assertIn("به پایان رسید", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
