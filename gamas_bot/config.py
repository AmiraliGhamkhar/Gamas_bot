from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in TRUTHY


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    admin_ids: frozenset[int]
    database_path: Path
    session_path: Path
    temp_dir: Path
    max_file_size: int
    stt_primary: str
    stt_fallback_enabled: bool
    stt_min_confidence: float
    speechmatics_api_key: str | None
    speechmatics_base_url: str
    deepgram_api_key: str | None
    deepgram_model: str
    gemini_api_key: str | None
    gemini_model: str
    max_concurrent_jobs: int
    stt_poll_interval: float
    stt_job_timeout: int
    presentation_enabled: bool = True
    presentation_include_slide_text: bool = True
    presentation_include_video_audio: bool = True
    presentation_legacy_enabled: bool = True
    presentation_min_clip_seconds: float = 1.0
    presentation_silence_seconds: float = 0.5
    presentation_max_clips: int = 300
    presentation_max_total_duration: int = 21600
    presentation_max_unpacked_bytes: int = 4_000_000_000
    presentation_wav_limit_bytes: int = 700_000_000
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    soffice_bin: str = "soffice"
    ffmpeg_timeout: int = 3600
    soffice_timeout: int = 600

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        load_dotenv(env_file, override=False)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        try:
            api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
            admins = frozenset(
                int(item.strip())
                for item in os.getenv("ADMIN_IDS", "").split(",")
                if item.strip()
            )
        except ValueError as exc:
            raise ValueError("TELEGRAM_API_ID و ADMIN_IDS باید عددی باشند.") from exc

        primary = os.getenv("STT_PRIMARY", "speechmatics").strip().lower()
        if primary not in {"speechmatics", "deepgram"}:
            raise ValueError("STT_PRIMARY فقط می‌تواند speechmatics یا deepgram باشد.")
        try:
            min_confidence = float(os.getenv("STT_MIN_CONFIDENCE", "0.65"))
            max_file_size = int(os.getenv("MAX_FILE_SIZE_BYTES", "2000000000"))
            max_jobs = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
            poll_interval = float(os.getenv("STT_POLL_INTERVAL_SECONDS", "5"))
            job_timeout = int(os.getenv("STT_JOB_TIMEOUT_SECONDS", "21600"))
        except ValueError as exc:
            raise ValueError("مقادیر عددی تنظیمات محیط معتبر نیستند.") from exc
        if not 0 <= min_confidence <= 1:
            raise ValueError("STT_MIN_CONFIDENCE باید بین صفر و یک باشد.")
        if min(max_file_size, max_jobs, poll_interval, job_timeout) <= 0:
            raise ValueError("اندازه فایل، هم‌زمانی و زمان‌های انتظار باید مثبت باشند.")

        try:
            min_clip = float(os.getenv("PPTX_MIN_CLIP_SECONDS", "1.0"))
            silence = float(os.getenv("PPTX_SILENCE_SECONDS", "0.5"))
            max_clips = int(os.getenv("PPTX_MAX_CLIPS", "300"))
            max_total_duration = int(os.getenv("PPTX_MAX_TOTAL_DURATION_SECONDS", "21600"))
            max_unpacked = int(os.getenv("PPTX_MAX_UNPACKED_BYTES", "4000000000"))
            wav_limit = int(os.getenv("PPTX_WAV_LIMIT_BYTES", "700000000"))
            ffmpeg_timeout = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "3600"))
            soffice_timeout = int(os.getenv("SOFFICE_TIMEOUT_SECONDS", "600"))
        except ValueError as exc:
            raise ValueError("مقادیر عددی مربوط به پردازش فایل ارائه معتبر نیستند.") from exc
        if min_clip < 0 or silence < 0:
            raise ValueError("PPTX_MIN_CLIP_SECONDS و PPTX_SILENCE_SECONDS نمی‌توانند منفی باشند.")
        if min(max_clips, max_total_duration, max_unpacked, wav_limit) <= 0:
            raise ValueError("محدودیت‌های عددی فایل ارائه باید مثبت باشند.")
        if min(ffmpeg_timeout, soffice_timeout) <= 0:
            raise ValueError("زمان‌های انتظار ffmpeg و soffice باید مثبت باشند.")

        return cls(
            telegram_bot_token=token,
            telegram_api_id=api_id,
            telegram_api_hash=api_hash,
            admin_ids=admins,
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            session_path=Path(os.getenv("TELEGRAM_SESSION_PATH", "data/telegram_bot")),
            temp_dir=Path(os.getenv("TEMP_DIR", "data/tmp")),
            max_file_size=max_file_size,
            stt_primary=primary,
            stt_fallback_enabled=_flag("STT_FALLBACK_ENABLED", True),
            stt_min_confidence=min_confidence,
            speechmatics_api_key=os.getenv("SPEECHMATICS_API_KEY") or None,
            speechmatics_base_url=os.getenv(
                "SPEECHMATICS_BASE_URL", "https://eu1.asr.api.speechmatics.com/v2"
            ).rstrip("/"),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY") or None,
            deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            max_concurrent_jobs=max_jobs,
            stt_poll_interval=poll_interval,
            stt_job_timeout=job_timeout,
            presentation_enabled=_flag("PPTX_ENABLED", True),
            presentation_include_slide_text=_flag("PPTX_INCLUDE_SLIDE_TEXT", True),
            presentation_include_video_audio=_flag("PPTX_INCLUDE_VIDEO_AUDIO", True),
            presentation_legacy_enabled=_flag("PPTX_LEGACY_ENABLED", True),
            presentation_min_clip_seconds=min_clip,
            presentation_silence_seconds=silence,
            presentation_max_clips=max_clips,
            presentation_max_total_duration=max_total_duration,
            presentation_max_unpacked_bytes=max_unpacked,
            presentation_wav_limit_bytes=wav_limit,
            ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg",
            ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe").strip() or "ffprobe",
            soffice_bin=os.getenv("SOFFICE_BIN", "soffice").strip() or "soffice",
            ffmpeg_timeout=ffmpeg_timeout,
            soffice_timeout=soffice_timeout,
        )

    def validate_runtime(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if self.telegram_api_id <= 0:
            missing.append("TELEGRAM_API_ID")
        if not self.telegram_api_hash:
            missing.append("TELEGRAM_API_HASH")
        if missing:
            raise ValueError("متغیرهای ضروری تنظیم نشده‌اند: " + ", ".join(missing))
        if not self.speechmatics_api_key and not self.deepgram_api_key:
            raise ValueError("حداقل یکی از SPEECHMATICS_API_KEY یا DEEPGRAM_API_KEY لازم است.")
