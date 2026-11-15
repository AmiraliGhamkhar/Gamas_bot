"""Runtime disk-reservation regressions for concurrent Telegram downloads."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gamas_bot.bot import GamasBot
from gamas_bot.telegram_transfer import (
    DiskSpaceError,
    DownloadReservationLedger,
    UploadTooLargeError,
    _download_media_limited,
)


class _Message:
    def __init__(self, size: int) -> None:
        self.file = SimpleNamespace(size=size)


class _OversizedStreamMessage:
    def __init__(self, declared_size: int, payload: bytes) -> None:
        self.file = SimpleNamespace(size=declared_size)
        self.payload = payload

    async def download_media(self, *, file, progress_callback=None):
        Path(file).write_bytes(self.payload)
        if progress_callback is not None:
            progress_callback(len(self.payload), self.file.size)
        return str(file)


class DownloadReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.first_started = asyncio.Event()
        self.allow_first_to_finish = asyncio.Event()
        self.attempts: list[tuple[int, int]] = []

        def ensure_headroom(_directory: Path, required: int, *, reserve_bytes: int = 0) -> None:
            self.attempts.append((required, reserve_bytes))
            # A 250-byte filesystem can hold one 100-byte source plus its
            # 100-byte working copy and a 10-byte floor, but not two at once.
            if 250 < required + reserve_bytes:
                raise DiskSpaceError("synthetic full filesystem")

        async def download(_message, target: Path, _limit: int, **_kwargs) -> Path:
            self.first_started.set()
            await self.allow_first_to_finish.wait()
            target.write_bytes(b"done")
            return target

        self.stub = SimpleNamespace(
            _download_semaphore=asyncio.Semaphore(2),
            _download_reservations=DownloadReservationLedger(),
            settings=SimpleNamespace(
                temp_min_free_bytes=10,
                download_stall_seconds=1.0,
                telegram_download_connections=1,
                download_timeout=5.0,
            ),
            deps=SimpleNamespace(
                ensure_disk_headroom=ensure_headroom,
                download_media_limited=download,
            ),
            client=None,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_second_simultaneous_admission_is_refused_when_first_reserved_bytes(self):
        first_target = self.root / "first" / "source.bin"
        second_target = self.root / "second" / "source.bin"
        first = asyncio.create_task(
            GamasBot._download_telegram_media(self.stub, _Message(100), first_target, 100)
        )
        await asyncio.wait_for(self.first_started.wait(), timeout=1.0)

        with self.assertRaises(DiskSpaceError):
            await GamasBot._download_telegram_media(self.stub, _Message(100), second_target, 100)
        self.assertEqual(self.attempts, [(200, 10), (400, 10)])

        self.allow_first_to_finish.set()
        self.assertEqual(await first, first_target)

    async def test_failed_download_releases_its_reservation_for_a_later_admission(self):
        async def fail_download(*_args, **_kwargs):
            raise RuntimeError("synthetic transfer failure")

        self.stub.deps.download_media_limited = fail_download
        target = self.root / "failed" / "source.bin"
        with self.assertRaisesRegex(RuntimeError, "synthetic transfer failure"):
            await GamasBot._download_telegram_media(self.stub, _Message(100), target, 100)

        lease = self.stub._download_reservations.reserve(
            self.root, 100, reserve_bytes=10, ensure_headroom=self.stub.deps.ensure_disk_headroom
        )
        self.assertEqual(lease.release(), 100)
        self.assertEqual(lease.release(), 0)

    async def test_cancelled_download_releases_its_reservation_for_a_later_admission(self):
        target = self.root / "cancelled" / "source.bin"
        task = asyncio.create_task(
            GamasBot._download_telegram_media(self.stub, _Message(100), target, 100)
        )
        await asyncio.wait_for(self.first_started.wait(), timeout=1.0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        lease = self.stub._download_reservations.reserve(
            self.root, 100, reserve_bytes=10, ensure_headroom=self.stub.deps.ensure_disk_headroom
        )
        self.assertEqual(lease.release(), 100)

    async def test_stream_larger_than_declared_size_aborts_and_removes_temp_file(self):
        target = self.root / "declared-overflow.bin"
        message = _OversizedStreamMessage(4, b"eight!!!")
        with self.assertRaises(UploadTooLargeError):
            await _download_media_limited(message, target, 10)
        self.assertFalse(target.exists())

    async def test_stream_larger_than_maximum_aborts_and_removes_temp_file(self):
        target = self.root / "maximum-overflow.bin"
        message = _OversizedStreamMessage(20, b"eleven-bytes")
        with self.assertRaises(UploadTooLargeError):
            await _download_media_limited(message, target, 10)
        self.assertFalse(target.exists())
