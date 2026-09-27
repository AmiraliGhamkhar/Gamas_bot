"""Production logging configuration with optional JSON and file rotation."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


log_job_id: ContextVar[str] = ContextVar("log_job_id", default="-")


class JobContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = log_job_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, suitable for journald and log collectors."""

    _standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._standard or key.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
            else:
                payload[key] = str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: "Settings | None" = None) -> None:
    """Configure stdout plus an optional size-rotated file handler."""
    level_name = settings.log_level if settings is not None else "INFO"
    level = getattr(logging, level_name, logging.INFO)
    formatter: logging.Formatter
    if settings is not None and settings.log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [job=%(job_id)s]: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings is not None and settings.log_file is not None:
        path = Path(settings.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path,
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
        )
    context_filter = JobContextFilter()
    for handler in handlers:
        handler.addFilter(context_filter)
        handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.captureWarnings(True)
    logging.getLogger("telethon").setLevel(max(level, logging.WARNING))
    logging.getLogger("aiohttp").setLevel(max(level, logging.WARNING))


def install_asyncio_exception_handler(loop) -> None:
    """Log orphaned task failures instead of silently losing them."""

    def handle(_loop, context):
        error = context.get("exception")
        message = context.get("message", "Unhandled asyncio error")
        if error is None:
            logging.getLogger("asyncio").error("%s context=%r", message, context)
        else:
            logging.getLogger("asyncio").error(
                "%s",
                message,
                exc_info=(type(error), error, error.__traceback__),
            )

    loop.set_exception_handler(handle)
