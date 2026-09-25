from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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
            stt_fallback_enabled=os.getenv("STT_FALLBACK_ENABLED", "true").lower()
            in {"1", "true", "yes", "on"},
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
