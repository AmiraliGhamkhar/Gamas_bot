"""The Mini App's server side: a WSGI application, no framework, no ASGI.

The bot already runs on two hosts the user controls: a cPanel "Setup Python App"
(Passenger) and, optionally, a Cloudflare-proxied domain. Both speak WSGI, both
terminate TLS themselves, and neither can be assumed to keep a websocket alive.
So the Mini App is built from three primitives that every one of those hosts has
had for fifteen years:

* a WSGI callable (this module) — no extension modules, no event loop;
* ``sqlite3`` opened read-mostly with the same WAL database the bot uses;
* one JSON request in, one JSON response out.

Authenticating the caller is the interesting part. A Mini App receives
``Telegram.WebApp.initData`` — a signed query string — and the only way to trust
it is to recompute Telegram's HMAC with the bot token. That check, plus the
``auth_date`` freshness window, is what stops a stranger from reading or editing
somebody else's booklet just because they guessed a reference number.

Everything in this module is deterministic and side-effect free at import time:
the application object is built by :func:`create_app`, which makes it testable
and lets the WSGI entry point stay a three-line file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

#: Telegram re-signs ``initData`` on every launch, so an old signature means a
#: replayed request, not a slow user. Five minutes is the documented practical
#: ceiling for a Mini App session.
DEFAULT_MAX_AUTH_AGE_SECONDS = 300

#: Longest booklet body the editor may save (characters). Matches the archived
#: library text budget so an edit can never grow past what the quiz builder reads.
MAX_BOOKLET_CHARS = 200_000

#: Longest request body accepted by the WSGI app.
MAX_REQUEST_BYTES = 1_000_000

JSON_HEADERS = (("Content-Type", "application/json; charset=utf-8"),)


class AppError(Exception):
    """A request that should be answered with ``status`` and a message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Telegram initData verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MiniAppUser:
    """The Telegram identity proven by a valid ``initData`` signature."""

    user_id: int
    first_name: str = ""
    username: str = ""
    language_code: str = ""
    auth_date: int = 0


def _secret_key(bot_token: str) -> bytes:
    """``HMAC_SHA256(key="WebAppData", msg=bot_token)``, per Telegram's docs."""
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def verify_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int = DEFAULT_MAX_AUTH_AGE_SECONDS,
    now: float | None = None,
) -> MiniAppUser:
    """Prove that ``init_data`` was signed by Telegram for this bot.

    The signature covers the data-check-string: every field except ``hash``,
    sorted by name, joined as ``name=value`` with newlines. ``signature`` (the
    newer Ed25519 third-party field) is excluded too, because Telegram does not
    include it in the HMAC it computes for bots.

    Raises :class:`AppError` with 401 for anything that does not verify. The
    message deliberately does not say *which* check failed: a prober should not
    learn whether the signature or the clock was the problem.
    """
    if not bot_token:
        raise AppError(500, "mini app is not configured")
    if not init_data:
        raise AppError(401, "missing Telegram credentials")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise AppError(401, "malformed Telegram credentials") from exc
    fields: dict[str, str] = {}
    for name, value in pairs:
        # Later duplicates win, as they do in Telegram's own client parser.
        fields[name] = value
    received_hash = fields.pop("hash", "")
    fields.pop("signature", None)
    if not received_hash:
        raise AppError(401, "unsigned request")

    check_string = "\n".join(f"{name}={fields[name]}" for name in sorted(fields))
    expected = hmac.new(
        _secret_key(bot_token), check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise AppError(401, "signature does not match")

    current = time.time() if now is None else now
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError as exc:
        raise AppError(401, "malformed auth date") from exc
    if auth_date <= 0:
        raise AppError(401, "missing auth date")
    if current - auth_date > max_age_seconds:
        raise AppError(401, "expired credentials")
    if auth_date - current > 60:
        # A future-dated payload is a sign of a tampered or skewed client.
        raise AppError(401, "credentials from the future")

    try:
        user = json.loads(fields.get("user", "{}"))
    except ValueError as exc:
        raise AppError(401, "malformed user payload") from exc
    if not isinstance(user, dict):
        raise AppError(401, "malformed user payload")
    try:
        user_id = int(user.get("id", 0))
    except (TypeError, ValueError) as exc:
        raise AppError(401, "malformed user payload") from exc
    if user_id <= 0:
        raise AppError(401, "malformed user payload")
    return MiniAppUser(
        user_id=user_id,
        first_name=str(user.get("first_name") or "")[:64],
        username=str(user.get("username") or "")[:64],
        language_code=str(user.get("language_code") or "")[:16],
        auth_date=auth_date,
    )


# ---------------------------------------------------------------------------
# The data the app exposes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MiniAppData:
    """Everything the handlers need from SQLite, behind one small surface.

    The concrete implementation is in :mod:`gamas_bot.mini_app_store`; keeping it
    behind a protocol-shaped class is what lets the HTTP layer be tested without
    a database, and what keeps ``sqlite3`` out of a request handler.
    """

    leaderboard: Callable[[int], list[dict[str, Any]]]
    rank: Callable[[int], dict[str, int]]
    booklets: Callable[[int, int], list[dict[str, Any]]]
    booklet: Callable[[int, int], dict[str, Any] | None]
    save_booklet: Callable[[int, int, str], bool]
    profile: Callable[[int], dict[str, Any]] = field(
        default=lambda _user_id: {}
    )


def leaderboard_payload(data: MiniAppData, user_id: int, limit: int = 20) -> dict:
    """The ranking screen: the top rows plus where *this* user stands."""
    rows = data.leaderboard(max(1, min(50, limit)))
    mine = data.rank(user_id)
    return {
        "rows": [
            {
                "rank": index + 1,
                "name": str(row.get("first_name") or "دانشجو"),
                "points": int(row.get("quiz_points") or 0),
            }
            for index, row in enumerate(rows)
        ],
        "me": {"rank": int(mine.get("rank") or 0), "points": int(mine.get("points") or 0)},
    }


def booklets_payload(data: MiniAppData, user_id: int, limit: int = 30) -> dict:
    return {"items": data.booklets(user_id, max(1, min(100, limit)))}


def booklet_payload(data: MiniAppData, user_id: int, item_id: int) -> dict:
    record = data.booklet(user_id, item_id)
    if record is None:
        raise AppError(404, "booklet not found")
    return {
        "id": int(item_id),
        "title": str(record.get("title") or ""),
        "text": str(record.get("content_text") or ""),
        "editable": bool(record.get("editable", True)),
        "max_chars": MAX_BOOKLET_CHARS,
    }


def save_booklet_payload(
    data: MiniAppData, user_id: int, item_id: int, body: Mapping[str, Any]
) -> dict:
    """Persist an edited booklet, refusing anything that is not text.

    Only the text cache is written. The Word file that was delivered stays in
    Telegram untouched — re-rendering it here would mean running python-docx
    inside a Passenger worker on every save, and a user who edits a booklet in
    the app expects the *booklet* (the text their quiz and flashcards are built
    from) to change, not the file they already downloaded.
    """
    text = body.get("text")
    if not isinstance(text, str):
        raise AppError(400, "text is required")
    if len(text) > MAX_BOOKLET_CHARS:
        raise AppError(413, "text is too long")
    if not text.strip():
        raise AppError(400, "text is empty")
    if not data.save_booklet(user_id, item_id, text):
        raise AppError(404, "booklet not found")
    return {"ok": True, "chars": len(text)}


# ---------------------------------------------------------------------------
# The WSGI application
# ---------------------------------------------------------------------------


def _json_response(
    start_response: Callable[..., Any], status: int, payload: Any
) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    reasons = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }
    start_response(
        f"{status} {reasons.get(status, 'Error')}",
        [*JSON_HEADERS, ("Content-Length", str(len(body))), ("Cache-Control", "no-store")],
    )
    return [body]


def _read_body(environ: Mapping[str, Any]) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        raise AppError(400, "malformed request length") from None
    if length < 0:
        raise AppError(400, "malformed request length")
    if length > MAX_REQUEST_BYTES:
        raise AppError(413, "request body is too large")
    stream = environ.get("wsgi.input")
    if length == 0 or stream is None:
        return b""
    data = stream.read(length)
    if not isinstance(data, bytes):
        data = bytes(data or b"")
    return data


def _json_body(environ: Mapping[str, Any]) -> dict[str, Any]:
    raw = _read_body(environ)
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AppError(400, "request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise AppError(400, "request body must be a JSON object")
    return payload


def create_app(
    *,
    data: MiniAppData | Callable[[], MiniAppData],
    bot_token: str | Callable[[], str],
    max_age_seconds: int = DEFAULT_MAX_AUTH_AGE_SECONDS,
    now: Callable[[], float] | None = None,
    health: Callable[[], Mapping[str, Any]] | None = None,
    static_dir: str | None = None,
) -> Callable[..., Iterable[bytes]]:
    """Build the WSGI callable.

    ``data`` and ``bot_token`` may be callables so a Passenger worker that was
    pre-forked before ``.env`` was loaded still resolves them per request.
    """
    clock = now or time.time

    def _resolve_data() -> MiniAppData:
        return data() if callable(data) else data

    def _resolve_token() -> str:
        return bot_token() if callable(bot_token) else bot_token

    def app(environ: Mapping[str, Any], start_response: Callable[..., Any]):
        path = str(environ.get("PATH_INFO") or "/").rstrip("/") or "/"
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        try:
            if path == "/health":
                if method not in {"GET", "HEAD"}:
                    raise AppError(405, "method not allowed")
                status = dict(health() if health is not None else {"ok": True})
                return _json_response(start_response, 200, status)
            if path == "/api/leaderboard":
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                limit = _int_field(body, "limit", 20)
                return _json_response(
                    start_response, 200, leaderboard_payload(_resolve_data(), user.user_id, limit)
                )
            if path == "/api/booklets":
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                return _json_response(
                    start_response, 200, booklets_payload(_resolve_data(), user.user_id)
                )
            if path == "/api/booklet":
                if method not in {"POST", "PUT"}:
                    raise AppError(405, "method not allowed")
                body = _json_body(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                item_id = _int_field(body, "id", 0)
                if item_id <= 0:
                    raise AppError(400, "id is required")
                if method == "PUT":
                    return _json_response(
                        start_response,
                        200,
                        save_booklet_payload(_resolve_data(), user.user_id, item_id, body),
                    )
                return _json_response(
                    start_response, 200, booklet_payload(_resolve_data(), user.user_id, item_id)
                )
            if path in {"/", "/index.html"}:
                if method not in {"GET", "HEAD"}:
                    raise AppError(405, "method not allowed")
                return _static(start_response, environ, method, "index.html", static_dir)
            if path.startswith("/static/"):
                if method not in {"GET", "HEAD"}:
                    raise AppError(405, "method not allowed")
                return _static(start_response, environ, method, path[len("/static/") :], static_dir)
            raise AppError(404, "not found")
        except AppError as exc:
            if exc.status >= 500:
                logger.error("mini app error path=%s status=%s", path, exc.status)
            return _json_response(start_response, exc.status, {"error": exc.message})
        except Exception:  # noqa: BLE001 - a WSGI app must never raise
            logger.exception("mini app crashed path=%s", path)
            return _json_response(start_response, 500, {"error": "internal error"})

    return app


def _json_body_cached(environ: Mapping[str, Any]) -> dict[str, Any]:
    """Read the body once per request (``wsgi.input`` is a one-shot stream)."""
    cached = environ.get("gamas.mini_app.body")
    if isinstance(cached, dict):
        return cached
    body = _json_body(environ)
    environ["gamas.mini_app.body"] = body  # type: ignore[index]
    return body


def _authenticate_with(
    body: Mapping[str, Any],
    bot_token: str,
    max_age_seconds: int,
    clock: Callable[[], float],
) -> MiniAppUser:
    init_data = body.get("initData")
    if not isinstance(init_data, str) or not init_data:
        raise AppError(401, "missing Telegram credentials")
    return verify_init_data(
        init_data,
        bot_token=bot_token,
        max_age_seconds=max_age_seconds,
        now=clock(),
    )


def _int_field(body: Mapping[str, Any], name: str, default: int) -> int:
    value = body.get(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AppError(400, f"{name} must be an integer") from None


_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".woff2": "font/woff2",
}


def _static(
    start_response: Callable[..., Any],
    environ: Mapping[str, Any],
    method: str,
    relative: str,
    static_dir: str | None,
) -> list[bytes]:
    """Serve the compiled Mini App from disk, refusing anything outside it."""
    from pathlib import Path

    if not static_dir:
        raise AppError(404, "the mini app bundle is not installed")
    root = Path(static_dir).resolve()
    candidate = (root / relative.lstrip("/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise AppError(404, "not found") from None
    if not candidate.is_file():
        raise AppError(404, "not found")
    body = candidate.read_bytes()
    content_type = _STATIC_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
    start_response(
        "200 OK",
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            # The Mini App shell is versioned with the deployment; a short cache
            # keeps a redeploy visible without hammering the Passenger worker.
            ("Cache-Control", "public, max-age=300"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            (
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self' "
                "https://telegram.org; frame-ancestors https://web.telegram.org "
                "https://*.telegram.org",
            ),
        ],
    )
    del environ, method  # documented parameters, unused by the file server
    return [body]


def default_now() -> float:
    """Indirection so tests can freeze the clock without patching ``time``."""
    return time.time()


__all__ = [
    "AppError",
    "MAX_BOOKLET_CHARS",
    "MiniAppData",
    "MiniAppUser",
    "booklet_payload",
    "booklets_payload",
    "create_app",
    "leaderboard_payload",
    "save_booklet_payload",
    "verify_init_data",
]
