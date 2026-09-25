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


async def _structure_chunk(
    chunk: str, settings: Settings, session: aiohttp.ClientSession
) -> str:
    assert settings.gemini_api_key
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT + chunk}]}],
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
