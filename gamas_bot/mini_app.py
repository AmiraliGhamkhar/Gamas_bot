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
import secrets
import time
from email.utils import formatdate, parsedate_to_datetime
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

#: Longest serialized structured document the editor may save. Larger than the
#: text budget because the JSON envelope repeats field names per block; the
#: canonical model itself still bounds every text field.
MAX_STRUCTURED_JSON_CHARS = 400_000

#: Most sections one edited document may carry (the compile bound for chunks).
MAX_EDIT_SECTIONS = 200

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
    a database, and what keeps ``sqlite3`` out of a request handler. The Gamas
    2.0 additions have defaults so an older deployment (or a test double) keeps
    the same API without losing the Home or revision endpoints entirely.
    """

    leaderboard: Callable[[int], list[dict[str, Any]]]
    rank: Callable[[int], dict[str, int]]
    booklets: Callable[[int, int], list[dict[str, Any]]]
    booklet: Callable[[int, int], dict[str, Any] | None]
    save_booklet: Callable[[int, int, str], bool]
    profile: Callable[[int], dict[str, Any]] = field(
        default=lambda _user_id: {}
    )
    leaderboard_window: Callable[[int, str], list[dict[str, Any]]] | None = None
    rank_window: Callable[[int, str], dict[str, int]] | None = None
    revisions: Callable[[int, int, int], list[dict[str, Any]]] = field(
        default=lambda _user_id, _item_id, _limit: []
    )
    restore_revision: Callable[[int, int, int], bool] = field(
        default=lambda _user_id, _item_id, _revision_id: False
    )
    speaker_turns: Callable[[int, int], list[dict[str, Any]]] = field(
        default=lambda _user_id, _submission_id: []
    )
    rename_speaker: Callable[..., bool] = field(
        default=lambda _user_id, _submission_id, _label, _name: False
    )
    #: Gamas 3.0 additions. Defaults keep an older deployment (or a test
    #: double) on the legacy text-only path instead of losing the endpoints.
    submissions: Callable[[int, int], list[dict[str, Any]]] = field(
        default=lambda _user_id, _limit: []
    )
    save_structured: Callable[..., dict[str, Any] | None] = field(
        default=lambda _user_id, _item_id, _notes: None
    )
    export_bytes: Callable[[int, int, str], tuple[bytes, str, str] | None] = field(
        default=lambda _user_id, _item_id, _kind: None
    )


LEADERBOARD_WINDOWS = ("all", "weekly", "monthly")


def resolve_leaderboard_window(value: object) -> str:
    """Only the three documented windows; anything else is all-time."""
    text = str(value or "all").strip().lower()
    return text if text in LEADERBOARD_WINDOWS else "all"


def leaderboard_payload(
    data: MiniAppData, user_id: int, limit: int = 20, window: str = "all"
) -> dict:
    """The ranking screen: the top rows plus where *this* user stands."""
    window = resolve_leaderboard_window(window)
    bounded = max(1, min(50, limit))
    if window in ("weekly", "monthly") and data.leaderboard_window is not None:
        rows = data.leaderboard_window(bounded, window)
        mine = (
            data.rank_window(user_id, window)
            if data.rank_window is not None
            else {"rank": 0, "points": 0}
        )
    else:
        rows = data.leaderboard(bounded)
        mine = data.rank(user_id)
    return {
        "window": window,
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


def home_payload(data: MiniAppData, user_id: int) -> dict:
    """The Home tab: the caller's own, non-private summary.

    No other user's data, no phone numbers, no file contents. It is intentionally
    small: one number (credits), one progress signal (points) and the two things
    a student can do next.
    """
    profile = data.profile(user_id) or {}
    return {
        "first_name": str(profile.get("first_name") or ""),
        "credits": int(profile.get("credits") or 0),
        "unlimited": bool(profile.get("unlimited", False)),
        "quiz_points": int(profile.get("quiz_points") or 0),
        "library_count": int(profile.get("library_count") or 0),
        "submission_count": int(profile.get("submission_count") or 0),
        "artifact_count": int(profile.get("artifact_count") or 0),
        "tabs": ["home", "leaderboard", "studio"],
    }


def revisions_payload(
    data: MiniAppData, user_id: int, item_id: int, limit: int = 20
) -> dict:
    """Revision history of one booklet the caller owns."""
    return {
        "items": data.revisions(user_id, item_id, max(1, min(50, limit))),
    }


def submissions_payload(data: MiniAppData, user_id: int, limit: int = 30) -> dict:
    """The Studio overview: the caller's submissions and what each offers."""
    return {"items": data.submissions(user_id, max(1, min(100, limit)))}


def speakers_payload(data: MiniAppData, user_id: int, submission_id: int) -> dict:
    """Anonymous speaker turns of one submission (no identity is inferred)."""
    if submission_id <= 0:
        raise AppError(400, "submission id is required")
    turns = data.speaker_turns(user_id, submission_id)
    labels = sorted(
        {str(turn["speaker_label"]) for turn in turns if turn.get("speaker_label")}
    )
    speakers: list[dict[str, Any]] = []
    for label in labels:
        own = [turn for turn in turns if str(turn.get("speaker_label")) == label]
        starts = [
            float(turn["start"]) for turn in own if turn.get("start") is not None
        ]
        ends = [float(turn["end"]) for turn in own if turn.get("end") is not None]
        speakers.append(
            {
                "label": label,
                "speaker_id": str(own[0].get("speaker_id") or f"speaker_{label}"),
                "default_name": str(
                    own[0].get("default_name") or own[0].get("display_name") or label
                ),
                "custom_name": own[0].get("custom_name") or None,
                "role": str(own[0].get("role") or ""),
                "segments": len(own),
                "first_start": min(starts) if starts else None,
                "last_end": max(ends) if ends else None,
            }
        )
    return {
        "submission_id": submission_id,
        "labels": labels,
        "speakers": speakers,
        "turns": turns,
        "unknown_attribution": any(not turn.get("speaker_label") for turn in turns),
    }


def rename_speaker_payload(
    data: MiniAppData,
    user_id: int,
    submission_id: int,
    label: str,
    name: str,
    role: object = None,
) -> dict:
    """Persist a user-chosen name (and optional role) for one anonymous label."""
    if submission_id <= 0 or not label or not name:
        raise AppError(400, "submission id, label and name are required")
    clean_name = " ".join(str(name).split())[:64]
    if not clean_name:
        raise AppError(400, "name is empty")
    clean_role: str | None = None
    if role is not None and not isinstance(role, str):
        raise AppError(400, "role must be text")
    if isinstance(role, str):
        clean_role = " ".join(role.split())[:64]
    # The four-argument call is kept for callers (and older test doubles) that
    # predate the optional role; only an explicit role reaches the new path.
    if clean_role is None:
        renamed = data.rename_speaker(user_id, submission_id, str(label), clean_name)
    else:
        renamed = data.rename_speaker(
            user_id, submission_id, str(label), clean_name, role=clean_role
        )
    if not renamed:
        raise AppError(404, "speaker not found")
    result: dict[str, Any] = {"ok": True, "label": str(label), "name": clean_name}
    if clean_role is not None:
        result["role"] = clean_role
    return result


def restore_revision_payload(
    data: MiniAppData, user_id: int, item_id: int, revision_id: int
) -> dict:
    if revision_id <= 0:
        raise AppError(400, "revision id is required")
    if not data.restore_revision(user_id, item_id, revision_id):
        raise AppError(404, "revision not found")
    return {"ok": True}


def booklets_payload(data: MiniAppData, user_id: int, limit: int = 30) -> dict:
    return {"items": data.booklets(user_id, max(1, min(100, limit)))}


def booklet_payload(data: MiniAppData, user_id: int, item_id: int) -> dict:
    record = data.booklet(user_id, item_id)
    if record is None:
        raise AppError(404, "booklet not found")
    structured: dict[str, Any] | None = None
    raw = record.get("structured_json")
    if raw:
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            structured = parsed
    return {
        "id": int(item_id),
        "title": str(record.get("title") or ""),
        "text": str(record.get("content_text") or ""),
        "structured": structured,
        "editable": bool(record.get("editable", True)),
        "max_chars": MAX_BOOKLET_CHARS,
        "submission_id": record.get("submission_id"),
        "available": list(record.get("available") or []),
    }


def _sanitize_document_strings(value: object) -> object:
    """Strip characters that are invalid in XML/PDF text from every string.

    The renderers escape markup, but a raw control character (``\\x00`` …)
    would produce a Word file Office refuses to open. Everything outside
    tab/newline/carriage-return/form-feed below 0x20, plus DEL, is removed
    recursively; the structure is left untouched for validation.
    """
    if isinstance(value, str):
        return "".join(
            ch for ch in value if ch >= " " or ch in "\t\n\r\f"
        ).replace("\x7f", "")
    if isinstance(value, dict):
        return {str(key): _sanitize_document_strings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_document_strings(item) for item in value]
    return value


def save_structured_booklet_payload(
    data: MiniAppData, user_id: int, item_id: int, body: Mapping[str, Any]
) -> dict:
    """Validate and persist a structured booklet edit, then re-render files.

    The canonical model — the same one the pipeline renders — validates the
    payload: block types, required fields, table shapes and per-field size
    bounds are enforced by :meth:`StructuredNotes.from_payload` before anything
    is written. Ownership is resolved from the signed user id inside the store,
    never from the payload.
    """
    from .notes import StructuredNotes, StructuringError

    document = body.get("document")
    if not isinstance(document, dict):
        raise AppError(400, "document is required")
    sanitized = _sanitize_document_strings(document)
    if not isinstance(sanitized, dict):  # pragma: no cover - dict in, dict out
        raise AppError(400, "document is required")
    try:
        notes = StructuredNotes.from_payload(sanitized)
    except StructuringError as exc:
        raise AppError(400, str(exc) or "ساختار جزوهٔ ارسالی معتبر نیست.") from None
    if len(notes.sections) > MAX_EDIT_SECTIONS:
        raise AppError(400, "تعداد بخش‌های جزوه بیش از حد مجاز است.")
    serialized = notes.to_json()
    if len(serialized) > MAX_STRUCTURED_JSON_CHARS:
        raise AppError(413, "ساختار جزوه بیش از حد بزرگ است.")
    result = data.save_structured(user_id, item_id, notes)
    if result is None:
        raise AppError(404, "booklet not found")
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        if reason == "migration":
            raise AppError(
                503, "ویرایش ساختاری تا اعمال مهاجرت ۰۲۸ توسط بات فعال نیست."
            )
        if reason == "too_large":
            raise AppError(413, "ساختار جزوه بیش از حد بزرگ است.")
        raise AppError(400, "ذخیرهٔ جزوه ممکن نشد.")
    return {
        "ok": True,
        "revision_id": int(result.get("revision_id") or 0),
        "rendered": list(result.get("rendered") or []),
        "chars": len(notes.to_markdown()),
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
    start_response: Callable[..., Any], status: int, payload: Any, *, head: bool = False
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
    return [b"" if head else body]


def booklet_docx_bytes(data: MiniAppData, user_id: int, item_id: int) -> bytes:
    """Render the *current* booklet text to a professional RTL Word file.

    This is the legacy path for booklets that have no structured model (created
    before structured editing, or authored as plain text): the export is a fresh
    render of the edited text, never the original Telegram attachment. The
    renderer is the same ``build_plain_docx`` the bot uses, so fonts, RTL
    direction, cover and page frame are identical. Bounded by
    ``MAX_BOOKLET_CHARS`` and by the WSGI worker's own request budget.
    """
    from datetime import datetime, timezone

    from .docx_export import DocumentMeta, build_plain_docx

    record = data.booklet(user_id, item_id)
    if record is None:
        raise AppError(404, "booklet not found")
    title = str(record.get("title") or "جزوهٔ گاماس").strip() or "جزوهٔ گاماس"
    text = str(record.get("content_text") or record.get("text") or "")
    if len(text) > MAX_BOOKLET_CHARS:
        raise AppError(413, "booklet is too large to export")
    meta = DocumentMeta(
        reference=f"GMS-APP-{int(item_id)}",
        created_at=datetime.now(timezone.utc),
    )
    try:
        return build_plain_docx(title, text, meta=meta)
    except Exception as exc:  # noqa: BLE001 - a renderer failure is a 500, not a crash
        logger.exception("mini app DOCX export failed")
        raise AppError(500, "export failed") from exc


def booklet_export_payload(
    data: MiniAppData, user_id: int, item_id: int, fmt: str
) -> tuple[bytes, str, str]:
    """The booklet's current PDF or DOCX: ``(body, filename, content_type)``.

    Order of preference, per the delivery contract:

    1. the facade's persisted artifact — the exact bytes the latest save (or
       the pipeline) rendered, served without re-rendering so repeated
       downloads are free and idempotent;
    2. for a booklet without a structured model, the legacy plain-text DOCX
       render (there is no PDF without structure to render);
    3. anything else is a 404, never a silently different document.
    """
    if fmt not in {"pdf", "docx"}:
        raise AppError(400, "format must be pdf or docx")
    stored = data.export_bytes(user_id, item_id, fmt)
    if stored is not None:
        return stored
    record = data.booklet(user_id, item_id)
    if record is None:
        raise AppError(404, "booklet not found")
    if fmt == "docx":
        body = booklet_docx_bytes(data, user_id, item_id)
        return (
            body,
            f"gamas-booklet-{int(item_id)}.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    raise AppError(
        404, "این جزوه نسخهٔ ساختارمند ندارد؛ خروجی PDF برای آن موجود نیست."
    )


def _binary_response(
    start_response: Callable[..., Any],
    body: bytes,
    filename: str,
    content_type: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
) -> list[bytes]:
    """Stream one exported file with no caching: it holds the user's own text."""
    start_response(
        "200 OK",
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Content-Disposition", f'attachment; filename="{filename}"'),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ],
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
    readiness: Callable[[], Mapping[str, Any]] | None = None,
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
        started_at = time.perf_counter()
        request_id = secrets.token_hex(8)
        original_start_response = start_response

        def instrumented_start_response(status, headers, exc_info=None):
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            safe_headers = [
                (name, value)
                for name, value in headers
                if name.lower() not in {"x-request-id", "server-timing"}
            ]
            safe_headers.extend(
                [
                    ("X-Request-ID", request_id),
                    ("Server-Timing", f"app;dur={elapsed_ms:.1f}"),
                ]
            )
            logger.info(
                "mini app request id=%s method=%s path=%s status=%s duration_ms=%.1f",
                request_id,
                method,
                path,
                status.split(" ", 1)[0],
                elapsed_ms,
            )
            if exc_info is None:
                return original_start_response(status, safe_headers)
            return original_start_response(status, safe_headers, exc_info)

        start_response = instrumented_start_response
        try:
            if path == "/health":
                if method not in {"GET", "HEAD"}:
                    raise AppError(405, "method not allowed")
                status = dict(health() if health is not None else {"ok": True})
                return _json_response(start_response, 200, status, head=method == "HEAD")
            if path == "/ready":
                if method not in {"GET", "HEAD"}:
                    raise AppError(405, "method not allowed")
                status = dict(readiness() if readiness is not None else {"ok": True})
                http_status = 200 if status.get("ok", False) else 503
                return _json_response(
                    start_response, http_status, status, head=method == "HEAD"
                )
            if path == "/api/home":
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                return _json_response(
                    start_response, 200, home_payload(_resolve_data(), user.user_id)
                )
            if path == "/api/leaderboard":
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                limit = _int_field(body, "limit", 20)
                window = resolve_leaderboard_window(body.get("window"))
                return _json_response(
                    start_response, 200,
                    leaderboard_payload(_resolve_data(), user.user_id, limit, window),
                )
            if path == "/api/booklets":
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                return _json_response(
                    start_response, 200, booklets_payload(_resolve_data(), user.user_id)
                )
            if path == "/api/submissions":
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                return _json_response(
                    start_response,
                    200,
                    submissions_payload(
                        _resolve_data(), user.user_id, _int_field(body, "limit", 30)
                    ),
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
                    if isinstance(body.get("document"), dict):
                        return _json_response(
                            start_response,
                            200,
                            save_structured_booklet_payload(
                                _resolve_data(), user.user_id, item_id, body
                            ),
                        )
                    return _json_response(
                        start_response,
                        200,
                        save_booklet_payload(_resolve_data(), user.user_id, item_id, body),
                    )
                return _json_response(
                    start_response, 200, booklet_payload(_resolve_data(), user.user_id, item_id)
                )
            if path == "/api/booklet/export":
                # POST carries the signed initData in a JSON body; the response
                # is a binary file, not JSON. A browser cannot attach
                # Telegram's signature to a plain link, so the frontend fetches
                # and saves the blob itself.
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                item_id = _int_field(body, "id", 0)
                if item_id <= 0:
                    raise AppError(400, "id is required")
                fmt = str(body.get("format") or "docx").strip().lower()
                document, filename, content_type = booklet_export_payload(
                    _resolve_data(), user.user_id, item_id, fmt
                )
                return _binary_response(
                    start_response, document, filename, content_type
                )
            if path == "/api/revisions":
                if method != "POST":
                    raise AppError(405, "method not allowed")
                body = _json_body_cached(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                item_id = _int_field(body, "id", 0)
                if item_id <= 0:
                    raise AppError(400, "id is required")
                return _json_response(
                    start_response, 200,
                    revisions_payload(
                        _resolve_data(), user.user_id, item_id,
                        _int_field(body, "limit", 20),
                    ),
                )
            if path == "/api/revision":
                if method != "PUT":
                    raise AppError(405, "method not allowed")
                body = _json_body(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                item_id = _int_field(body, "id", 0)
                if item_id <= 0:
                    raise AppError(400, "id is required")
                return _json_response(
                    start_response, 200,
                    restore_revision_payload(
                        _resolve_data(), user.user_id, item_id,
                        _int_field(body, "revision_id", 0),
                    ),
                )
            if path == "/api/speakers":
                if method not in {"POST", "PUT"}:
                    raise AppError(405, "method not allowed")
                body = _json_body(environ)
                user = _authenticate_with(body, _resolve_token(), max_age_seconds, clock)
                submission_id = _int_field(body, "submission_id", 0)
                if method == "PUT":
                    return _json_response(
                        start_response, 200,
                        rename_speaker_payload(
                            _resolve_data(), user.user_id, submission_id,
                            str(body.get("label") or ""),
                            str(body.get("name") or ""),
                            body.get("role"),
                        ),
                    )
                return _json_response(
                    start_response, 200,
                    speakers_payload(_resolve_data(), user.user_id, submission_id),
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
    stat = candidate.stat()
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    last_modified = formatdate(stat.st_mtime, usegmt=True)
    content_type = _STATIC_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
    cache_control = (
        "no-cache" if candidate.name == "index.html" else "public, max-age=300, must-revalidate"
    )
    headers = [
        ("Content-Type", content_type),
        ("ETag", etag),
        ("Last-Modified", last_modified),
        ("Cache-Control", cache_control),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        (
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' "
            "https://telegram.org; frame-ancestors https://web.telegram.org "
            "https://*.telegram.org",
        ),
    ]
    request_etags = str(environ.get("HTTP_IF_NONE_MATCH") or "")
    if request_etags:
        matches = any(value.strip() in {etag, "*"} for value in request_etags.split(","))
    else:
        request_modified_since = str(environ.get("HTTP_IF_MODIFIED_SINCE") or "")
        try:
            since = parsedate_to_datetime(request_modified_since).timestamp()
            matches = stat.st_mtime <= since + 1
        except (TypeError, ValueError, OverflowError):
            matches = False
    if matches:
        start_response("304 Not Modified", headers)
        return [b""]

    headers.append(("Content-Length", str(len(body))))
    start_response("200 OK", headers)
    return [b"" if method == "HEAD" else body]


__all__ = [
    "AppError",
    "MAX_BOOKLET_CHARS",
    "MAX_EDIT_SECTIONS",
    "MAX_STRUCTURED_JSON_CHARS",
    "MiniAppData",
    "MiniAppUser",
    "booklet_docx_bytes",
    "booklet_export_payload",
    "booklet_payload",
    "booklets_payload",
    "create_app",
    "home_payload",
    "leaderboard_payload",
    "rename_speaker_payload",
    "resolve_leaderboard_window",
    "restore_revision_payload",
    "save_booklet_payload",
    "save_structured_booklet_payload",
    "speakers_payload",
    "revisions_payload",
    "submissions_payload",
    "verify_init_data",
]
