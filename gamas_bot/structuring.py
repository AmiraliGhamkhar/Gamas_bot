from __future__ import annotations

import logging

import aiohttp

from .config import Settings

logger = logging.getLogger(__name__)


class StructuringError(RuntimeError):
    pass


PROMPT = """شما دستیار آموزشی فارسی هستید. متن پیاده‌سازی‌شده از یک کلاس یا فایل صوتی را به جزوه‌ای دقیق، خوانا و مناسب مرور تبدیل کنید.

قواعد:
- فقط بر پایهٔ متن داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید.
- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم را جداگانه برجسته کنید.
- مطالب را با عنوان‌ها، زیرعنوان‌ها، فهرست و تأکید مناسب مرتب کنید.
- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از جدول سادهٔ Markdown استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.
- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.
- اگر متن ناقص یا تکراری است، مفهوم موجود را مرتب کنید و چیزی به آن نیفزایید.
- پاسخ را فقط به زبان فارسی و به شکل جزوه ارائه کنید؛ مقدمهٔ گفت‌وگویی ننویسید.

متن پیاده‌سازی‌شده:
"""


def split_transcript(text: str, max_chars: int = 22000) -> list[str]:
    """Split long transcripts near sentence boundaries to fit LLM context limits."""
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


PRESENTATION_PROMPT = """شما دستیار آموزشی فارسی هستید. از روی محتوای یک فایل ارائهٔ درسی (PowerPoint) — شامل متن اسلایدها، یادداشت‌های گوینده و متن پیاده‌سازی‌شدهٔ صدای ضبط‌شدهٔ همان ارائه — یک جزوهٔ دقیق، خوانا و مناسب مرور بسازید.

قواعد:
- فقط بر پایهٔ مطالب داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید.
- ترتیب جزوه را از ترتیب اسلایدها بگیرید و توضیح‌های صوتی را زیر همان موضوع اسلاید ادغام کنید.
- اگر صدا مطلبی فراتر از متن اسلاید دارد، آن را به‌عنوان توضیح کامل‌کننده بیاورید؛ مطالب تکراری را یک بار بنویسید.
- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم گوینده را جداگانه برجسته کنید.
- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از جدول سادهٔ Markdown استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.
- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.
- پاسخ را فقط به زبان فارسی و به شکل جزوه ارائه کنید؛ مقدمهٔ گفت‌وگویی ننویسید.

محتوای ارائه:
"""


async def _structure_chunk(
    chunk: str, settings: Settings, session: aiohttp.ClientSession, prompt: str = PROMPT
) -> str:
    assert settings.gemini_api_key
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt + chunk}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }
    async with session.post(
        url,
        params={"key": settings.gemini_api_key},
        json=payload,
    ) as response:
        if response.status != 200:
            detail = (await response.text())[:600]
            raise StructuringError(f"Gemini خطا داد ({response.status}): {detail}")
        data = await response.json(content_type=None)
    try:
        text = "".join(
            part.get("text", "")
            for part in data["candidates"][0]["content"]["parts"]
        ).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise StructuringError("پاسخ Gemini خالی یا نامعتبر است.") from exc
    if not text:
        raise StructuringError("پاسخ Gemini خالی بود.")
    return text


async def structure_transcript(text: str, settings: Settings) -> str:
    """Provider-isolated LLM entry point; currently uses Gemini 2.5 Flash-Lite."""
    if not text.strip():
        raise StructuringError("متن پیاده‌سازی‌شده خالی است.")
    if not settings.gemini_api_key:
        raise StructuringError("کلید GEMINI_API_KEY تنظیم نشده است.")
    chunks = split_transcript(text)
    timeout = aiohttp.ClientTimeout(total=240, connect=30, sock_read=180)
    outputs: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, chunk in enumerate(chunks, start=1):
            try:
                result = await _structure_chunk(chunk, settings, session)
            except Exception:
                logger.exception("Transcript structuring failed for chunk %s/%s", index, len(chunks))
                raise
            if len(chunks) > 1:
                result = f"## بخش {index}\n\n{result}"
            outputs.append(result)
    return "\n\n---\n\n".join(outputs)


def build_presentation_document(outline: str, transcript: str) -> str:
    """Combine slide text and narration transcript into one prompt payload."""
    sections: list[str] = []
    if outline.strip():
        sections.append("## متن اسلایدها\n\n" + outline.strip())
    if transcript.strip():
        sections.append("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n" + transcript.strip())
    return "\n\n".join(sections)


async def structure_presentation(
    outline: str, transcript: str, settings: Settings, max_chars: int = 22000
) -> str:
    """Build a slide-ordered booklet from slide text plus narration transcript."""
    if not outline.strip() and not transcript.strip():
        raise StructuringError("محتوای قابل‌استفاده‌ای از فایل ارائه به دست نیامد.")
    if not settings.gemini_api_key:
        raise StructuringError("کلید GEMINI_API_KEY تنظیم نشده است.")

    # The outline is repeated in every chunk so each request keeps slide context;
    # it is trimmed first so a long deck cannot crowd out the transcript.
    outline_budget = max(0, max_chars // 2)
    trimmed_outline = outline.strip()
    if len(trimmed_outline) > outline_budget:
        trimmed_outline = (
            trimmed_outline[:outline_budget].rsplit("\n", 1)[0]
            + "\n\n(ادامهٔ متن اسلایدها کوتاه شد)"
        )
    transcript_budget = max(2000, max_chars - len(trimmed_outline) - 500)
    chunks = split_transcript(transcript, transcript_budget) or [""]

    timeout = aiohttp.ClientTimeout(total=240, connect=30, sock_read=180)
    outputs: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, chunk in enumerate(chunks, start=1):
            document = build_presentation_document(trimmed_outline, chunk)
            try:
                result = await _structure_chunk(
                    document, settings, session, PRESENTATION_PROMPT
                )
            except Exception:
                logger.exception(
                    "Presentation structuring failed for chunk %s/%s", index, len(chunks)
                )
                raise
            if len(chunks) > 1:
                result = f"## بخش {index}\n\n{result}"
            outputs.append(result)
    return "\n\n---\n\n".join(outputs)
