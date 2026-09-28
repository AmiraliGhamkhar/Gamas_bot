from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .config import Settings

logger = logging.getLogger(__name__)


class StructuringError(RuntimeError):
    pass


ERROR_DETAIL_LIMIT = 180

#: Maximum accepted lengths when normalising model-supplied strings.
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 20000


SYSTEM_PROMPT = """شما دستیار آموزشی فارسی «گاماس» هستید. ورودی شما متن پیاده‌سازی‌شدهٔ خام یک کلاس درسی است و خروجی شما یک جزوهٔ ساختارمند فارسی است.

قواعد خروجی — مطلق‌اند و استثنا ندارند:
۱) پاسخ فقط و فقط یک شیء JSON معتبر است. هیچ متن، عنوان، توضیح، علامت نقل‌قول بلوکی یا خنده‌کد (code fence) قبل یا بعد از آن ننویسید.
۲) ساختار دقیق JSON این است:
{
  "title": "عنوان کوتاه جزوه",
  "summary": "خلاصهٔ دو تا چهار جمله‌ای محتوا",
  "sections": [
    {
      "heading": "عنوان بخش",
      "paragraphs": ["پاراگراف توضیحی"],
      "bullets": ["مورد فهرستی"],
      "key_points": ["نکتهٔ کلیدی همین بخش"],
      "table": {"headers": ["ستون ۱", "ستون ۲"], "rows": [["مقدار", "مقدار"]]},
      "callouts": [{"kind": "نکته", "text": "متن برجسته"}]
    }
  ],
  "key_points": ["نکته‌های کلیدی کل جزوه"],
  "glossary": [{"term": "اصطلاح", "definition": "تعریف کوتاه"}]
}
۳) هر کلید اختیاری است؛ اگر محتوایی برایش ندارید آن را حذف کنید یا آرایه/رشتهٔ خالی بدهید. کلید تازه‌ای از خودتان نسازید.
۴) «sections» را خالی نگذارید؛ دست‌کم یک بخش با عنوان معنادار و محتوای واقعی بسازید.
۵) در callouts مقدار kind فقط یکی از این سه باشد: «نکته»، «هشدار» یا «یادآوری».
۶) JSON باید بدون خطا قابل خواندن باشد: از نقل‌قول دوگانه استفاده کنید، کامای اضافه نگذارید و خط جدید داخل رشته‌ها را با \\n بنویسید.

قواعد محتوا:
- فقط بر پایهٔ متن داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید.
- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم را در callouts یا key_points جداگانه برجسته کنید.
- مطالب را با عنوان‌های بخش کوتاه و گویا مرتب کنید.
- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از table استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.
- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.
- اگر متن ناقص یا تکراری است، مفهوم موجود را مرتب کنید و چیزی به آن نیفزایید.
"""

#: The user-message wrapper for a raw lecture transcript.
TRANSCRIPT_PROMPT = "متن پیاده‌سازی‌شدهٔ خام:\n\n"


PRESENTATION_SYSTEM_PROMPT = """شما دستیار آموزشی فارسی «گاماس» هستید. ورودی شما محتوای یک فایل ارائهٔ درسی (PowerPoint) است — شامل متن اسلایدها، یادداشت‌های گوینده و متن پیاده‌سازی‌شدهٔ صدای ضبط‌شدهٔ همان ارائه — و خروجی شما یک جزوهٔ ساختارمند فارسی است.

قواعد خروجی — مطلق‌اند و استثنا ندارند:
۱) پاسخ فقط و فقط یک شیء JSON معتبر است. هیچ متن، عنوان، توضیح یا خنده‌کد (code fence) قبل یا بعد از آن ننویسید.
۲) ساختار دقیق JSON این است:
{
  "title": "عنوان کوتاه جزوه",
  "summary": "خلاصهٔ دو تا چهار جمله‌ای محتوا",
  "sections": [
    {
      "heading": "عنوان بخش",
      "paragraphs": ["پاراگراف توضیحی"],
      "bullets": ["مورد فهرستی"],
      "key_points": ["نکتهٔ کلیدی همین بخش"],
      "table": {"headers": ["ستون ۱", "ستون ۲"], "rows": [["مقدار", "مقدار"]]},
      "callouts": [{"kind": "نکته", "text": "متن برجسته"}]
    }
  ],
  "key_points": ["نکته‌های کلیدی کل جزوه"],
  "glossary": [{"term": "اصطلاح", "definition": "تعریف کوتاه"}]
}
۳) هر کلید اختیاری است؛ اگر محتوایی برایش ندارید آن را حذف کنید یا آرایه/رشتهٔ خالی بدهید. کلید تازه‌ای از خودتان نسازید.
۴) «sections» را خالی نگذارید؛ دست‌کم یک بخش با عنوان معنادار و محتوای واقعی بسازید.
۵) در callouts مقدار kind فقط یکی از این سه باشد: «نکته»، «هشدار» یا «یادآوری».
۶) JSON باید بدون خطا قابل خواندن باشد: از نقل‌قول دوگانه استفاده کنید، کامای اضافه نگذارید و خط جدید داخل رشته‌ها را با \\n بنویسید.

قواعد محتوا:
- فقط بر پایهٔ مطالب داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید.
- ترتیب بخش‌ها را از ترتیب اسلایدها بگیرید و توضیح‌های صوتی را زیر همان موضوع اسلاید ادغام کنید.
- اگر صدا مطلبی فراتر از متن اسلاید دارد، آن را به‌عنوان توضیح کامل‌کننده بیاورید؛ مطالب تکراری را یک بار بنویسید.
- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم گوینده را در callouts یا key_points جداگانه برجسته کنید.
- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از table استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.
- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.
"""

#: The user-message wrapper for combined slide text and narration.
PRESENTATION_PROMPT = "محتوای ارائه:\n\n"

#: Appended when a first answer failed JSON validation: one bounded repair pass.
JSON_REMINDER = (
    "\n\nیادآوری مهم: پاسخ قبلی JSON معتبر نبود. این بار فقط و فقط یک شیء JSON "
    "معتبر با ساختار خواسته‌شده برگردانید؛ بدون هیچ متن، توضیح یا خنده‌کد اضافه."
)


# ---------------------------------------------------------------------------
# Structured notes model
# ---------------------------------------------------------------------------

VALID_CALLOUT_KINDS = ("نکته", "هشدار", "یادآوری")


def _bounded_text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    """Model-supplied value as a bounded, stripped string."""
    return str(value if value is not None else "").strip()[:limit]


def _string_list(value: object) -> list[str]:
    """Model-supplied list as a list of non-empty bounded strings."""
    if not isinstance(value, (list, tuple)):
        return []
    return [_bounded_text(item) for item in value if _bounded_text(item)]


@dataclass(frozen=True, slots=True)
class NoteCallout:
    kind: str
    text: str

    @classmethod
    def from_payload(cls, payload: object) -> "NoteCallout | None":
        if not isinstance(payload, dict):
            return None
        text = _bounded_text(payload.get("text"))
        if not text:
            return None
        kind = _bounded_text(payload.get("kind"), 20)
        if kind not in VALID_CALLOUT_KINDS:
            kind = "نکته"
        return cls(kind, text)


@dataclass(frozen=True, slots=True)
class NoteTable:
    headers: list[str]
    rows: list[list[str]]

    @classmethod
    def from_payload(cls, payload: object) -> "NoteTable | None":
        if not isinstance(payload, dict):
            return None
        headers = _string_list(payload.get("headers"))
        raw_rows = payload.get("rows")
        if not headers or not isinstance(raw_rows, (list, tuple)):
            return None
        rows: list[list[str]] = []
        for raw_row in raw_rows:
            if isinstance(raw_row, (list, tuple)):
                cells = [_bounded_text(cell, 4000) for cell in raw_row]
            else:
                cells = [_bounded_text(raw_row, 4000)]
            if any(cell for cell in cells):
                # Keep the row aligned with the header for rendering.
                cells = (cells + [""] * len(headers))[: len(headers)]
                rows.append(cells)
        return cls(headers, rows) if rows else None


@dataclass(frozen=True, slots=True)
class NoteSection:
    heading: str
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()
    key_points: tuple[str, ...] = ()
    table: NoteTable | None = None
    callouts: tuple[NoteCallout, ...] = ()

    @property
    def has_content(self) -> bool:
        return bool(
            self.paragraphs
            or self.bullets
            or self.key_points
            or self.table is not None
            or self.callouts
        )

    @classmethod
    def from_payload(cls, payload: object, fallback_index: int) -> "NoteSection | None":
        if not isinstance(payload, dict):
            return None
        heading = _bounded_text(payload.get("heading"), MAX_TITLE_CHARS)
        if not heading:
            heading = f"بخش {fallback_index}"
        callouts = tuple(
            filter(None, (NoteCallout.from_payload(item) for item in payload.get("callouts") or []))
        )
        section = cls(
            heading=heading,
            paragraphs=tuple(_string_list(payload.get("paragraphs"))),
            bullets=tuple(_string_list(payload.get("bullets"))),
            key_points=tuple(_string_list(payload.get("key_points"))),
            table=NoteTable.from_payload(payload.get("table")),
            callouts=callouts,
        )
        return section if section.has_content else None


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    term: str
    definition: str

    @classmethod
    def from_payload(cls, payload: object) -> "GlossaryEntry | None":
        if not isinstance(payload, dict):
            return None
        term = _bounded_text(payload.get("term"), MAX_TITLE_CHARS)
        definition = _bounded_text(payload.get("definition"))
        return cls(term, definition) if term and definition else None


@dataclass(frozen=True, slots=True)
class StructuredNotes:
    """The validated note structure the LLM must produce as strict JSON."""

    title: str = ""
    summary: str = ""
    sections: tuple[NoteSection, ...] = ()
    key_points: tuple[str, ...] = ()
    glossary: tuple[GlossaryEntry, ...] = ()

    @property
    def display_title(self) -> str:
        return self.title or "جزوهٔ کلاس"

    @property
    def has_content(self) -> bool:
        return bool(self.summary or self.sections or self.key_points or self.glossary)

    def to_payload(self) -> dict:
        return {
            "title": self.title,
            "summary": self.summary,
            "sections": [
                {
                    "heading": section.heading,
                    "paragraphs": list(section.paragraphs),
                    "bullets": list(section.bullets),
                    "key_points": list(section.key_points),
                    "table": (
                        {
                            "headers": section.table.headers,
                            "rows": section.table.rows,
                        }
                        if section.table
                        else None
                    ),
                    "callouts": [
                        {"kind": callout.kind, "text": callout.text}
                        for callout in section.callouts
                    ],
                }
                for section in self.sections
            ],
            "key_points": list(self.key_points),
            "glossary": [
                {"term": entry.term, "definition": entry.definition}
                for entry in self.glossary
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

    def to_markdown(self) -> str:
        """Render as the Markdown subset the Telegram renderer understands."""
        parts: list[str] = [f"# {self.display_title}"]
        if self.summary:
            parts.append(f"**{self.summary}**")
        for section in self.sections:
            parts.append(f"## {section.heading}")
            parts.extend(section.paragraphs)
            parts.extend(f"- {bullet}" for bullet in section.bullets)
            if section.key_points:
                parts.append("**نکته‌های کلیدی این بخش**")
                parts.extend(f"- {point}" for point in section.key_points)
            for callout in section.callouts:
                parts.append(f"**{callout.kind}:** {callout.text}")
            if section.table is not None:
                header = "| " + " | ".join(section.table.headers) + " |"
                separator = "|" + "|".join("-" * max(len(header) + 2, 3) for header in section.table.headers) + "|"
                rows = "\n".join(
                    "| " + " | ".join(row) + " |" for row in section.table.rows
                )
                parts.append(f"{header}\n{separator}\n{rows}")
        if self.key_points:
            parts.append("## نکته‌های کلیدی")
            parts.extend(f"- {point}" for point in self.key_points)
        if self.glossary:
            parts.append("## واژه‌نامه")
            parts.extend(f"- **{entry.term}:** {entry.definition}" for entry in self.glossary)
        return "\n\n".join(parts)

    @classmethod
    def from_payload(cls, payload: object) -> "StructuredNotes":
        """Validate and normalise a model-supplied JSON payload."""
        if not isinstance(payload, dict):
            raise StructuringError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
        raw_sections = payload.get("sections")
        sections = tuple(
            filter(
                None,
                (
                    NoteSection.from_payload(item, index)
                    for index, item in enumerate(
                        raw_sections if isinstance(raw_sections, (list, tuple)) else [],
                        start=1,
                    )
                ),
            )
        )
        raw_glossary = payload.get("glossary")
        glossary_items = raw_glossary if isinstance(raw_glossary, (list, tuple)) else []
        glossary = tuple(
            filter(None, (GlossaryEntry.from_payload(item) for item in glossary_items))
        )
        notes = cls(
            title=_bounded_text(payload.get("title"), MAX_TITLE_CHARS),
            summary=_bounded_text(payload.get("summary")),
            sections=sections,
            key_points=tuple(_string_list(payload.get("key_points"))),
            glossary=glossary,
        )
        if not notes.has_content:
            raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
        return notes


def merge_structured_notes(notes: list[StructuredNotes]) -> StructuredNotes:
    """Combine per-chunk notes into one document, preserving order."""
    if not notes:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی بود.")
    if len(notes) == 1:
        return notes[0]
    merged = StructuredNotes(
        title=notes[0].title,
        summary=next((item.summary for item in notes if item.summary), ""),
        sections=tuple(section for item in notes for section in item.sections),
        key_points=tuple(point for item in notes for point in item.key_points),
        glossary=tuple(entry for item in notes for entry in item.glossary),
    )
    if not merged.has_content:
        raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
    return merged


def extract_json_object(text: str) -> str:
    """Best-effort extraction of the JSON object from a raw model answer."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL)
    if fence and "{" in fence.group(1):
        candidate = fence.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise StructuringError("پاسخ سرویس تولید جزوه شامل JSON نبود.")
    return candidate[start : end + 1]


def _load_json_payload(text: str) -> dict:
    candidate = extract_json_object(text)
    # Trailing commas before a closing bracket are the most common LLM slip.
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
    for attempt in (candidate, repaired):
        try:
            payload = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise StructuringError("پاسخ سرویس تولید جزوه یک JSON معتبر نبود.")


def parse_structured_notes(text: str) -> StructuredNotes:
    """Parse a model answer into validated structured notes."""
    return StructuredNotes.from_payload(_load_json_payload(text))


# ---------------------------------------------------------------------------
# Transcript splitting
# ---------------------------------------------------------------------------


def split_transcript(text: str, max_chars: int = 22000) -> list[str]:
    """Split long transcripts near sentence boundaries to fit LLM context limits."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    while len(text) > max_chars:
        boundary = max(
            text.rfind(mark, 0, max_chars)
            for mark in (". ", "؟", "!", "؟ ", "؛", "\n")
        )
        if boundary < max_chars // 2:
            boundary = text.rfind(" ", 0, max_chars)
        if boundary < max_chars // 2:
            boundary = max_chars
        else:
            boundary += 1
        pieces.append(text[:boundary].strip())
        text = text[boundary:].strip()
    if text:
        pieces.append(text)
    return pieces


def build_presentation_document(outline: str, transcript: str) -> str:
    """Combine slide text and narration transcript into one prompt payload."""
    sections: list[str] = []
    if outline.strip():
        sections.append("## متن اسلایدها\n\n" + outline.strip())
    if transcript.strip():
        sections.append("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n" + transcript.strip())
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Provider plumbing
# ---------------------------------------------------------------------------


RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def _endpoint(base_url: str, suffix: str) -> str:
    """Append an API path without breaking an exact endpoint's query string."""
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix_path = "/" + suffix.strip("/")
    path = base_path if base_path.endswith(suffix_path) else base_path + suffix_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _retry_delay(response: aiohttp.ClientResponse, attempt: int) -> float:
    """Respect Retry-After while keeping a bounded exponential fallback."""
    raw = response.headers.get("Retry-After", "").strip()
    if raw:
        try:
            return min(max(float(raw), 0.25), 30.0)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return min(max(delay, 0.25), 30.0)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(2 ** attempt, 15.0)


def _sanitize_error_text(value: object, key: str | None) -> str:
    """Bound and redact a provider-supplied string so it is safe to log.

    Only printable text survives, lengths are capped, and the configured API
    key is blanked if a gateway ever echoes it back.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    if key and key in text:
        text = text.replace(key, "***")
    return text[:ERROR_DETAIL_LIMIT]


def _extract_error_detail(raw_body: bytes | None, key: str | None) -> str | None:
    """Return the provider's structured diagnosis from an error body, or None.

    Only well-known metadata fields are extracted (status/type/code, the
    provider's own message, and per-detail ``reason`` codes), because all of
    the supported APIs shape errors as ``{"error": {...}}`` (Gemini's
    google.rpc shape, OpenAI-compatible, Anthropic). Raw bodies are never
    returned: gateways may answer with HTML pages or echo request fragments,
    so anything the parser does not recognise simply stays out of the logs.
    """
    if not raw_body:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None
    error = payload["error"]
    parts: list[str] = []
    for field_name in ("status", "type", "code", "message"):
        value = error.get(field_name)
        if isinstance(value, str) and value.strip():
            sanitized = _sanitize_error_text(value, key)
            if sanitized not in parts:
                parts.append(sanitized)
    details = error.get("details")
    if isinstance(details, list):
        for item in details[:3]:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reason = _sanitize_error_text(item["reason"], key)
                if reason and reason not in parts:
                    parts.append(reason)
    return "; ".join(parts) or None


def _provider_request(
    chunk: str,
    settings: Settings,
    prompt: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[str, dict[str, str], dict, dict[str, str]]:
    """Build a request for Gemini, Anthropic, or an OpenAI-compatible endpoint."""
    provider = settings.note_api_provider
    key = settings.effective_note_api_key
    model = settings.effective_note_model
    extra_headers = dict(settings.note_api_extra_headers)
    full_prompt = prompt + chunk

    if provider == "gemini":
        if not key:
            raise StructuringError("کلید NOTE_API_KEY یا GEMINI_API_KEY تنظیم نشده است.")
        base = settings.note_api_base_url or "https://generativelanguage.googleapis.com/v1beta"
        # Accepting the documented "models/<name>" spelling prevents the
        # .../models/models%2F... URL that Google rejects with HTTP 400.
        model_id = quote(model.strip().removeprefix("models/"), safe="")
        url = _endpoint(base, f"models/{model_id}:generateContent")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": settings.note_api_max_output_tokens,
                # Native JSON mode keeps Gemini from wrapping the answer in prose.
                "responseMimeType": "application/json",
            },
        }
        headers = {"x-goog-api-key": key}
        headers.update(extra_headers)
        return url, headers, payload, {}

    if provider == "openai_compatible":
        base = settings.note_api_base_url or "https://api.openai.com/v1"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        headers.update(extra_headers)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": settings.note_api_max_output_tokens,
        }
        if settings.note_api_json_mode:
            # Opt-in: not every OpenAI-compatible gateway implements
            # response_format, so it must never be forced on by default.
            payload["response_format"] = {"type": "json_object"}
        return _endpoint(base, "chat/completions"), headers, payload, {}

    if provider == "anthropic":
        if not key:
            raise StructuringError("برای Anthropic باید NOTE_API_KEY تنظیم شود.")
        base = settings.note_api_base_url or "https://api.anthropic.com/v1"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        headers.update(extra_headers)
        payload = {
            "model": model,
            "max_tokens": settings.note_api_max_output_tokens,
            "temperature": 0.2,
            "system": system_prompt,
            "messages": [{"role": "user", "content": full_prompt}],
        }
        return _endpoint(base, "messages"), headers, payload, {}

    raise StructuringError("سرویس تولید جزوه غیرفعال است.")


def _provider_response(payload: dict, provider: str) -> str:
    """Normalize supported provider responses to plain text."""
    try:
        if provider == "gemini":
            candidates = payload.get("candidates")
            if not candidates:
                # Safety-blocked or empty answers return no candidates; the
                # blockReason enum makes such failures diagnosable.
                feedback = payload.get("promptFeedback")
                if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
                    reason = _sanitize_error_text(feedback["blockReason"], None)
                    raise StructuringError(f"پاسخ سرویس تولید جزوه مسدود شد ({reason}).")
                raise StructuringError("پاسخ سرویس تولید جزوه خالی یا نامعتبر است.")
            if candidates[0].get("finishReason") == "MAX_TOKENS":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            parts = candidates[0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts)
        elif provider == "anthropic":
            if payload.get("stop_reason") == "max_tokens":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            text = "".join(
                str(part.get("text", ""))
                for part in payload["content"]
                if part.get("type") == "text"
            )
        else:
            if payload["choices"][0].get("finish_reason") == "length":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                text = "".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                text = "" if content is None else str(content)
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی یا نامعتبر است.") from exc
    text = text.strip()
    if not text:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی بود.")
    return text


async def _structure_chunk(
    chunk: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    prompt: str = TRANSCRIPT_PROMPT,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    reminder: str = "",
) -> str:
    url, headers, payload, params = _provider_request(
        chunk + reminder, settings, prompt, system_prompt
    )
    provider = settings.note_api_provider
    attempts = settings.note_api_retries + 1
    for attempt in range(attempts):
        try:
            async with session.post(
                url, headers=headers, params=params, json=payload
            ) as response:
                if (
                    response.status in RETRYABLE_HTTP_STATUSES
                    or 500 <= response.status < 600
                ) and attempt + 1 < attempts:
                    delay = _retry_delay(response, attempt)
                    await response.read()
                    logger.warning(
                        "Note API transient failure provider=%s status=%s retry=%s/%s delay=%.1fs",
                        provider,
                        response.status,
                        attempt + 1,
                        settings.note_api_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status < 200 or response.status >= 300:
                    # Never log raw response bodies: gateways may echo fragments
                    # of the prompt or lecture text. Only the provider's
                    # structured error metadata is extracted and logged — enough
                    # to diagnose a Gemini HTTP 400 (e.g. API_KEY_INVALID)
                    # without exposing content or the API key.
                    body = await response.read()
                    detail = _extract_error_detail(body, settings.effective_note_api_key)
                    request_id = (
                        response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                        or "unknown"
                    )
                    logger.error(
                        "Note API request failed provider=%s status=%s request_id=%s detail=%s",
                        provider,
                        response.status,
                        request_id,
                        detail or "not provided",
                    )
                    message = f"سرویس {provider} خطای HTTP {response.status} داد."
                    if detail:
                        message += f" ({detail})"
                    raise StructuringError(message)
                data = await response.json(content_type=None)
                return _provider_response(data, provider)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt + 1 >= attempts:
                raise StructuringError(
                    f"ارتباط با سرویس تولید جزوه ({provider}) برقرار نشد."
                ) from None
            delay = min(2 ** attempt, 15.0)
            logger.warning(
                "Note API network failure provider=%s retry=%s/%s delay=%.1fs: %s",
                provider,
                attempt + 1,
                settings.note_api_retries,
                delay,
                type(exc).__name__,
            )
            await asyncio.sleep(delay)
    raise StructuringError("سرویس تولید جزوه پاسخی برنگرداند.")


async def _structured_notes_for(
    document: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    prompt: str,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    label: str = "chunk",
) -> StructuredNotes:
    """One LLM answer parsed as strict JSON, with a single bounded repair pass."""
    started = asyncio.get_running_loop().time()
    try:
        raw = await _structure_chunk(document, settings, session, prompt, system_prompt=system_prompt)
    except Exception as exc:
        if isinstance(exc, StructuringError):
            logger.warning(
                "Note structuring failed %s elapsed_seconds=%.1f error=%s",
                label,
                asyncio.get_running_loop().time() - started,
                exc,
            )
        else:
            logger.exception("Note structuring failed unexpectedly %s", label)
        raise
    try:
        notes = parse_structured_notes(raw)
    except StructuringError as exc:
        logger.warning(
            "Note API answer failed JSON validation (%s); requesting one repair pass", exc
        )
        raw = await _structure_chunk(
            document,
            settings,
            session,
            prompt,
            system_prompt=system_prompt,
            reminder=JSON_REMINDER,
        )
        try:
            notes = parse_structured_notes(raw)
        except StructuringError as exc:
            logger.error("Note API answer was not valid JSON even after the repair pass")
            raise StructuringError(
                "پاسخ سرویس تولید جزوه پس از تلاش مجدد همچنان JSON معتبر نبود."
            ) from exc
    logger.info(
        "Note structuring completed %s elapsed_seconds=%.1f sections=%s key_points=%s "
        "glossary=%s title_chars=%s",
        label,
        asyncio.get_running_loop().time() - started,
        len(notes.sections),
        len(notes.key_points),
        len(notes.glossary),
        len(notes.title),
    )
    return notes


async def structure_transcript(text: str, settings: Settings) -> StructuredNotes:
    """Turn a transcript into structured notes with the configured provider."""
    if not text.strip():
        raise StructuringError("متن پیاده‌سازی‌شده خالی است.")
    if settings.note_api_provider == "disabled":
        raise StructuringError("سرویس تولید جزوه غیرفعال است.")
    chunks = split_transcript(text)
    timeout = aiohttp.ClientTimeout(
        total=settings.note_api_timeout,
        connect=min(30, settings.note_api_timeout),
        sock_read=settings.note_api_timeout,
    )
    notes: list[StructuredNotes] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, chunk in enumerate(chunks, start=1):
            notes.append(
                await _structured_notes_for(
                    chunk,
                    settings,
                    session,
                    TRANSCRIPT_PROMPT,
                    label=f"chunk {index}/{len(chunks)}",
                )
            )
    return merge_structured_notes(notes)


async def structure_presentation(
    outline: str, transcript: str, settings: Settings, max_chars: int = 22000
) -> StructuredNotes:
    """Build a slide-ordered booklet from slide text plus narration transcript."""
    if not outline.strip() and not transcript.strip():
        raise StructuringError("محتوای قابل‌استفاده‌ای از فایل ارائه به دست نیامد.")
    if settings.note_api_provider == "disabled":
        raise StructuringError("سرویس تولید جزوه غیرفعال است.")

    if max_chars < 1000:
        raise ValueError("max_chars must be at least 1000 for presentation context")
    outline = outline.strip()
    if transcript.strip() and len(outline) <= max_chars // 2:
        # Repeat a short outline as context for each narration chunk.
        transcript_budget = max_chars - len(build_presentation_document(outline, "")) - 100
        documents = [
            build_presentation_document(outline, chunk)
            for chunk in split_transcript(transcript, transcript_budget)
        ]
    else:
        # Long decks (including slides-only decks) must not silently lose their
        # final slides. Chunk the complete material instead of truncating it.
        documents = split_transcript(build_presentation_document(outline, transcript), max_chars)

    timeout = aiohttp.ClientTimeout(
        total=settings.note_api_timeout,
        connect=min(30, settings.note_api_timeout),
        sock_read=settings.note_api_timeout,
    )
    notes: list[StructuredNotes] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, document in enumerate(documents, start=1):
            notes.append(
                await _structured_notes_for(
                    document,
                    settings,
                    session,
                    PRESENTATION_PROMPT,
                    system_prompt=PRESENTATION_SYSTEM_PROMPT,
                    label=f"presentation chunk {index}/{len(documents)}",
                )
            )
    return merge_structured_notes(notes)
