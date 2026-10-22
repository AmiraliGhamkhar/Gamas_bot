"""Small HTTP helpers shared by the provider clients.

Only the pieces that must behave identically in :mod:`gamas_bot.llm` and
:mod:`gamas_bot.stt` live here: both quote a provider error body back to the
user, and both must never load an unbounded body to do it.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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


def sanitize_error_text(value: object, limit: int = 180) -> str:
    """Collapse whitespace and bound a provider-supplied error string."""
    return " ".join(str(value).split())[:limit]


def _parse_retry_value(raw: object) -> float | None:
    """Parse one Retry-After / RetryInfo / RateLimit-Reset value to seconds."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    # Google RPC RetryInfo format: "42s" or "12.5s".
    if text.endswith("s") and text[:-1].replace(".", "", 1).isdigit():
        text = text[:-1]
    try:
        value = float(text)
        if math.isfinite(value):
            now = datetime.now(timezone.utc).timestamp()
            if value > 1_000_000_000_000:
                # OpenRouter X-RateLimit-Reset is Unix epoch in milliseconds.
                value = (value / 1000.0) - now
            elif value > 1_000_000_000:
                # Unix epoch in seconds.
                value = value - now
            return min(max(value, 0.0), 86_400.0)
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(text)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return min(
            max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0),
            86_400.0,
        )
    except (TypeError, ValueError, OverflowError):
        return None


def retry_after_seconds(headers, payload: object = None) -> float | None:
    """Extract a bounded retry delay (seconds) from HTTP headers or error JSON."""
    if headers:
        for header_name in (
            "Retry-After",
            "retry-after",
            "X-RateLimit-Reset",
            "x-ratelimit-reset",
        ):
            parsed = _parse_retry_value(headers.get(header_name, ""))
            if parsed is not None:
                return parsed
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                meta_headers = metadata.get("headers")
                if isinstance(meta_headers, dict):
                    for key in (
                        "Retry-After",
                        "retry-after",
                        "X-RateLimit-Reset",
                        "x-ratelimit-reset",
                    ):
                        parsed = _parse_retry_value(meta_headers.get(key, ""))
                        if parsed is not None:
                            return parsed
            details = error.get("details")
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict) and "retryDelay" in item:
                        parsed = _parse_retry_value(item.get("retryDelay"))
                        if parsed is not None:
                            return parsed
    return None
