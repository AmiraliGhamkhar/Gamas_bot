"""Telegram MTProto transfer primitives, byte-range downloads, and disk headroom (SRP / DIP).

Separates low-level Telegram file transport, parallel MTProto byte-range
fetching, stall watchdogs, and temporary disk-headroom enforcement from high-level
bot workflows (:mod:`gamas_bot.bot`).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from typing import Any

from .fileio import AsyncFileWriter, run_file_io, run_file_io_resource

logger = logging.getLogger("gamas_bot.bot.transfer")


def _max_mtproto_part_size_kb(file_size: int) -> int:
    """Return 512 KiB (MTProto's maximum part size) for all positive file sizes."""
    if file_size <= 0:
        return 64
    return 512


def _configure_telethon_part_size() -> None:
    try:
        from telethon import utils as telethon_utils

        telethon_utils.get_appropriated_part_size = _max_mtproto_part_size_kb
    except Exception:
        logger.debug("could not configure Telethon MTProto part size", exc_info=True)


_configure_telethon_part_size()


class UploadTooLargeError(ValueError):
    """A Telegram transfer exceeded its configured byte ceiling."""


class DownloadStalledError(ValueError):
    """A Telegram download produced no bytes for the configured stall window."""


class DiskSpaceError(ValueError):
    """The temporary filesystem cannot hold the job's files."""


TELEGRAM_DOWNLOAD_CHUNK_BYTES = 512 * 1024


def _monotonic() -> float:
    bot_mod = sys.modules.get("gamas_bot.bot")
    time_mod = getattr(bot_mod, "time", time)
    return float(time_mod.monotonic())


def _reported_upload_bytes(message: Any, fallback: int) -> int:
    """The upload's byte size as Telegram reported it, or *fallback*."""
    reported = getattr(getattr(message, "file", None), "size", None)
    return reported if isinstance(reported, int) and reported > 0 else fallback


def _ensure_disk_headroom(
    directory: Path, required_bytes: int, *, reserve_bytes: int = 0
) -> None:
    """Refuse a download that would leave the temp filesystem below its floor."""
    free = shutil.disk_usage(directory).free
    if free < required_bytes + reserve_bytes:
        logger.warning(
            "temp disk headroom refused: free=%d required=%d reserve=%d dir=%s",
            free,
            required_bytes,
            reserve_bytes,
            directory,
        )
        raise DiskSpaceError(
            "فضای موقت سرور برای این فایل کافی نیست؛ لطفاً کمی بعد دوباره بفرستید."
        )


def _message_file_size(message: Any) -> int | None:
    """The declared size of a message's file, or ``None`` when unknown."""
    size = getattr(getattr(message, "file", None), "size", None)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return size


def plan_download_ranges(total_bytes: int, connections: int) -> list[tuple[int, int]]:
    """Split a file into ``(offset, chunk_count)`` ranges Telethon fetches directly."""
    chunk = TELEGRAM_DOWNLOAD_CHUNK_BYTES
    total_chunks = max(1, math.ceil(total_bytes / chunk))
    wanted = max(1, min(int(connections), total_chunks))
    per = 1
    while per * 2 <= max(1, total_chunks // wanted):
        per *= 2
    if wanted == 1:
        return [(0, total_chunks)]
    head = total_chunks - per * (wanted - 1)
    ranges = [(0, head)]
    for index in range(1, wanted):
        ranges.append(((head + (index - 1) * per) * chunk, per))
    return ranges


def _pwrite_all(descriptor: int, payload: bytes, offset: int) -> None:
    """Write an entire range chunk, including on a short positional write."""
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.pwrite(descriptor, view[written:], offset + written)
        if count <= 0:
            raise OSError("short positional write while saving Telegram download")
        written += count


async def _download_media_ranges(
    client: Any,
    message: Any,
    target: Path,
    limit_bytes: int,
    *,
    connections: int,
    stall_seconds: float,
) -> None:
    """Download one file as concurrent byte ranges, each writing its own region."""
    size = _message_file_size(message)
    if size is None:
        raise RuntimeError("telegram did not report a file size")
    if size > limit_bytes:
        raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
    ranges = plan_download_ranges(size, connections)
    last_progress = [_monotonic()]
    written = [0]
    descriptor = await run_file_io_resource(
        os.open,
        os.close,
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )

    async def fetch(offset: int, chunks: int) -> None:
        position = offset
        end = min(size, offset + chunks * TELEGRAM_DOWNLOAD_CHUNK_BYTES)
        async for piece in client.iter_download(
            message.media,
            offset=offset,
            limit=chunks,
            request_size=TELEGRAM_DOWNLOAD_CHUNK_BYTES,
            file_size=size,
        ):
            payload = bytes(piece)
            if not payload or position + len(payload) > end:
                raise RuntimeError("Telegram download returned an invalid range")
            await run_file_io(_pwrite_all, descriptor, payload, position)
            position += len(payload)
            written[0] += len(payload)
            last_progress[0] = _monotonic()
            if written[0] > limit_bytes:
                raise UploadTooLargeError(
                    f"file exceeds limit ({limit_bytes} bytes)"
                )
        if position != end:
            raise RuntimeError(f"Telegram download ended at {position}, expected {end}")

    tasks = [
        asyncio.create_task(fetch(offset, chunks)) for offset, chunks in ranges
    ]
    try:
        if stall_seconds > 0:
            tick = max(0.05, min(stall_seconds / 4, 15.0))
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=tick, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    error = finished.exception()
                    if error is not None:
                        raise error
                if pending and _monotonic() - last_progress[0] > stall_seconds:
                    raise DownloadStalledError(
                        "دانلود فایل از تلگرام متوقف شد؛ لطفاً دوباره بفرستید."
                    )
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await run_file_io(os.close, descriptor)
    actual_size = (await run_file_io(target.stat)).st_size
    if actual_size != size:
        raise RuntimeError(
            f"ranged download produced {actual_size} of {size} bytes"
        )
    logger.info(
        "ranged download done bytes=%d connections=%d ranges=%d",
        size,
        connections,
        len(ranges),
    )


async def _download_media_limited(
    message: Any,
    target: Path,
    limit_bytes: int,
    *,
    stall_seconds: float = 0.0,
    client: Any = None,
    connections: int = 1,
) -> Path:
    """Download Telegram media with size, stall and event-loop I/O bounds."""
    if limit_bytes <= 0:
        raise ValueError("download limit must be positive")
    declared_size = _message_file_size(message)
    if declared_size is not None and declared_size > limit_bytes:
        raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")

    supports_async_download = client is not None and callable(
        getattr(client, "iter_download", None)
    )
    ranged = (
        supports_async_download
        and connections >= 1
        and hasattr(os, "pwrite")
        and declared_size is not None
    )
    if ranged:
        try:
            await _download_media_ranges(
                client,
                message,
                target,
                limit_bytes,
                connections=connections,
                stall_seconds=stall_seconds,
            )
            return target
        except (UploadTooLargeError, DownloadStalledError):
            await run_file_io(target.unlink, missing_ok=True)
            raise
        except Exception as exc:
            logger.warning(
                "ranged download failed (%s: %s); retrying single-stream",
                type(exc).__name__,
                exc,
            )
            await run_file_io(target.unlink, missing_ok=True)

    last_progress = [_monotonic()]

    def _guard(received: int, total: int) -> None:
        last_progress[0] = _monotonic()
        if total and total > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
        if received > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")

    writer = await AsyncFileWriter.open(target) if supports_async_download else None
    file_target: Any = writer if writer is not None else str(target)
    task: asyncio.Task[Any] | None = None
    try:

        async def _download() -> Any:
            return await message.download_media(
                file=file_target, progress_callback=_guard
            )

        task = asyncio.create_task(_download())
        if stall_seconds > 0:
            tick = max(0.05, min(stall_seconds / 4, 15.0))
            while True:
                done, _pending = await asyncio.wait((task,), timeout=tick)
                if done:
                    break
                idle = _monotonic() - last_progress[0]
                if idle > stall_seconds:
                    raise DownloadStalledError(
                        "دانلود فایل از تلگرام متوقف شد؛ لطفاً دوباره بفرستید."
                    )
        downloaded = await task
        try:
            file_stat = await run_file_io(target.stat)
        except OSError as exc:
            raise RuntimeError("download did not produce a file") from exc
        if not downloaded or not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError("download did not produce a file")
        if file_stat.st_size > limit_bytes:
            raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
        return target
    except BaseException:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if writer is not None:
            try:
                await writer.aclose()
            except OSError:
                pass
            writer = None
        try:
            await run_file_io(target.unlink, missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if writer is not None:
            await writer.aclose()
