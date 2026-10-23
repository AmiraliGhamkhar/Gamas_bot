"""Small asynchronous file adapters for asyncio-only network clients.

Telethon and aiohttp accept file-like streams, but their ordinary path/IOBase
adapters call ``read`` and ``write`` synchronously from the event loop. These
adapters move each bounded disk operation to asyncio's shared (bounded) thread
pool and preserve cancellation until an in-progress system call has finished.
No full media file is buffered in memory.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, BinaryIO, TypeVar, cast

from aiohttp.payload import AsyncIterablePayload

T = TypeVar("T")
FILE_IO_CHUNK_BYTES = 256 * 1024


async def run_file_io(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one blocking filesystem call in the shared executor.

    ``asyncio.to_thread`` cannot stop a system call already running in its
    worker when the awaiting task is cancelled. Shield and join that bounded
    operation before returning cancellation so callers can safely close or
    unlink the file afterwards.
    """
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        try:
            await operation
        except Exception:
            # Cancellation remains the public outcome; the owning operation
            # handles its normal error path on the next call.
            pass
        raise


async def run_file_io_resource(
    function: Callable[..., T],
    cleanup: Callable[[T], Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Acquire a file/descriptor in the executor and dispose it on cancellation."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        try:
            resource = await operation
        except Exception:
            pass
        else:
            try:
                await run_file_io(cleanup, resource)
            except Exception:
                pass
        raise


class AsyncFileWriter:
    """A sequential, async ``write`` stream understood by Telethon downloads."""

    def __init__(self, path: Path, handle: Any) -> None:
        self.name = str(path)
        self._handle = handle
        self._position = 0
        self._closed = False

    @classmethod
    async def open(cls, path: Path) -> AsyncFileWriter:
        handle = await run_file_io_resource(
            path.open, lambda stream: stream.close(), "wb", buffering=0
        )
        return cls(path, handle)

    async def write(self, data: bytes | bytearray | memoryview) -> int:
        if self._closed:
            raise ValueError("write to closed async file")
        view = memoryview(data)
        written_total = 0
        while written_total < len(view):
            written = await run_file_io(self._handle.write, view[written_total:])
            if not written:
                raise OSError("short write while saving Telegram download")
            written_total += written
        self._position += written_total
        return written_total

    def tell(self) -> int:
        return self._position

    def flush(self) -> None:
        # The handle is opened unbuffered, so each completed write has already
        # reached the OS. Telethon's download loop calls flush synchronously.
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await run_file_io(self._handle.close)


class AsyncFileReader(io.IOBase):
    """A named async ``read`` stream understood by Telethon's upload helpers."""

    def __init__(self, path: Path, handle: Any) -> None:
        super().__init__()
        self.name = path.name
        self._handle = handle
        self._closed = False

    @classmethod
    async def open(cls, path: Path) -> AsyncFileReader:
        handle = await run_file_io_resource(
            path.open, lambda stream: stream.close(), "rb", buffering=0
        )
        return cls(path, handle)

    def readable(self) -> bool:
        return not self._closed

    def seekable(self) -> bool:
        # Telethon receives file_size explicitly; a synchronous seek would
        # force its photo-resize helper to read this async stream incorrectly.
        return False

    async def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("read from closed async file")
        return await run_file_io(self._handle.read, size)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await run_file_io(self._handle.close)
        finally:
            super().close()


async def _file_chunks(path: Path, size: int, chunk_size: int) -> AsyncIterator[bytes]:
    handle = cast(
        BinaryIO,
        await run_file_io_resource(
            path.open, lambda stream: stream.close(), "rb", buffering=0
        ),
    )
    remaining = size
    try:
        while remaining:
            chunk = await run_file_io(handle.read, min(chunk_size, remaining))
            if not chunk:
                raise OSError("file changed while an upload was being streamed")
            remaining -= len(chunk)
            yield chunk
        # Detect a file that grew after its size was measured rather than
        # silently sending a truncated prefix under the original Content-Length.
        if await run_file_io(handle.read, 1):
            raise OSError("file grew while an upload was being streamed")
    finally:
        await run_file_io(handle.close)


class AsyncFilePayload(AsyncIterablePayload):
    """Sized aiohttp payload that streams a path through bounded thread reads."""

    def __init__(
        self,
        path: Path,
        size: int,
        *,
        content_type: str = "application/octet-stream",
        chunk_size: int = FILE_IO_CHUNK_BYTES,
    ) -> None:
        if size < 0:
            raise ValueError("file payload size must not be negative")
        super().__init__(
            _file_chunks(path, size, max(1, chunk_size)),
            filename=path.name,
            content_type=content_type,
        )
        # AsyncIterablePayload has no public size argument. Setting the payload
        # size lets aiohttp calculate Content-Length without reading the file
        # into memory or switching provider uploads to chunked transfer.
        self._size = size

    @classmethod
    async def from_path(
        cls,
        path: Path,
        *,
        content_type: str = "application/octet-stream",
        chunk_size: int = FILE_IO_CHUNK_BYTES,
    ) -> AsyncFilePayload:
        stat_result = await run_file_io(path.stat)
        return cls(
            path,
            stat_result.st_size,
            content_type=content_type,
            chunk_size=chunk_size,
        )
