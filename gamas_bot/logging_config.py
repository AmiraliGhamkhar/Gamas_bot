"""Production logging configuration with JSON/plain formatters, rotation, and secret redaction.

Design responsibilities (SRP):

* **Level & format resolution** — :func:`resolve_log_level` and
  :data:`VALID_LOG_FORMATS` map operator strings to validated ``logging``
  constants without exposing ``getattr(logging, ...)`` pitfalls.
* **Context & privacy** — :class:`JobContextFilter` (backed by a chained
  ``LogRecord`` factory) stamps ``job_id`` onto every record, while
  :func:`redact_log_secrets` scrubs configured tokens and API keys from
  rendered log lines and tracebacks (R11).
* **Transport resilience** — :class:`FlushingStreamHandler` flushes every line
  immediately and survives non-UTF-8 Windows consoles (``cp1252``) and closed
  pipes; :class:`SecureRotatingFileHandler` enforces ``0600`` owner-only file
  permissions across log creation and rotation (R10).
"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import re
import sys
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from .config import Settings


log_job_id: ContextVar[str] = ContextVar("log_job_id", default="-")

VALID_LOG_FORMATS: tuple[str, ...] = ("plain", "json")

_STANDARD_LOG_LEVELS: dict[str, int] = {
    name.upper(): int(value)
    for name, value in logging.getLevelNamesMapping().items()
    if isinstance(value, int)
}

_BOT_TOKEN_URL_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{10,}")
_BEARER_TOKEN_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_GOOGLE_API_KEY_RE = re.compile(
    r"(x-goog-api-key['\":=\s]+)[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)

_MIN_SECRET_LEN = 6
_RECORD_FACTORY_INSTALLED = False


def is_valid_log_level(value: str | int | None) -> bool:
    """True when ``value`` names a standard Python log level or non-negative integer."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value >= 0
    if value is None:
        return False
    cleaned = str(value).strip().upper()
    if not cleaned:
        return False
    if cleaned in _STANDARD_LOG_LEVELS:
        return True
    return cleaned.isdigit()


def resolve_log_level(value: str | int | None, default: int = logging.INFO) -> int:
    """Resolve ``value`` to a numeric ``logging`` level without raising."""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    if value is None:
        return default
    cleaned = str(value).strip().upper()
    if not cleaned:
        return default
    mapped = _STANDARD_LOG_LEVELS.get(cleaned)
    if mapped is not None:
        return mapped
    if cleaned.isdigit():
        return int(cleaned)
    return default


def collect_configured_secrets(settings: Settings | None) -> tuple[str, ...]:
    """Extract non-trivial secret strings from ``settings`` (or env fallback) for redaction."""
    candidates: list[str] = []
    if settings is not None:
        candidates.extend(
            [
                settings.bot_token,
                settings.api_hash,
                settings.speechmatics_api_key,
                settings.deepgram_api_key,
                settings.stt_openai_api_key,
                settings.note_api_key,
                settings.nara_api_key,
                settings.openrouter_api_key,
                settings.gemini_api_key,
            ]
        )
    else:
        for env_key in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_API_HASH",
            "SPEECHMATICS_API_KEY",
            "DEEPGRAM_API_KEY",
            "STT_OPENAI_API_KEY",
            "NOTE_API_KEY",
            "NARA_API_KEY",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
        ):
            raw = os.environ.get(env_key, "").strip()
            if raw:
                candidates.append(raw)
    # Longest secrets first so a prefix never masks only half of a token.
    unique = {item.strip() for item in candidates if item and len(item.strip()) >= _MIN_SECRET_LEN}
    return tuple(sorted(unique, key=len, reverse=True))


def redact_log_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    """Scrub configured credentials and common bearer/bot-token patterns from ``text``."""
    if not text:
        return text
    redacted = text
    for secret in secrets:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _BOT_TOKEN_URL_RE.sub("bot[REDACTED]", redacted)
    redacted = _BEARER_TOKEN_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _GOOGLE_API_KEY_RE.sub(r"\1[REDACTED]", redacted)
    return redacted


def _install_record_factory() -> None:
    """Stamp ``job_id`` at record creation so external handlers never fail formatting."""
    global _RECORD_FACTORY_INSTALLED
    if _RECORD_FACTORY_INSTALLED:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if not hasattr(record, "job_id"):
            record.job_id = log_job_id.get()
        return record

    logging.setLogRecordFactory(factory)
    _RECORD_FACTORY_INSTALLED = True


class JobContextFilter(logging.Filter):
    """Attach the active ``ContextVar`` job identifier to every handled record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = log_job_id.get()
        return True


class PlainFormatter(logging.Formatter):
    """Single-line human-readable formatter with guaranteed ``job_id`` and redaction."""

    DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s [job=%(job_id)s]: %(message)s"
    DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

    def __init__(
        self,
        fmt: str = DEFAULT_FORMAT,
        datefmt: str = DEFAULT_DATEFMT,
        *,
        secrets: Sequence[str] = (),
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "job_id"):
            record.job_id = log_job_id.get()
        rendered = super().format(record)
        return redact_log_secrets(rendered, self._secrets)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, suitable for journald and log collectors."""

    _standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def __init__(self, *, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "job_id"):
            record.job_id = log_job_id.get()
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_log_secrets(record.getMessage(), self._secrets),
        }
        for key, value in record.__dict__.items():
            if key in self._standard or key.startswith("_"):
                continue
            if isinstance(value, str):
                payload[key] = redact_log_secrets(value, self._secrets)
            elif isinstance(value, (int, float, bool)) or value is None:
                payload[key] = value
            else:
                payload[key] = redact_log_secrets(str(value), self._secrets)
        if record.exc_info:
            payload["exception"] = redact_log_secrets(
                self.formatException(record.exc_info), self._secrets
            )
        return json.dumps(payload, ensure_ascii=False)


class FlushingStreamHandler(logging.StreamHandler):
    """Stream handler that flushes each record and degrades safely on non-UTF-8 consoles."""

    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is None:
            return
        try:
            msg = self.format(record)
            stream = self.stream
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                encoding = getattr(stream, "encoding", None) or "utf-8"
                safe = msg.encode(encoding, errors="backslashreplace").decode(
                    encoding, errors="replace"
                )
                stream.write(safe + self.terminator)
            self.flush()
        except (BrokenPipeError, ValueError):
            # Detached pipes or streams closed during interpreter teardown must
            # not trigger recursive logging tracebacks.
            return
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


class SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Size-rotating UTF-8 file handler enforcing owner-only (0600) permissions."""

    def _open(self) -> io.TextIOWrapper:
        stream = super()._open()
        _restrict_file_mode(Path(self.baseFilename))
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        base = Path(self.baseFilename)
        _restrict_file_mode(base)
        for index in range(1, self.backupCount + 1):
            candidate = Path(f"{self.baseFilename}.{index}")
            if candidate.exists():
                _restrict_file_mode(candidate)


def _restrict_file_mode(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_private_log_dir(directory: Path) -> None:
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created and os.name == "posix":
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass


def _stream_is_devnull(stream: TextIO | None) -> bool:
    if stream is None:
        return True
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    try:
        stream_stat = os.fstat(fd)
        devnull_stat = os.stat(os.devnull)
    except OSError:
        return False
    return (
        stream_stat.st_dev == devnull_stat.st_dev
        and stream_stat.st_ino == devnull_stat.st_ino
    )


def _env_log_file_fallback() -> Path | None:
    raw = os.environ.get("LOG_FILE", "").strip()
    if raw in ("", "-"):
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        project_dir = Path(__file__).resolve().parent.parent
        path = project_dir / path
    return path


def configure_logging(
    settings: "Settings | None" = None,
    *,
    stream: TextIO | None = None,
) -> None:
    """Configure console output plus an optional size-rotated private file handler."""
    _install_record_factory()

    if settings is not None:
        level = resolve_log_level(settings.log_level, logging.INFO)
        fmt_kind = (settings.log_format or "plain").strip().lower()
        log_file: Path | None = Path(settings.log_file) if settings.log_file is not None else None
        max_bytes = settings.log_max_bytes
        backup_count = settings.log_backup_count
    else:
        level = resolve_log_level(os.environ.get("LOG_LEVEL", "INFO"), logging.INFO)
        fmt_kind = os.environ.get("LOG_FORMAT", "plain").strip().lower()
        log_file = _env_log_file_fallback()
        max_bytes = 5 * 1024 * 1024
        backup_count = 5

    secrets = collect_configured_secrets(settings)
    formatter: logging.Formatter
    if fmt_kind == "json":
        formatter = JsonFormatter(secrets=secrets)
    else:
        formatter = PlainFormatter(secrets=secrets)

    handlers: list[logging.Handler] = []
    file_open_error: OSError | None = None

    primary_stream = stream
    if primary_stream is None:
        # Early bootstrap / crash fallback (``settings is None``) prefers stderr
        # so background launchers redirecting stdout to DEVNULL still record it.
        if settings is None:
            primary_stream = sys.stderr if sys.stderr is not None else sys.stdout
        else:
            primary_stream = sys.stdout if sys.stdout is not None else sys.stderr

    if primary_stream is not None and not (
        log_file is not None and stream is None and _stream_is_devnull(primary_stream)
    ):
        handlers.append(FlushingStreamHandler(primary_stream))

    if log_file is not None:
        try:
            _ensure_private_log_dir(log_file.parent)
            handlers.append(
                SecureRotatingFileHandler(
                    log_file,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
            )
        except OSError as exc:
            file_open_error = exc

    # When running detached under the launcher (stdout -> DEVNULL, stderr -> launcher.err),
    # mirror ERROR/CRITICAL records (or everything if opening LOG_FILE failed) to stderr.
    if (
        stream is None
        and sys.stderr is not None
        and not _stream_is_devnull(sys.stderr)
        and all(getattr(h, "stream", None) is not sys.stderr for h in handlers)
    ):
        if file_open_error is not None:
            handlers.append(FlushingStreamHandler(sys.stderr))
        elif _stream_is_devnull(primary_stream):
            err_handler = FlushingStreamHandler(sys.stderr)
            err_handler.setLevel(logging.ERROR)
            handlers.append(err_handler)

    if not handlers:
        handlers.append(logging.NullHandler())

    context_filter = JobContextFilter()
    for handler in handlers:
        handler.addFilter(context_filter)
        handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.captureWarnings(True)
    logging.getLogger("telethon").setLevel(max(level, logging.WARNING))
    logging.getLogger("aiohttp").setLevel(max(level, logging.WARNING))

    if file_open_error is not None:
        logging.getLogger(__name__).warning(
            "could not open LOG_FILE=%s (%s); continuing with stream logging only",
            log_file,
            file_open_error,
        )


def install_asyncio_exception_handler(loop: object) -> None:
    """Log orphaned task failures instead of silently losing them."""

    def handle(_loop: object, context: dict[str, object]) -> None:
        error = context.get("exception")
        message = context.get("message", "Unhandled asyncio error")
        if not isinstance(error, BaseException):
            logging.getLogger("asyncio").error("%s context=%r", message, context)
        else:
            logging.getLogger("asyncio").error(
                "%s",
                message,
                exc_info=(type(error), error, error.__traceback__),
            )

    set_handler = getattr(loop, "set_exception_handler", None)
    if callable(set_handler):
        set_handler(handle)
