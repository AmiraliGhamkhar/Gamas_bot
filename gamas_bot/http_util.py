"""Small HTTP helpers shared by the provider clients.

Only the pieces that must behave identically in :mod:`gamas_bot.llm` and
:mod:`gamas_bot.stt` live here: both quote a provider error body back to the
user, and both must never load an unbounded body to do it.
"""

from __future__ import annotations

#: Enough of a provider error body to extract a useful message. Real error
#: payloads are a few hundred bytes; a misrouted gateway can answer with a
#: multi-megabyte HTML page and reading it whole is a needless allocation.
MAX_ERROR_BODY_BYTES = 64_000


async def read_error_body(response: object, *, limit: int = MAX_ERROR_BODY_BYTES) -> bytes:
    """Read at most *limit* bytes of a response body.

    ``aiohttp`` exposes a streaming ``response.content.read(n)``; the stubs the
    tests (and some local gateways) use only implement ``response.read()``, so
    that remains a fallback and is truncated afterwards.
    """
    content = getattr(response, "content", None)
    reader = getattr(content, "read", None)
    if callable(reader):
        return bytes(await reader(limit))
    read = getattr(response, "read", None)
    if read is None:  # pragma: no cover - every real response can be read
        return b""
    return bytes(await read())[:limit]
