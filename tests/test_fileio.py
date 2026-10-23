"""Async file adapters used by Telethon and aiohttp, with no live network."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

from telethon.client.uploads import _resize_photo_if_needed

from gamas_bot.fileio import AsyncFilePayload, AsyncFileReader, AsyncFileWriter, run_file_io


class AsyncFileAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_writer_and_reader_use_named_streams_without_losing_bytes(self):
        path = self.root / "artifact.txt"
        writer = await AsyncFileWriter.open(path)
        self.assertEqual(writer.name, str(path))
        self.assertEqual(await writer.write(b"first"), 5)
        self.assertEqual(await writer.write(memoryview(b" second")), 7)
        self.assertEqual(writer.tell(), 12)
        writer.flush()
        await writer.aclose()
        await writer.aclose()  # cleanup is idempotent

        reader = await AsyncFileReader.open(path)
        self.assertEqual(reader.name, path.name)
        self.assertEqual(await reader.read(5), b"first")
        self.assertEqual(await reader.read(), b" second")
        await reader.aclose()
        self.assertTrue(reader.closed)

    async def test_aiohttp_payload_streams_chunks_and_advertises_exact_size(self):
        path = self.root / "lecture.wav"
        expected = bytes(range(256)) * 1100
        path.write_bytes(expected)
        payload = await AsyncFilePayload.from_path(
            path, content_type="audio/wav", chunk_size=4096
        )
        self.assertEqual(payload.size, len(expected))
        self.assertEqual(payload.filename, path.name)
        self.assertEqual(payload.content_type, "audio/wav")
        self.assertEqual(await payload.as_bytes(), expected)

    async def test_stream_refuses_a_file_truncated_after_size_preflight(self):
        path = self.root / "changed.bin"
        path.write_bytes(b"before")
        payload = await AsyncFilePayload.from_path(path, chunk_size=2)
        path.write_bytes(b"x")
        with self.assertRaises(OSError):
            await payload.as_bytes()

    async def test_telethon_photo_resize_skips_async_nonseekable_reader(self):
        path = self.root / "receipt.jpg"
        path.write_bytes(b"not-needed-by-the-resize-guard")
        reader = await AsyncFileReader.open(path)
        try:
            # Telethon's photo helper must not pass an async ``read`` coroutine
            # to Pillow; a non-seekable IOBase stream bypasses its sync resize.
            self.assertIs(_resize_photo_if_needed(reader, True), reader)
        finally:
            await reader.aclose()

    async def test_cancellation_waits_for_the_bounded_thread_operation(self):
        finished = threading.Event()

        def slow_filesystem_call() -> str:
            time.sleep(0.03)
            finished.set()
            return "done"

        task = asyncio.create_task(run_file_io(slow_filesystem_call))
        await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
