"""Injectable collaborator gateway for :class:`gamas_bot.bot.GamasBot` (DIP).

Workflows access external collaborators (STT, LLM, media worker, document
parser, progress bars, transfer limiter, and monotonic clock) through
:class:`BotDependencies` rather than hard-wiring concrete module functions.

When no explicit override is supplied on :class:`BotDependencies`, each method
resolves the default collaborator via ``gamas_bot.bot`` so both constructor
dependency injection (``GamasBot(settings, deps=...)``) and existing test seams
(``mock.patch("gamas_bot.bot.transcribe", ...)``) work identically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import sys
import time
from typing import Any

import aiohttp
from telethon.errors import FloodWaitError

from .doc_extract import extract_document
from .llm import complete_json
from .media import extract_audio_track, needs_transcode, probe_media
from .notes import structure_transcript
from .presentations import load_presentation, prepare_audio
from .progress import JobProgress
from .stt import transcribe
from .telegram_text import render_pages
from .telegram_transfer import _download_media_limited, _ensure_disk_headroom


def _bot_attr(name: str, fallback: Any) -> Any:
    bot_mod = sys.modules.get("gamas_bot.bot")
    if bot_mod is not None and hasattr(bot_mod, name):
        return getattr(bot_mod, name)
    return fallback


@dataclass
class BotDependencies:
    """Explicit collaborator bundle for :class:`GamasBot` workflows (DIP)."""

    transcribe_fn: Callable[..., Any] | None = None
    probe_media_fn: Callable[..., Any] | None = None
    prepare_audio_fn: Callable[..., Any] | None = None
    needs_transcode_fn: Callable[..., Any] | None = None
    extract_audio_track_fn: Callable[..., Any] | None = None
    load_presentation_fn: Callable[..., Any] | None = None
    extract_document_fn: Callable[..., Any] | None = None
    structure_transcript_fn: Callable[..., Any] | None = None
    complete_json_fn: Callable[..., Any] | None = None
    render_pages_fn: Callable[..., Any] | None = None
    ensure_disk_headroom_fn: Callable[..., Any] | None = None
    download_media_limited_fn: Callable[..., Any] | None = None
    create_job_progress_fn: Callable[..., Any] | None = None
    monotonic_fn: Callable[[], float] | None = None

    @property
    def transcribe(self) -> Callable[..., Any]:
        return self.transcribe_fn or _bot_attr("transcribe", transcribe)

    @property
    def probe_media(self) -> Callable[..., Any]:
        return self.probe_media_fn or _bot_attr("probe_media", probe_media)

    @property
    def prepare_audio(self) -> Callable[..., Any]:
        return self.prepare_audio_fn or _bot_attr("prepare_audio", prepare_audio)

    @property
    def needs_transcode(self) -> Callable[..., Any]:
        return self.needs_transcode_fn or _bot_attr("needs_transcode", needs_transcode)

    @property
    def extract_audio_track(self) -> Callable[..., Any]:
        return self.extract_audio_track_fn or _bot_attr(
            "extract_audio_track", extract_audio_track
        )

    @property
    def load_presentation(self) -> Callable[..., Any]:
        return self.load_presentation_fn or _bot_attr(
            "load_presentation", load_presentation
        )

    @property
    def extract_document(self) -> Callable[..., Any]:
        return self.extract_document_fn or _bot_attr(
            "extract_document", extract_document
        )

    @property
    def structure_transcript(self) -> Callable[..., Any]:
        return self.structure_transcript_fn or _bot_attr(
            "structure_transcript", structure_transcript
        )

    @property
    def complete_json(self) -> Callable[..., Any]:
        return self.complete_json_fn or _bot_attr("complete_json", complete_json)

    @property
    def render_pages(self) -> Callable[..., Any]:
        return self.render_pages_fn or _bot_attr("render_pages", render_pages)

    @property
    def ensure_disk_headroom(self) -> Callable[..., Any]:
        return self.ensure_disk_headroom_fn or _bot_attr(
            "_ensure_disk_headroom", _ensure_disk_headroom
        )

    @property
    def download_media_limited(self) -> Callable[..., Any]:
        return self.download_media_limited_fn or _bot_attr(
            "_download_media_limited", _download_media_limited
        )

    @property
    def create_job_progress(self) -> Callable[..., Any]:
        if self.create_job_progress_fn is not None:
            return self.create_job_progress_fn
        progress_cls = _bot_attr("JobProgress", JobProgress)
        return progress_cls.create

    @property
    def flood_wait_error(self) -> type[BaseException]:
        return _bot_attr("FloodWaitError", FloodWaitError)

    @property
    def aiohttp_module(self) -> Any:
        return _bot_attr("aiohttp", aiohttp)

    def monotonic(self) -> float:
        if self.monotonic_fn is not None:
            return self.monotonic_fn()
        time_mod = _bot_attr("time", time)
        return float(time_mod.monotonic())

    def now_wall(self) -> float:
        time_mod = _bot_attr("time", time)
        return float(time_mod.time())

    @property
    def raw_fallback_max_pages(self) -> int:
        return int(_bot_attr("RAW_FALLBACK_MAX_PAGES", 40))

    @property
    def learning_progress_edit_timeout_seconds(self) -> float:
        return float(_bot_attr("LEARNING_PROGRESS_EDIT_TIMEOUT_SECONDS", 1.0))


DEFAULT_BOT_DEPS = BotDependencies()


def deps_for(owner: Any) -> BotDependencies:
    """Return ``owner.deps`` when present, else :data:`DEFAULT_BOT_DEPS`."""
    deps = getattr(owner, "deps", None)
    return deps if isinstance(deps, BotDependencies) else DEFAULT_BOT_DEPS
