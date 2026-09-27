"""Real subprocess/media smoke tests; no Telegram or provider credentials needed."""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from gamas_bot.media import MediaToolError, extract_audio_track, merge_audio_tracks, run_command
from support import make_settings

FFMPEG = os.environ.get("FFMPEG_TEST_BIN") or shutil.which("ffmpeg")


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


@unittest.skipUnless(FFMPEG, "Install ffmpeg or set FFMPEG_TEST_BIN for real media smoke tests")
class RealFFmpegTests(unittest.IsolatedAsyncioTestCase):
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
            settings = make_settings(ffmpeg_bin=FFMPEG)
            output = await merge_audio_tracks(inputs, root / "out", settings, total_duration=2.5)
            with wave.open(str(output), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnchannels(), 1)
                self.assertAlmostEqual(audio.getnframes() / 16000, 2.5, places=2)

    async def test_extract_audio_supports_wav_and_opus(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.wav"
            self.write_wav(source, 22050, 2)
            for limit, suffix in ((700_000_000, ".wav"), (100, ".ogg")):
                settings = make_settings(ffmpeg_bin=FFMPEG, presentation_wav_limit_bytes=limit)
                output = await extract_audio_track(source, root / str(limit), settings, total_duration=1)
                self.assertEqual(output.suffix, suffix)
                self.assertGreater(output.stat().st_size, 0)

    async def test_network_playlist_is_rejected_without_fetching_url(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            playlist = root / "input.m3u8"
            playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1:9/private.ts\n#EXT-X-ENDLIST\n")
            with self.assertRaises(MediaToolError) as caught:
                await extract_audio_track(playlist, root / "out", make_settings(ffmpeg_bin=FFMPEG))
            self.assertIn("not on whitelist", str(caught.exception))
