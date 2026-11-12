"""Gamas Bot v4 — modular SOLID orchestrator for Telegram workflows.

Architecture
------------
``GamasBot`` is a thin orchestrator that wires collaborator services and
composes six single-responsibility domain workflow mixins:

* :mod:`gamas_bot.telegram_transfer` — MTProto parallel transfer, disk headroom
  guards, and stall detection.
* :mod:`gamas_bot.intake` — attachment classification, receipt validation, and
  extensible failure taxonomy (:func:`classify_failure` /
  :func:`register_failure_rule`).
* :mod:`gamas_bot.conversation_state` — :class:`ConversationStateStore`
  encapsulating per-user/per-admin wizard states and disarm rules.
* :mod:`gamas_bot.routing` — :class:`CallbackRouter` registry dispatching exact
  and longest-prefix callback data without monolithic ``if/elif`` ladders.
* :mod:`gamas_bot.bot_deps` — :class:`BotDependencies` injectable collaborator
  gateway (DIP) while preserving ``gamas_bot.bot`` module-level patch seams.
* :mod:`gamas_bot.bot_support` — :class:`SupportWorkflowMixin`.
* :mod:`gamas_bot.bot_wallet` — :class:`WalletWorkflowMixin`.
* :mod:`gamas_bot.bot_admin` — :class:`AdminWorkflowMixin`.
* :mod:`gamas_bot.bot_jobs` — :class:`JobPipelineMixin`.
* :mod:`gamas_bot.bot_documents` — :class:`DocumentOfficeWorkflowMixin`.
* :mod:`gamas_bot.bot_learning` — :class:`LearningWorkflowMixin`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import shutil
import time
from types import SimpleNamespace
from typing import Any
import weakref

import aiohttp
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageIdInvalidError, MessageNotModifiedError

from .artifacts import (
    KIND_BOOKLET,
    KIND_DOCX,
    KIND_FLASHCARDS,
    KIND_KEYPOINTS,
    KIND_QUIZ,
    KIND_TRANSCRIPT,
    KIND_TXT,
    delivery_markup,
    key_points_of,
)
from .billing import format_seconds_as_credits, plan_catalog
from .bot_admin import AdminWorkflowMixin
from .bot_deps import DEFAULT_BOT_DEPS, BotDependencies, deps_for
from .bot_documents import DocumentOfficeWorkflowMixin
from .bot_jobs import JobPipelineMixin
from .bot_learning import LearningWorkflowMixin
from .bot_referrals import ReferralWorkflowMixin
from .bot_support import SupportWorkflowMixin
from .bot_ui import (
    ACTIVE_LIMIT_MESSAGE,
    CATEGORY_FILTER_LABELS,
    CURATION_GUIDANCE,
    CURATION_STALE_ALERT,
    DEFAULT_INTAKE_SUFFIX,
    DOC_EXTRACTION_STOPPED,
    DOC_GENERATE_CARDS,
    DOC_GENERATE_QUESTIONS,
    DOC_OCR_FAILED,
    DOC_PHOTO_HINT,
    DOC_UNSUPPORTED_HINT,
    FLASHCARD_ASK_BACK,
    FLASHCARD_ASK_FRONT,
    FLASHCARD_EMPTY,
    FREE_TRY_BADGE,
    FREE_TRY_RESTORED,
    FREE_TRY_USED_NOTICE,
    LEADERBOARD_EMPTY,
    LEADERBOARD_FALLBACK_NAME,
    LEARNING_BUSY_ALERT,
    LEARNING_CREDIT_RESTORED,
    LEARNING_DELIVERY_CTA,
    LEARNING_GENERATION_FAILED,
    LEARNING_GENERATION_RETRY,
    LEARNING_LEGACY_NOTICE,
    LEARNING_NO_CREDIT,
    LEARNING_PROGRESS_EDIT_TIMEOUT_SECONDS,
    LEARNING_REFUND_PENDING_NOTICE,
    LEARNING_STALE_ALERT,
    MAX_OFFICE_OUTPUT_BYTES,
    MAX_OFFICE_SESSIONS,
    MAX_RAW_PREVIEW_LINES,
    OFFICE_UPLOAD_STOPPED,
    QUIZ_COUNTS,
    QUIZ_POINTS_PER_ANSWER,
    QUIZ_START_GUIDANCE,
    RAW_FALLBACK_MAX_PAGES,
    RAW_PREVIEW_CHARS,
    SUPPORT_CATEGORY_BUTTONS,
    SUPPORT_STATUS_LABELS,
    TELEGRAM_CAPTION_LIMIT,
    VIEW_LABELS,
)
from .bot_wallet import WalletWorkflowMixin
from .config import Settings, note_routes
from .conversation_state import ConversationStateStore
from .db import Database, iso
from .doc_extract import extract_document
from .docx_export import jalali_date
from .fileio import run_file_io
from .gate_resume import ResumedEvent
from .intake import (
    AUDIO_EXTENSIONS,
    FAILURE_KIND_LABELS_FA,
    FailureClassificationRule,
    MAX_RECEIPT_IMAGE_PIXELS,
    RECEIPT_IMAGE_EXTENSIONS,
    USER_VISIBLE_ERRORS,
    VIDEO_EXTENSIONS,
    _command_name,
    _document_metadata,
    _image_metadata,
    _is_receipt_image,
    _is_safe_receipt_path,
    _is_unsupported_attachment,
    _media_metadata,
    _normalize_receipt_image,
    _parse_telegram_user_id,
    _presentation_metadata,
    _safe_user_error_text,
    _sanitize_log_value,
    _text_file_metadata,
    _to_int,
    classify_failure,
    is_open_payment,
    is_pending_payment_review,
    raw_parts,
    register_failure_rule,
)
from .jobs import JobQueue
from .llm import complete_json
from .media import extract_audio_track, needs_transcode, probe_media
from .notes import structure_transcript
from .ocr import I2OCRClient
from .office import OfficeError, OfficeSession
from .presentations import load_presentation, prepare_audio
from .progress import JobProgress, WorkProgress, to_persian_digits
from .routing import CallbackRouter
from .stt import first_configured_stt_url, transcribe
from .subscription import (
    PENDING_GATE_TTL_DAYS,
    SUBSCRIPTION_PREFIX,
    SWEEP_ACTIVE_DAYS,
    SWEEP_BATCH_SIZE,
    SWEEP_INTERVAL_SECONDS,
    SWEEP_PAUSE_SECONDS,
    SWEEP_START_DELAY_SECONDS,
    SubscriptionService,
)
from .telegram_text import button_label, render_pages
from . import texts
from .telegram_transfer import (
    TELEGRAM_DOWNLOAD_CHUNK_BYTES,
    DiskSpaceError,
    DownloadStalledError,
    UploadTooLargeError,
    _download_media_limited,
    _download_media_ranges,
    _ensure_disk_headroom,
    _max_mtproto_part_size_kb,
    _message_file_size,
    _pwrite_all,
    _reported_upload_bytes,
    plan_download_ranges,
)

logger = logging.getLogger(__name__)


class GamasBot(
    SupportWorkflowMixin,
    WalletWorkflowMixin,
    AdminWorkflowMixin,
    JobPipelineMixin,
    DocumentOfficeWorkflowMixin,
    LearningWorkflowMixin,
    ReferralWorkflowMixin,
):
    """Main Telegram bot orchestrator composing single-responsibility workflows."""

    deps: BotDependencies = DEFAULT_BOT_DEPS

    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        client: Any | None = None,
        deps: BotDependencies | None = None,
    ) -> None:
        self.settings = settings
        self.deps = deps if deps is not None else BotDependencies()
        self.db = db if db is not None else Database(settings.db_path)
        self.queue = JobQueue(
            max_queued=settings.max_queued_jobs,
            max_active=settings.max_active_jobs,
            worker=self._run_job,
        )
        self._download_semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._media_semaphore = asyncio.Semaphore(settings.media_worker_concurrency)
        # Bound memory-heavy PDF/Office parsing separately from media workers.
        self._document_semaphore = asyncio.Semaphore(settings.media_worker_concurrency)
        self._document_locks: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._office_semaphore = asyncio.Semaphore(max(1, settings.office_job_concurrency))
        self._office_in_flight: set[int] = set()
        self._office_tasks: set[asyncio.Task[Any]] = set()
        self._office_last_use: dict[int, float] = {}
        self._office_session_cleanup_task: asyncio.Task[Any] | None = None
        self._upload_semaphore = asyncio.Semaphore(settings.max_concurrent_uploads)
        self._seen_messages: dict[tuple[int, int], float] = {}
        self._active_upload_keys: dict[tuple[int, str], str] = {}
        self._receipt_cleanup_task: asyncio.Task[Any] | None = None
        self._membership_sweep_task: asyncio.Task[Any] | None = None
        self._transcript_cleanup_task: asyncio.Task[Any] | None = None
        self._prewarm_task: asyncio.Task[Any] | None = None

        # Encapsulated conversation state store (SRP). Expose the underlying
        # dict/set attributes directly so workflow mixins and tests share one
        # canonical source of truth.
        self.conversations = ConversationStateStore()
        self._pending_payments = self.conversations.pending_payments
        self._pending_broadcast = self.conversations.pending_broadcast
        self._support_states = self.conversations.support_states
        self._support_admin_states = self.conversations.support_admin_states
        self._pending_reject_reason = self.conversations.pending_reject_reason
        self._pending_flashcard = self.conversations.pending_flashcard
        self._pending_flashcard_front = self.conversations.pending_flashcard_front
        self._pending_user_search = self.conversations.pending_user_search
        self._pending_free_reset = self.conversations.pending_free_reset
        self._curation = self.conversations.curation
        self._office_sessions: dict[int, OfficeSession] = self.conversations.office_sessions

        self._learning_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._learning_in_flight: set[int] = set()
        self._learning_global_semaphore = asyncio.Semaphore(settings.learning_global_concurrency)
        self._curation_tasks: dict[int, asyncio.Task[Any]] = {}
        self._learning_tasks: dict[int, asyncio.Task[Any]] = {}
        self._admission_tasks: set[asyncio.Task[Any]] = set()
        self._intake_tasks: set[asyncio.Task[Any]] = set()
        self._shutting_down = False
        self._admin_action_times: dict[tuple[int, str], float] = {}
        self._broadcast_running: set[int] = set()
        self._broadcast_previews: dict[int, tuple[str, int | None]] = {}
        self._broadcast_resume: dict[int, int] = {}
        self._broadcast_stop = asyncio.Event()
        self._ocr_client: I2OCRClient | None = None
        self._started_at = time.time()

        self.client = (
            client
            if client is not None
            else TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)
        )
        self.client.parse_mode = None
        self._http: aiohttp.ClientSession | None = None
        self.subscription = SubscriptionService(self, settings, self.db)

        self.callback_router = CallbackRouter()
        self._configure_callback_routes()
        self._register_handlers()

    # ------------------------------------------------------------------ boot
    def _prepare_private_dirs(self) -> None:
        """Create the bot's private directories and restrict them to 0700."""
        for directory in (self.settings.temp_dir, self.settings.receipt_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
            except OSError:
                logger.warning("could not prepare private directory %s", directory)
        try:
            self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("could not prepare Telegram session directory")

    async def run(self) -> None:
        await run_file_io(self._prepare_private_dirs)
        await self.db.connect()
        await self.db.sync_plans(
            [
                {
                    "code": plan.code,
                    "hours": plan.included_seconds / 3600,
                    "price": plan.price_toman,
                    "validity_days": plan.validity_days,
                }
                for plan in plan_catalog(self.settings.tariff_values())
                if not plan.is_free
            ]
        )
        await self.db.recover_interrupted()
        recovered = await self.db.recover_pending_free_uses()
        if recovered:
            logger.info("restored %s interrupted free learning claims", recovered)
        refunded = await self.db.recover_pending_learning_charges()
        if refunded:
            logger.info("refunded %s interrupted paid learning charges", refunded)
        await run_file_io(self._sweep_stale_temp_dirs)
        await self.db.clear_expired_pending_gate_actions(
            iso(datetime.now(timezone.utc) - timedelta(days=PENDING_GATE_TTL_DAYS))
        )
        self.queue.ensure_workers()
        self._receipt_cleanup_task = asyncio.create_task(self._receipt_cleanup_loop())
        self._transcript_cleanup_task = asyncio.create_task(self._transcript_cleanup_loop())
        # Support prompts share this bounded periodic sweep even when Office is disabled.
        self._office_session_cleanup_task = asyncio.create_task(
            self._office_session_cleanup_loop()
        )
        if not self.settings.bot_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set; refusing interactive Telethon login."
            )
        await self.client.start(bot_token=self.settings.bot_token)
        await self._recover_broadcasts()
        logger.info("Gamas Bot v3 started admins=%d", len(self.settings.admin_ids))
        await self.subscription.startup_self_check()
        if self.subscription.required and self.subscription.configured:
            self._membership_sweep_task = asyncio.create_task(
                self._membership_sweep_loop()
            )
        if not self.settings.payment_card_number:
            logger.warning("PAYMENT_CARD_NUMBER is not set; card-to-card payments are disabled")
        await self.client.run_until_disconnected()

    def _sweep_stale_temp_dirs(self) -> None:
        """Remove work directories left behind by a crash (audit F-07)."""
        root = self.settings.temp_dir
        if not root.is_dir():
            return
        removed = 0
        for entry in root.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not entry.name.startswith(("submission-", "deck-", "doc-")):
                continue
            try:
                if entry.stat().st_mtime >= self._started_at:
                    continue
                shutil.rmtree(entry)
                removed += 1
            except OSError:
                logger.warning("could not remove stale temp dir %s", entry.name)
        if removed:
            logger.info("removed %d stale temp dir(s) at startup", removed)

    async def shutdown(self) -> None:
        self._shutting_down = True
        current = asyncio.current_task()
        admissions = [
            task
            for task in self._admission_tasks | self._intake_tasks
            if task is not current and not task.done()
        ]
        for task in admissions:
            task.cancel()
        if admissions:
            await asyncio.gather(*admissions, return_exceptions=True)

        learning = [
            task
            for task in set(self._learning_tasks.values())
            if task is not current and not task.done()
        ]
        for task in learning:
            task.cancel()
        if learning:
            await asyncio.gather(*learning, return_exceptions=True)

        office = [
            task for task in self._office_tasks if task is not current and not task.done()
        ]
        for task in office:
            task.cancel()
        if office:
            await asyncio.gather(*office, return_exceptions=True)
        self._office_sessions.clear()
        self._office_last_use.clear()

        timers = list(self._curation_tasks.values())
        self._curation_tasks.clear()
        for task in timers:
            task.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        for user_id, state in list(self._curation.items()):
            async with self._learning_lock(user_id):
                await self._release_curation(user_id, state)

        background = [
            task
            for task in (
                self._transcript_cleanup_task,
                self._receipt_cleanup_task,
                self._prewarm_task,
                self._office_session_cleanup_task,
                self._membership_sweep_task,
            )
            if task is not None
        ]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._transcript_cleanup_task = None
        self._receipt_cleanup_task = None
        self._prewarm_task = None
        self._office_session_cleanup_task = None
        self._membership_sweep_task = None

        if self._ocr_client is not None:
            await run_file_io(self._ocr_client.close)
            self._ocr_client = None

        async def _fail_queued(job) -> None:
            try:
                await self.db.set_submission_status(
                    job.submission_id,
                    "failed",
                    error="interrupted by restart",
                    error_kind="cancelled",
                )
                if job.message is not None:
                    try:
                        message_id = getattr(job.message, "id", None)
                        notification = texts.RESTARTING_JOB_CANCELLED
                        if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
                            notification += "\n" + texts.RESTARTING_JOB_MESSAGE_REFERENCE.format(
                                message_id=to_persian_digits(message_id)
                            )
                        await asyncio.wait_for(
                            job.message.reply(notification),
                            timeout=2.0,
                        )
                    except Exception:
                        pass
            except Exception:
                logger.warning(
                    "failed to mark queued job as failed submission=%s", job.submission_id
                )
            finally:
                fp = self._upload_fingerprint(job.message)
                key = (job.user_id, fp) if fp is not None else None
                if key is not None and self._active_upload_keys.get(key) == job.job_id:
                    self._active_upload_keys.pop(key, None)
                await self._close_cancelled_progress(job)

        await self.queue.shutdown(on_reject=_fail_queued)
        if self._prewarm_task is not None:
            self._prewarm_task.cancel()
            await asyncio.gather(self._prewarm_task, return_exceptions=True)
            self._prewarm_task = None
        if self._http is not None:
            await self._http.close()
            self._http = None
        await self.subscription.close()
        await self.client.disconnect()
        await self.db.close()

    def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or getattr(self._http, "closed", False):
            aiohttp_mod = deps_for(self).aiohttp_module
            connector = aiohttp_mod.TCPConnector(
                limit=self.settings.llm_connector_limit,
                limit_per_host=self.settings.llm_connector_limit_per_host,
                ttl_dns_cache=self.settings.llm_connector_ttl_dns_cache,
                keepalive_timeout=float(self.settings.llm_connector_keepalive),
            )
            self._http = aiohttp_mod.ClientSession(
                timeout=aiohttp_mod.ClientTimeout(
                    total=None, sock_read=self.settings.stt_job_timeout
                ),
                connector=connector,
            )
            if self.settings.llm_prewarm and not self._shutting_down:
                self._schedule_llm_prewarm()
        return self._http

    def _schedule_llm_prewarm(self) -> None:
        """Warm connections to the primary LLM and STT hosts (no quota used)."""
        if self._shutting_down:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._prewarm_task is None or self._prewarm_task.done():
            self._prewarm_task = loop.create_task(self._prewarm_llm())

    async def _prewarm_llm(self) -> None:
        session = self._http
        if session is None:
            return
        urls: list[str] = []
        try:
            routes = note_routes(self.settings)
            if routes:
                urls.append(routes[0].base_url)
            stt_url = first_configured_stt_url(self.settings)
            if stt_url and stt_url not in urls:
                urls.append(stt_url)
        except Exception:
            logger.debug("HTTP pre-warm route lookup failed (ignored)", exc_info=True)
            return
        if not urls:
            return

        async def _warm_url(url: str) -> None:
            try:
                logger.debug("pre-warming HTTP connection host=%s", url)
                async with session.head(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    await response.read()
            except Exception:
                logger.debug("HTTP connection pre-warm failed (ignored)", exc_info=True)

        await asyncio.gather(*(_warm_url(url) for url in urls))

    # -------------------------------------------------------------- handlers
    def _register_handlers(self) -> None:
        client = self.client
        if not hasattr(client, "on"):
            return

        @client.on(events.CallbackQuery)
        async def handle_callback(event):
            try:
                await self._handle_callback(event)
            except MessageNotModifiedError:
                pass
            except Exception:
                logger.exception("callback failed")
                await self._ack(event, texts.CALLBACK_FAILED, alert=True)

        @client.on(events.NewMessage)
        async def handle_message(event):
            try:
                await self._handle_message(event)
            except FloodWaitError:
                raise
            except Exception:
                logger.exception("message handler failed")
                try:
                    await event.reply(texts.UNEXPECTED_ERROR)
                except Exception:
                    pass

    async def _ack(self, event, text: str | None = None, *, alert: bool = False) -> None:
        if getattr(event, "_gamas_answered", False) or getattr(event, "_answered", False):
            return
        try:
            event._gamas_answered = True
            await event.answer(text, alert=alert)
        except Exception:
            logger.debug("callback answer failed", exc_info=True)

    async def _edit(self, event, text: str, buttons=None) -> None:
        """Edit a callback message; fall back to sending a new one."""
        try:
            await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            pass
        except Exception:
            logger.debug("callback edit failed; responding instead", exc_info=True)
            try:
                await event.respond(text, buttons=buttons)
            except Exception:
                logger.warning("callback screen could not be delivered", exc_info=True)

    # ----------------------------------------------------------------- menus
    def _reply_keyboard(self, *, is_admin: bool) -> Any:
        """The persistent bottom keyboard attached to the menu message.

        ``resize_keyboard`` keeps it one line tall; ``is_persistent`` asks
        Telegram to keep showing it once the user starts typing instead of
        hiding it behind the keyboard toggle, which is what makes it a real
        navigation bar rather than a one-off prompt.
        """
        from telethon.tl.types import (
            ButtonTypeDefault,
            ButtonTypeSimpleWebView,
            KeyboardButton,
            KeyboardButtonRow,
            ReplyKeyboardMarkup,
        )

        rows = [
            KeyboardButtonRow(
                buttons=[
                    KeyboardButton(text=label, type=ButtonTypeDefault())
                    for label in row
                ]
            )
            for row in texts.reply_menu_rows(
                is_admin=is_admin, office_enabled=self.settings.office_enabled
            )
        ]
        # A reply-keyboard WebView button opens Gamas Studio inside Telegram.
        # It is added only when the app is configured, so a deployment without
        # a Mini App never shows a button that cannot open (no dead buttons).
        if self.settings.mini_app_url:
            rows.append(
                KeyboardButtonRow(
                    buttons=[
                        KeyboardButton(
                            text=texts.BTN_STUDIO,
                            type=ButtonTypeSimpleWebView(self.settings.mini_app_url),
                        )
                    ]
                )
            )
        return ReplyKeyboardMarkup(
            rows=rows,
            resize=True,
            persistent=True,
            placeholder=texts.REPLY_KEYBOARD_PLACEHOLDER,
        )

    def _user_menu(self, *, is_admin: bool = False) -> list:
        """The user panel: library, wallet, optional free Office, support and help."""
        rows = [
            [
                Button.inline(texts.BTN_LIBRARY, b"library"),
                Button.inline(texts.BTN_WALLET, b"wallet"),
            ],
        ]
        if self.settings.office_enabled:
            rows.append(
                [
                    Button.inline(texts.BTN_OFFICE, b"office:start"),
                    Button.inline(texts.BTN_LEADERBOARD, b"board"),
                ]
            )
        else:
            rows.append([Button.inline(texts.BTN_LEADERBOARD, b"board")])
        rows.append(
            [
                Button.inline(texts.BTN_SUPPORT, b"support"),
                Button.inline(texts.BTN_HELP, b"help"),
            ]
        )
        rows.append([Button.inline(texts.BTN_REFERRALS, b"ref:home")])
        if is_admin:
            rows.append([Button.inline(texts.BTN_ADMIN, b"adm:home")])
        return rows

    def _admin_menu(self) -> list:
        """The admin panel: three hubs, unrelated sections, and the way back."""
        return [
            [
                Button.inline(texts.BTN_ADMIN_STATS, b"adm:home"),
                Button.inline(texts.BTN_ADMIN_PAYMENTS, b"adm:hub:pay"),
            ],
            [
                Button.inline(texts.BTN_ADMIN_USERS, b"adm:hub:usr"),
                Button.inline(texts.BTN_ADMIN_SYSTEM, b"adm:hub:sys"),
            ],
            [Button.inline(texts.BTN_ADMIN_SUPPORT, b"adm:sup")],
            [
                Button.inline(texts.BTN_ADMIN_BROADCAST, b"adm:bcast"),
                Button.inline(texts.BTN_ADMIN_AUDIT, b"adm:audit"),
            ],
            [Button.inline(texts.BTN_ADMIN_TOP_USERS, b"adm:board")],
            [Button.inline(texts.BTN_ADMIN_REFERRALS, b"adm:ref")],
            [Button.inline(texts.BTN_ADMIN_BACK_USER, b"menu:main")],
        ]

    async def _show_main(self, event, *, first_start: bool = False) -> None:
        user_id = event.sender_id
        await self.db.ensure_user(user_id)
        if await self.db.grant_free_plan_once(user_id, self._free_plan_seconds()):
            logger.info("free plan granted")
        text = texts.main_screen(
            first_start=first_start,
            balance=await self._balance_text(user_id),
            office_enabled=self.settings.office_enabled,
            notes_configured=self.settings.note_configured,
            streak_days=await self.db.activity_streak(user_id),
        )
        await self._edit_or_reply(
            event,
            text,
            self._user_menu(is_admin=self._is_admin(user_id)),
            # A reply keyboard is set once and stays: attaching it again on every
            # screen would turn every menu tap into a new message (Telegram
            # cannot add a keyboard by editing), which is exactly the clutter the
            # one-message results screen removes. It rides on the /start message
            # and is re-attached whenever the user sends text.
            reply_markup=None
            if hasattr(event, "data")
            else self._reply_keyboard(is_admin=self._is_admin(user_id)),
        )

    # -------------------------------------------------------- membership gate
    async def _require_membership(
        self,
        event,
        user_id: int,
        *,
        kind: str,
        callback_data: str = "",
    ) -> bool:
        """Gate one value-delivery action; keep the request if it is blocked.

        Help, wallet, support, library browsing and receipt upload never call
        this. Intake and generation do. A blocked request is remembered so the
        successful ``sub:check`` resumes exactly what the user started.
        """
        if self._is_admin(user_id):
            return True
        if await self.subscription.require_access(event):
            return True
        await self._remember_pending_gate(
            event, user_id, kind=kind, callback_data=callback_data
        )
        return False

    async def _remember_pending_gate(
        self, event, user_id: int, *, kind: str, callback_data: str = ""
    ) -> None:
        message_id = getattr(getattr(event, "message", None), "id", None)
        if not isinstance(message_id, int):
            message_id = getattr(event, "message_id", 0)
        chat_id = getattr(event, "chat_id", None)
        if not isinstance(chat_id, int):
            chat_id = getattr(getattr(event, "message", None), "chat_id", None)
        if not isinstance(chat_id, int):
            chat_id = user_id
        if not isinstance(message_id, int):
            return
        try:
            await self.db.save_pending_gate_action(
                user_id,
                chat_id=chat_id,
                message_id=message_id,
                kind=kind,
                callback_data=callback_data[:64],
            )
        except Exception:  # noqa: BLE001 - remembering is best effort
            logger.warning("could not remember a gate-blocked request", exc_info=True)

    async def _resume_pending_gate_action(self, user_id: int) -> bool:
        """Re-enter a blocked request after a successful membership check."""
        pending = await self.db.take_pending_gate_action(user_id)
        if not pending:
            return False
        try:
            created = datetime.fromisoformat(pending["created_at"])
        except (TypeError, ValueError):
            created = datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < datetime.now(timezone.utc) - timedelta(
            days=PENDING_GATE_TTL_DAYS
        ):
            return False
        if int(pending["message_id"]) > 0:
            try:
                message = await self.client.get_messages(
                    int(pending["chat_id"]), ids=int(pending["message_id"])
                )
            except Exception:  # noqa: BLE001 - a lost message is not a crash
                logger.warning("could not reload a gate-blocked message", exc_info=True)
                message = None
        else:
            # Callback actions need no message content; the button can be
            # dispatched directly from the stored callback data.
            message = SimpleNamespace(id=0)
        if message is None:
            return False
        resumed = ResumedEvent(
            client=self.client,
            chat_id=int(pending["chat_id"]),
            sender_id=user_id,
            message=message,
            data=(
                pending["callback_data"].encode("utf-8")
                if pending["kind"] == "callback" and pending["callback_data"]
                else None
            ),
        )
        logger.info("resuming a gate-blocked request kind=%s", pending["kind"])
        if pending["kind"] == "callback":
            await self._handle_callback(resumed)
        else:
            await self._handle_message(resumed)
        return True

    # ------------------------------------------------------ membership sweep
    async def _membership_sweep_loop(self) -> None:
        """Bounded, resumable re-checks of recently active users."""
        await asyncio.sleep(SWEEP_START_DELAY_SECONDS)
        while True:
            try:
                await self._membership_sweep_batch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sweep must never stop the bot
                logger.warning("membership sweep batch failed", exc_info=True)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    async def _membership_sweep_batch(self) -> int:
        """Check one small batch; an inconclusive result ends the batch."""
        active_since = iso(
            datetime.now(timezone.utc) - timedelta(days=SWEEP_ACTIVE_DAYS)
        )
        user_ids = await self.db.users_for_membership_sweep(
            SWEEP_BATCH_SIZE, active_since=active_since
        )
        checked = 0
        for user_id in user_ids:
            if self._shutting_down:
                break
            result = await self.subscription.check_membership(
                user_id, force=True, allow_cache=False
            )
            await self.subscription.record_result(user_id, result, touch_checked_at=True)
            checked += 1
            if result.is_unknown:
                logger.warning("membership sweep paused on an inconclusive check")
                break
            await asyncio.sleep(SWEEP_PAUSE_SECONDS)
        return checked

    async def _edit_or_reply(
        self, event, text: str, buttons=None, *, reply_markup: Any = None
    ) -> None:
        """Render a screen: edit when triggered by a button, reply when by a message.

        ``reply_markup`` is the *bottom* keyboard. Telethon delivers either an
        inline keyboard or a reply keyboard through the same ``buttons``
        argument, and Telegram cannot attach a reply keyboard by editing — so a
        request for one always sends a fresh message, while an ordinary edit
        keeps editing in place (the client already shows the keyboard).
        """
        if reply_markup is not None:
            await event.reply(text, buttons=reply_markup)
            return
        if hasattr(event, "data"):
            await self._edit(event, text, buttons)
            return
        await event.reply(text, buttons=buttons)

    def _is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.admin_ids

    async def _capture_sender_name(self, event) -> None:
        """Best-effort leaderboard name capture; a lookup failure is invisible."""
        user_id = event.sender_id
        if user_id is None:
            return
        try:
            sender = getattr(event, "sender", None)
            if sender is None and hasattr(event, "get_sender"):
                sender = await event.get_sender()
            if sender is None:
                return
            first_name = getattr(sender, "first_name", None)
            username = getattr(sender, "username", None)
            last_name = getattr(sender, "last_name", None)
            await self.db.update_identity(
                user_id,
                first_name=" ".join(str(first_name or "").split())[:64],
                username=username or "",
                last_name=" ".join(str(last_name or "").split())[:64],
            )
        except Exception:
            logger.debug("could not capture sender name", exc_info=True)

    async def _balance_text(self, user_id: int) -> str:
        """The balance as users see it: a special user has no limit to show."""
        if await self.db.is_unlimited(user_id):
            return texts.UNLIMITED_BALANCE
        return format_seconds_as_credits(await self.db.available_seconds(user_id))

    def _free_plan_seconds(self) -> int:
        for plan in plan_catalog(self.settings.tariff_values()):
            if plan.is_free:
                return plan.included_seconds
        return 3600

    # --------------------------------------------------------------- library
    async def _show_library(self, event, *, page: int = 0) -> None:
        """Show six newest Telegram artifacts; the user's id scopes every read."""
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 6
        count = await self.db.library_count(user_id)
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        items = await self.db.library_list(user_id, limit=page_size, offset=page * page_size)
        if count == 0:
            text = texts.library_empty(notes_configured=self.settings.note_configured)
            await self._edit_or_reply(
                event,
                text,
                [
                    [
                        Button.inline(texts.BTN_BOOKLETS, b"library"),
                        Button.inline(texts.BTN_DOCUMENTS, b"lib:docs:0"),
                    ],
                    [Button.inline(texts.BTN_FLASHCARDS, b"library:fc")],
                    [Button.inline(texts.BTN_WALLET, b"wallet")],
                    [Button.inline(texts.BTN_BACK_MAIN, b"menu:main")],
                ],
            )
            return

        lines = [
            texts.LIBRARY_TITLE,
            "",
            texts.LIBRARY_COUNT_TEMPLATE.format(count=to_persian_digits(count)),
        ]
        buttons: list[list] = []
        for item in items:
            title = self._item_display_title(item)
            label = f"{self._library_icon(str(item['kind']))} {title}"
            buttons.append(
                [
                    Button.inline(
                        button_label(label),
                        f"lib:it:{item['item_id']}".encode("ascii"),
                    )
                ]
            )
        nav = []
        if page > 0:
            nav.append(Button.inline(texts.BTN_PREV, f"lib:{page - 1}".encode("ascii")))
        if (page + 1) * page_size < count:
            nav.append(Button.inline(texts.BTN_NEXT, f"lib:{page + 1}".encode("ascii")))
        if nav:
            buttons.append(nav)
        buttons.extend(
            [
                [
                    Button.inline(texts.BTN_BOOKLETS, b"library"),
                    Button.inline(texts.BTN_DOCUMENTS, b"lib:docs:0"),
                ],
                [Button.inline(texts.BTN_FLASHCARDS, b"library:fc")],
                [Button.inline(texts.BTN_WALLET, b"wallet")],
                [Button.inline(texts.BTN_BACK_MAIN, b"menu:main")],
            ]
        )
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _show_library_item(self, event, item_id: int) -> None:
        """Booklet/transcript detail: re-download plus learning entry points."""
        user_id = event.sender_id
        if user_id is None:
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None:
            await self._ack(event, texts.ITEM_UNAVAILABLE, alert=True)
            return
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._show_document_card(event, user_id, int(document["id"]))
            return
        title = self._item_display_title(item)
        try:
            date_label = jalali_date(datetime.fromisoformat(item["created_at"]))
        except (TypeError, ValueError):
            date_label = ""
        header = (
            f"{self._library_icon(str(item['kind']))} {title}"
            + (f"\n📅 {date_label}" if date_label else "")
        )
        buttons: list[list] = [
            [Button.inline(texts.BTN_REDOWNLOAD, f"lib:f:{item_id}".encode("ascii"))],
        ]
        if item["kind"] == "booklet":
            if item.get("content_text") and self.settings.note_configured:
                buttons.append(
                    [
                        Button.inline(
                            texts.BTN_QUIZ_FROM_BOOKLET,
                            f"quiz:new:{item_id}".encode("ascii"),
                        )
                    ]
                )
                buttons.append(
                    [
                        Button.inline(
                            texts.BTN_CARDS_FROM_BOOKLET,
                            f"fc:ai:{item_id}".encode("ascii"),
                        )
                    ]
                )
            elif item.get("content_text"):
                header += "\n\n" + texts.LEARNING_GENERATION_UNAVAILABLE
            else:
                header += "\n\n" + LEARNING_LEGACY_NOTICE
        buttons.append([Button.inline(texts.BTN_BACK, b"lib:0")])
        await self._edit(event, header, buttons)

    async def _forward_library_item(self, event, user_id: int, item_id: int) -> None:
        """Re-forward one artifact only after an owner-scoped lookup."""
        item = await self.db.library_item(user_id, item_id)
        if item is None:
            await self._ack(event, texts.ITEM_UNAVAILABLE, alert=True)
            return
        # New booklets are persisted byte artifacts, not automatically posted
        # Telegram files.  Library re-download therefore sends the stored PDF
        # directly; legacy rows continue through the forward path below.
        submission_id = int(item.get("submission_id") or 0)
        if item.get("kind") == "booklet" and submission_id:
            binary_kinds = await self.db.binary_artifact_kinds(submission_id, user_id)
            kind = KIND_BOOKLET if KIND_BOOKLET in binary_kinds else KIND_DOCX
            if kind in binary_kinds:
                if await self._fetch_persisted_booklet(
                    event, user_id, submission_id, kind, f"GMS-{submission_id}"
                ):
                    return
        back = [[Button.inline(texts.BTN_BACK, f"lib:it:{item_id}".encode("ascii"))]]
        progress = WorkProgress.for_editor(
            lambda text: self._edit(event, text, back),
            headline=texts.ITEM_FORWARD_HEADLINE,
            done_headline=texts.ITEM_FORWARDED_HEADLINE,
        )
        await progress.update(
            35, texts.ITEM_FORWARDING_STEP, texts.ITEM_FORWARDING_DETAIL
        )
        try:
            forwarded = await self.client.forward_messages(
                event.chat_id,
                [int(item["message_id"])],
                from_peer=int(item["chat_id"]),
            )
            if not forwarded or (isinstance(forwarded, (list, tuple)) and not any(forwarded)):
                raise MessageIdInvalidError(request=None)
        except MessageIdInvalidError:
            try:
                await self.db.library_mark_unavailable(item_id)
            except Exception:
                logger.warning(
                    "could not mark unavailable library item=%s", item_id, exc_info=True
                )
            await progress.fail(
                texts.ITEM_UNAVAILABLE,
                headline=texts.ITEM_UNAVAILABLE_FAILED,
            )
            await self._ack(event, texts.ITEM_UNAVAILABLE, alert=True)
            return
        except Exception:
            logger.warning("library forward failed item=%s", item_id, exc_info=True)
            await progress.fail(
                texts.ITEM_FORWARD_FAILED,
                headline=texts.ITEM_UNAVAILABLE_FAILED,
            )
            await self._ack(event, texts.ITEM_FORWARD_FAILED, alert=True)
            return
        if item["unavailable"]:
            try:
                await self.db.library_mark_available(item_id)
            except Exception:
                logger.warning(
                    "could not clear library unavailable flag item=%s", item_id, exc_info=True
                )
        await progress.complete(texts.ITEM_FORWARD_DONE)

    # -------------------------------------------------------- callback router
    def _configure_callback_routes(self) -> None:
        """Register exact and prefix callback handlers on :attr:`callback_router`."""
        r = self.callback_router

        r.register_exact("help", self._cb_help)
        r.register_exact("menu:main", lambda e, _u, _d: self._show_main(e))
        r.register_exact("library", lambda e, _u, _d: self._show_library(e))
        r.register_prefix("res:", self._cb_result_set)
        r.register_exact("library:fc", lambda e, _u, _d: self._show_flashcard_hub(e))
        r.register_exact("board", lambda e, _u, _d: self._show_leaderboard(e))
        r.register_exact("wallet", lambda e, _u, _d: self._show_wallet(e))
        r.register_exact("support", self._handle_support_callback)
        r.register_exact("adm:home", lambda e, _u, _d: self._show_admin_overview(e))
        r.register_exact("adm:users", lambda e, _u, _d: self._show_admin_users(e))
        r.register_exact("adm:pay", lambda e, _u, _d: self._show_admin_payments(e, 0))
        r.register_exact("adm:bcast", self._cb_admin_broadcast)
        r.register_exact("adm:bcgo", self._cb_broadcast_go)
        r.register_exact("adm:bcstop", self._cb_broadcast_stop)
        r.register_exact("adm:bccancel", self._cb_broadcast_cancel)
        r.register_prefix("adm:bcresume:", self._cb_broadcast_resume)
        r.register_exact("adm:audit", lambda e, _u, _d: self._show_admin_audit(e, 0))
        r.register_exact("adm:board", lambda e, _u, _d: self._admin_board(e))
        r.register_exact("ref:home", lambda e, u, _d: self._show_referrals(e, u))
        r.register_exact("adm:ref", lambda e, _u, _d: self._show_admin_referrals(e))
        r.register_exact("adm:sup", self._handle_admin_support_callback)

        r.register_prefix("lib:docs", self._cb_lib_docs)
        r.register_prefix("lib:doc:", self._cb_lib_doc)
        r.register_prefix("lib:f:", self._cb_lib_forward)
        r.register_prefix("lib:it:", self._cb_lib_item)
        r.register_prefix("lib:", self._cb_lib_page)
        r.register_prefix("office:", self._route_office_callback)
        r.register_prefix("doc:", self._route_document_callback)
        r.register_prefix("walletp:", self._cb_wallet_page)
        r.register_prefix("plan:", lambda e, _u, d: self._start_payment(e, d.split(":", 1)[1]))
        r.register_prefix("payr:", self._cb_payment_action)
        r.register_prefix("payc:", self._cb_payment_action)
        r.register_prefix("payopen:", self._cb_payment_action)
        r.register_prefix("payv:", lambda e, _u, d: self._review_payment(e, d))
        r.register_prefix("adm:ov", self._cb_admin_overview)
        r.register_prefix("adm:hub:", self._route_admin_hub)
        r.register_prefix("adm:pay:", self._cb_admin_pay_page)
        r.register_prefix("adm:ph:", self._cb_admin_pay_history)
        r.register_prefix("adm:pd:", self._cb_admin_pay_detail)
        r.register_prefix("adm:audit:", self._cb_admin_audit_page)
        r.register_prefix("adm:bgok:", self._cb_admin_gift_exec)
        r.register_prefix("adm:bg:", self._cb_admin_gift)
        r.register_prefix("adm:rcpt:", lambda e, _u, d: self._show_receipt(e, d))
        for adm_sup_prefix in ("adm:sl:", "adm:sv:", "adm:sr:", "adm:sc:", "adm:so:"):
            r.register_prefix(adm_sup_prefix, self._handle_admin_support_callback)
        r.register_prefix("sup:", self._handle_support_callback)
        r.register_prefix("art:", self._route_artifact_callback)
        r.register_prefix("quiz:", self._route_quiz_callback)
        r.register_prefix("fc:", self._route_flashcard_callback)

    async def _route_reply_menu(self, event, user_id: int, text: str) -> bool:
        """Run the screen behind a bottom-keyboard label; ``False`` if not one."""
        label = text.strip()
        routes = {
            texts.BTN_START_HERE: self._show_main,
            texts.BTN_MY_RESULTS: self._show_results_hub,
            texts.BTN_LIBRARY: lambda e: self._show_library(e),
            texts.BTN_UPGRADE: lambda e: self._show_wallet(e),
            texts.BTN_LEADERBOARD: lambda e: self._show_leaderboard(e),
            texts.BTN_OFFICE: lambda e: self._route_office_callback(e, user_id, "office:start"),
            texts.BTN_SUPPORT: lambda e: self._show_support_home(e),
            texts.BTN_REFERRALS: lambda e: self._show_referrals(e, user_id),
        }
        if label == texts.BTN_HELP or label == texts.BTN_MENU_GUIDE:
            await self._cb_help(event, user_id, "")
            return True
        if label == texts.BTN_ADMIN and self._is_admin(user_id):
            # The same screen the inline "adm:home" route renders: one admin
            # panel, reached by two paths, with no second implementation to drift.
            await self._show_admin_overview(event)
            return True
        handler = routes.get(label)
        if handler is None:
            return False
        await handler(event)
        return True

    async def _show_results_hub(self, event, *, page: int = 0) -> None:
        """Page through the user's submission history and artifact sets."""
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 8
        count = await self.db.user_submission_count(user_id)
        if not count:
            await self._edit_or_reply(
                event,
                texts.results_hub_empty(
                    notes_configured=self.settings.note_configured
                ),
                [[Button.inline(texts.BTN_BACK_MAIN, b"menu:main")]],
            )
            return
        total_pages = (count + page_size - 1) // page_size
        page = min(max(0, int(page)), total_pages - 1)
        rows = await self.db.user_submissions(
            user_id, limit=page_size, offset=page * page_size
        )
        buttons: list[list[Any]] = []
        for row in rows:
            reference = f"GMS-{row['submission_id']}"
            label = texts.RESULTS_HUB_ROW_TEMPLATE.format(
                reference=reference,
                status=texts.RESULTS_STATUS.get(str(row["status"]), str(row["status"])),
            )
            buttons.append(
                [Button.inline(label, f"res:{row['submission_id']}".encode("ascii"))]
            )
        navigation = []
        if page > 0:
            navigation.append(
                Button.inline(texts.BTN_PREV, f"res:page:{page - 1}".encode("ascii"))
            )
        if page + 1 < total_pages:
            navigation.append(
                Button.inline(texts.BTN_NEXT, f"res:page:{page + 1}".encode("ascii"))
            )
        if navigation:
            buttons.append(navigation)
        buttons.append([Button.inline(texts.BTN_BACK_MAIN, b"menu:main")])
        title = (
            texts.RESULTS_HUB_TITLE
            + "\n\n"
            + texts.RESULTS_HUB_PAGE_TEMPLATE.format(
                page=to_persian_digits(page + 1),
                pages=to_persian_digits(total_pages),
            )
        )
        await self._edit_or_reply(event, title, buttons)

    async def _cb_result_set(self, event, user_id: int, data: str) -> None:
        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "page":
            page = _to_int(parts[2])
            if page is None or page < 0:
                await self._ack(event, texts.INVALID_REQUEST, alert=True)
                return
            await self._show_results_hub(event, page=page)
            return
        raw = parts[1].strip() if len(parts) == 2 else ""
        if not raw.isdigit():
            await self._ack(event)
            return
        await self._show_result_set(event, user_id, int(raw))

    async def _show_result_set(self, event, user_id: int, submission_id: int) -> None:
        """One submission's artifacts, for a user coming back later."""
        row = await self.db.submission(submission_id)
        if row is None or int(row.user_id) != int(user_id):
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return
        reference = f"GMS-{submission_id}"
        books = await self.db.library_list_kind(user_id, "booklet", limit=50)
        transcript = await self.db.transcription(submission_id)
        has_text = bool(transcript and str(transcript.get("text") or "").strip())
        has_keypoints = bool(
            transcript and key_points_of(transcript.get("notes_json"))
        )
        # The central registry is the first source of truth, but it is
        # reconciled with the rows the buttons actually read. A partial
        # registry write (or a pre-026 deployment) must not hide a real
        # download, and a stale registration must not advertise a dead one.
        try:
            kinds = set(await self.db.artifact_kinds(submission_id))
        except Exception:
            logger.warning(
                "artifact registry read failed submission=%s", submission_id,
                exc_info=True,
            )
            kinds = set()
        has_booklet = KIND_BOOKLET in kinds
        if not has_booklet:
            # Pre-027 Word booklets are Telegram-hosted library rows. Keep the
            # old file reachable, while new PDFs always come from stored bytes.
            has_booklet = any(
                int(item.get("submission_id") or 0) == submission_id
                and int(item.get("chat_id") or 0)
                and int(item.get("message_id") or 0)
                and not item.get("unavailable")
                for item in books
            )
            if has_booklet:
                kinds.add(KIND_BOOKLET)
        if has_text:
            kinds.update({KIND_TRANSCRIPT, KIND_TXT})
        else:
            kinds.difference_update({KIND_TRANSCRIPT, KIND_TXT})
        if has_keypoints:
            kinds.add(KIND_KEYPOINTS)
        else:
            kinds.discard(KIND_KEYPOINTS)
        item_id: int | None = None
        if has_booklet:
            match = next(
                (
                    item
                    for item in books
                    if int(item.get("submission_id") or 0) == submission_id
                    and not item.get("unavailable")
                ),
                None,
            )
            if match is not None:
                item_id = int(match["item_id"])
            # The booklet is forwardable even if the optional library lookup
            # for learning buttons failed; only quiz/flashcards need item_id.
            kinds.add(KIND_BOOKLET)
            if item_id is not None:
                kinds.update({KIND_QUIZ, KIND_FLASHCARDS})
            else:
                kinds.difference_update({KIND_QUIZ, KIND_FLASHCARDS})
        else:
            kinds.difference_update({KIND_BOOKLET, KIND_QUIZ, KIND_FLASHCARDS})
        buttons = delivery_markup(
            reference=reference,
            speakers=0,
            has_booklet=has_booklet,
            kinds=kinds,
            item_id=item_id,
            mini_app_url=self.settings.mini_app_url,
            learning_available=self.settings.note_configured,
        )
        await self._edit(
            event,
            texts.delivery_screen(
                reference=reference,
                speakers=0,
                has_booklet=has_booklet,
                has_keypoints=has_keypoints,
                learning_available=self.settings.note_configured,
            ),
            buttons,
        )

    async def _route_artifact_callback(self, event, user_id: int, data: str) -> None:
        """One results button: hand the user the artifact they asked for."""
        if not await self._require_membership(
            event, user_id, kind="artifact", callback_data=data
        ):
            return
        await self._fetch_artifact(event, user_id, data)

    async def _cb_help(self, event, _user_id: int, _data: str) -> None:
        await event.respond(
            texts.help_screen(
                office_enabled=self.settings.office_enabled,
                notes_configured=self.settings.note_configured,
                ocr_enabled=self.settings.ocr_enabled,
            ),
            buttons=[[Button.inline(texts.BTN_BACK_MAIN, b"menu:main")]],
        )

    async def _cb_lib_docs(self, event, _user_id: int, data: str) -> None:
        parts = data.split(":")
        page = _to_int(parts[2]) if len(parts) > 2 else 0
        if page is None:
            await self._ack(event, texts.DOCS_INVALID_PAGE, alert=True)
            return
        await self._show_documents(event, page=max(0, page))

    async def _cb_lib_doc(self, event, user_id: int, data: str) -> None:
        document_id = _to_int(data.rsplit(":", 1)[-1])
        if document_id is None:
            await self._ack(event, texts.DOCUMENT_NOT_FOUND, alert=True)
        else:
            await self._show_document_card(event, user_id, document_id)

    async def _cb_lib_forward(self, event, user_id: int, data: str) -> None:
        item_id = _to_int(data.rsplit(":", 1)[-1])
        if item_id is None:
            await self._ack(event, texts.ITEM_UNAVAILABLE, alert=True)
        else:
            await self._forward_library_item(event, user_id, item_id)

    async def _cb_lib_item(self, event, _user_id: int, data: str) -> None:
        item_id = _to_int(data.rsplit(":", 1)[-1])
        if item_id is None:
            await self._ack(event, texts.ITEM_UNAVAILABLE, alert=True)
        else:
            await self._show_library_item(event, item_id)

    async def _cb_lib_page(self, event, _user_id: int, data: str) -> None:
        page = _to_int(data.split(":", 1)[1])
        if page is None:
            await self._ack(event, texts.LIBRARY_INVALID_PAGE, alert=True)
        else:
            await self._show_library(event, page=max(0, page))

    async def _cb_wallet_page(self, event, _user_id: int, data: str) -> None:
        page = _to_int(data.split(":", 1)[1])
        if page is None:
            await self._ack(event, texts.INVALID_PAYMENTS_PAGE, alert=True)
        else:
            await self._show_wallet(event, payments_page=max(0, page))

    async def _cb_payment_action(self, event, _user_id: int, data: str) -> None:
        try:
            payment_id = int(data.split(":", 1)[1])
        except ValueError:
            await event.respond(texts.INVALID_REQUEST)
            return
        if data.startswith("payc:"):
            await self._cancel_payment(event, payment_id)
        elif data.startswith("payopen:"):
            await self._show_payment(event, payment_id, edit_in_place=True)
        else:
            await self._await_receipt(event, payment_id)

    async def _cb_admin_overview(self, event, user_id: int, data: str) -> None:
        parts = data.split(":")
        section = parts[1] if len(parts) > 1 else "ov"
        period = parts[2] if len(parts) > 2 else "7d"
        if period not in self.ADMIN_PERIOD_KEYS:
            await self._ack(event, texts.INVALID_PERIOD, alert=True)
            return
        if section == "ov":
            await self._show_admin_overview(event, period)
        elif section in {"ovu", "ovp", "ovpay", "ovs"}:
            aliases = {
                "ovu": "usr:stats",
                "ovp": "sys:proc",
                "ovpay": "pay:stats",
                "ovs": "sys:status",
            }
            await self._route_admin_hub(event, user_id, f"adm:hub:{aliases[section]}:{period}")
        else:
            await self._ack(event, texts.INVALID_REPORT, alert=True)

    async def _cb_admin_pay_page(self, event, _user_id: int, data: str) -> None:
        parts = data.split(":")
        parsed_page = _to_int(parts[2]) if len(parts) == 3 else None
        if parsed_page is None:
            await self._ack(event, texts.INVALID_ORDERS_PAGE, alert=True)
            return
        await self._show_admin_payments(event, max(0, parsed_page))

    async def _cb_admin_pay_history(self, event, _user_id: int, data: str) -> None:
        page = _to_int(data.rsplit(":", 1)[-1])
        await self._show_admin_payment_history(event, max(0, page or 0))

    async def _cb_admin_pay_detail(self, event, _user_id: int, data: str) -> None:
        detail_payment_id = _to_int(data.rsplit(":", 1)[-1])
        if detail_payment_id is None:
            await self._ack(event, texts.INVALID_ORDER_ID, alert=True)
            return
        await self._show_admin_payment_detail(event, detail_payment_id)

    async def _cb_admin_broadcast(self, event, user_id: int, _data: str) -> None:
        if self._broadcast_running:
            await self._ack(event, texts.BROADCAST_RUNNING, alert=True)
            return
        self._broadcast_previews.pop(user_id, None)
        self._broadcast_resume.pop(user_id, None)
        self._pending_broadcast.add(user_id)
        await event.respond(
            texts.BROADCAST_PROMPT,
            buttons=[[Button.inline(texts.BTN_CANCEL, b"adm:home")]],
        )

    async def _cb_broadcast_go(self, event, user_id: int, _data: str) -> None:
        preview = self._broadcast_previews.pop(user_id, None)
        if preview is None:
            await self._ack(event, texts.INVALID_REQUEST, alert=True)
            return
        await self._ack(event)
        await self._broadcast(event, preview[0], resume_id=preview[1])

    async def _cb_broadcast_stop(self, event, user_id: int, _data: str) -> None:
        if user_id in self._broadcast_running:
            self._broadcast_stop.set()
            await self._ack(event, texts.BROADCAST_STOPPED)
        else:
            await self._ack(event, texts.INVALID_REQUEST, alert=True)

    async def _cb_broadcast_cancel(self, event, user_id: int, _data: str) -> None:
        self._broadcast_previews.pop(user_id, None)
        self._broadcast_resume.pop(user_id, None)
        self._pending_broadcast.discard(user_id)
        await self._edit_or_reply(event, texts.BROADCAST_CANCELLED, self._admin_menu())

    async def _cb_broadcast_resume(self, event, user_id: int, data: str) -> None:
        # `or None` keeps the "not an integer" and "0" cases identical: neither
        # is a real run id, and passing None skips the query entirely.
        found = _to_int(data.rsplit(":", 1)[-1]) or None
        if found is None:
            await self._ack(event, texts.INVALID_REQUEST, alert=True)
            return
        run = await self.db.broadcast_get_run(found)
        if not run or run["admin_id"] != user_id or run["status"] != "stopped":
            await self._ack(event, texts.INVALID_REQUEST, alert=True)
            return
        if self._broadcast_running:
            await self._ack(event, texts.BROADCAST_RUNNING, alert=True)
            return
        self._broadcast_resume[user_id] = found
        self._pending_broadcast.add(user_id)
        await event.respond(texts.BROADCAST_RESUME_PROMPT)

    async def _cb_admin_audit_page(self, event, _user_id: int, data: str) -> None:
        page = _to_int(data.rsplit(":", 1)[-1])
        if page is None or page < 0:
            await self._ack(event, texts.INVALID_REPORT_PAGE, alert=True)
        else:
            await self._show_admin_audit(event, page)

    async def _cb_admin_gift_exec(self, event, user_id: int, data: str) -> None:
        parts = data.split(":", 3)
        target_id = _to_int(parts[2]) if len(parts) == 4 else None
        if target_id is None:
            await self._ack(event, texts.INVALID_GIFT, alert=True)
        else:
            await self._admin_gift_execute(event, user_id, target_id, parts[3])

    async def _cb_admin_gift(self, event, _user_id: int, data: str) -> None:
        parts = data.split(":")
        target_id = _to_int(parts[2]) if len(parts) in (3, 4) else None
        if target_id is None:
            await self._ack(event, texts.INVALID_GIFT, alert=True)
        elif len(parts) == 3:
            await self._admin_gift_pick(event, target_id)
        else:
            await self._admin_gift_confirm(event, target_id, parts[3])

    # ------------------------------------------------------------- callbacks
    async def _handle_callback(self, event) -> None:
        """Answer each callback once, after specific alerts have had priority."""
        try:
            await self._dispatch_callback(event)
        except Exception:
            await self._ack(event, texts.CALLBACK_FAILED, alert=True)
            raise
        finally:
            await self._ack(event)

    async def _dispatch_callback(self, event) -> None:
        if self._shutting_down:
            await self._ack(
                event, texts.RESTARTING, alert=True
            )
            return
        data = (event.data or b"").decode("utf-8", "replace").strip()
        user_id = event.sender_id
        if user_id is None:
            return
        if not getattr(event, "is_private", False):
            return
        if await self.db.is_banned(user_id):
            await self._ack(event, texts.ACCESS_BANNED, alert=True)
            return
        if not self._is_admin(user_id) and data.startswith(("adm:", "payv:")):
            await self._ack(event, texts.ADMIN_ONLY, alert=True)
            return
        await self.db.ensure_user(user_id)
        await self._capture_sender_name(event)
        if data.startswith(SUBSCRIPTION_PREFIX):
            await self.subscription.handle_callback(event, user_id, data)
            return

        self.conversations.on_callback_navigation(user_id, data)
        if data not in ("adm:bcgo", "adm:bcstop"):
            self._broadcast_previews.pop(user_id, None)
            self._broadcast_resume.pop(user_id, None)

        if not await self.callback_router.dispatch(event, user_id, data):
            await self._ack(event)

    # -------------------------------------------------------------- messages
    async def _handle_message(self, event) -> None:
        if event.message is None or not event.is_private:
            return
        if self._shutting_down:
            await event.reply(texts.RESTARTING)
            return
        user_id = event.sender_id
        if user_id is None:
            return
        self.conversations.sweep_support_states(self.deps.monotonic())
        sender = getattr(event, "sender", None)
        existed = await self.db.user_exists(user_id)
        user = await self.db.ensure_user(
            user_id,
            first_name=getattr(sender, "first_name", None) if sender else None,
            username=getattr(sender, "username", None) if sender else None,
            last_name=getattr(sender, "last_name", None) if sender else None,
        )
        if user.banned:
            await event.reply(texts.ACCESS_BANNED)
            return
        await self._capture_sender_name(event)
        text = (event.message.message or "").strip()
        command = _command_name(text)
        if command == "/start":
            self._broadcast_previews.pop(user_id, None)
            self._broadcast_resume.pop(user_id, None)
            self.conversations.reset_on_start(user_id)
            self._office_sessions.pop(user_id, None)
            if not existed:
                parts = text.split(maxsplit=1)
                payload = parts[1].strip() if len(parts) == 2 else ""
                if payload.lower().startswith("ref_"):
                    await self._apply_referral(event, user_id, payload[4:])
            await self._show_main(event, first_start=True)
            pending = self._curation.get(user_id)
            if pending and pending.get("delivery_failed") and not self._curation_stale(pending):
                prefix = self._curation_prefix(str(pending["kind"]))
                await event.respond(
                    texts.CURATION_DELIVERY_RETRY,
                    buttons=[[Button.inline(texts.CURATION_RESUME_BUTTON,
                                            f"{prefix}:cur:resume".encode("ascii"))]],
                )
            return

        # Reply-keyboard labels are navigation even while a text prompt is armed.
        # Clear the abandoned prompt after routing; keep an Office draft only when
        # the selected destination is Office itself.
        if not command and await self._route_reply_menu(event, user_id, text):
            self.conversations.reset_on_reply_navigation(
                user_id,
                preserve_office_session=(
                    text == texts.BTN_OFFICE and self.settings.office_enabled
                ),
            )
            self._broadcast_previews.pop(user_id, None)
            self._broadcast_resume.pop(user_id, None)
            return

        # Admin conversational states.
        if self._is_admin(user_id) and user_id in self._pending_reject_reason:
            payment_id = self._pending_reject_reason.pop(user_id, None)
            if payment_id is not None:
                if command == "/cancel":
                    await event.reply(texts.REJECT_CANCELLED)
                    await self._show_admin_payments(event)
                    return
                if not text:
                    self._pending_reject_reason[user_id] = payment_id
                    await event.reply(texts.REJECT_REASON_PROMPT)
                    return
                if text.startswith("/"):
                    self._pending_reject_reason[user_id] = payment_id
                    await event.reply(texts.REJECT_REASON_INVALID)
                    return
                await self._reject_payment_with_reason(event, payment_id, text)
                return
        if self._is_admin(user_id) and user_id in self._pending_broadcast:
            if command == "/cancel":
                self._broadcast_previews.pop(user_id, None)
                self._broadcast_resume.pop(user_id, None)
                self._pending_broadcast.discard(user_id)
                await event.reply(texts.BROADCAST_CANCELLED)
                return
            if not text:
                await event.reply(
                    texts.BROADCAST_NEEDS_TEXT
                )
                return
            if text.startswith("/"):
                await event.reply(texts.INVALID_COMMAND_TEXT)
                return
            await self._prepare_broadcast(event, user_id, text)
            return
        if self._is_admin(user_id) and user_id in self._support_admin_states and not command:
            ticket_id, _armed_at = self._support_admin_states[user_id]
            await self._handle_admin_support_text(event, user_id, ticket_id, text)
            return
        if self._is_admin(user_id) and command:
            self.conversations.disarm_admin_command_prompts(user_id)
        if (
            self._is_admin(user_id)
            and user_id in self._pending_user_search
            and not command
        ):
            self._pending_user_search.discard(user_id)
            await self._admin_user_search(event, user_id, text)
            return
        if self._is_admin(user_id) and command == "/grant":
            await self._admin_grant(event, text)
            return
        if self._is_admin(user_id) and command == "/unlimited":
            await self._admin_unlimited(event, text)
            return
        if self._is_admin(user_id) and command in {"/ban", "/unban"}:
            await self._admin_ban(event, text)
            return

        if user_id in self._support_states and not command:
            state = self._support_states[user_id]
            await self._handle_support_text(event, user_id, state, text)
            return
        if user_id in self._support_states and command:
            self._support_states.pop(user_id, None)

        if user_id in self._pending_payments and not command:
            await self._handle_receipt_upload(event, user_id)
            return

        if command == "/cancel":
            self._broadcast_previews.pop(user_id, None)
            self._broadcast_resume.pop(user_id, None)
            self.conversations.reset_on_cancel(user_id)
            self._office_sessions.pop(user_id, None)
            await event.reply(texts.ORDER_CANCELLED)
            return

        if user_id in self._pending_flashcard:
            if command:
                self._pending_flashcard.pop(user_id, None)
                self._pending_flashcard_front.pop(user_id, None)
            else:
                await self._handle_flashcard_text(event, user_id, text)
                return

        try:
            text_suffix, text_filename = _text_file_metadata(event.message)
        except OfficeError as exc:
            await event.reply(str(exc))
            return
        if text_suffix is not None:
            if not self.settings.office_enabled:
                await event.reply(texts.OFFICE_DISABLED)
                return
            if await self._is_duplicate_upload(event, user_id):
                return
            await self._accept_office_text_file(
                event, user_id, text_filename or f"source{text_suffix}", text_suffix
            )
            return
        office_session = (
            self._office_get_session(user_id) if self.settings.office_enabled else None
        )
        if office_session is not None and text and not command:
            await self._office_add_message(event, user_id, text)
            return
        if office_session is not None and _is_unsupported_attachment(event.message):
            await event.reply(
                texts.OFFICE_DRAFT_OPEN,
                buttons=self._office_session_keyboard(),
            )
            return

        dkind, dfilename = _document_metadata(event.message)
        if dkind is not None:
            if await self._is_duplicate_upload(event, user_id):
                return
            await self._accept_document(event, user_id, dkind, dfilename)
            return

        pkind, pfilename = _presentation_metadata(event.message)
        if pkind is not None:
            if await self._is_duplicate_upload(event, user_id):
                return
            await self._accept_presentation(event, user_id, pkind, pfilename)
            return

        kind, filename, duration = _media_metadata(event.message)
        if kind is not None:
            if await self._is_duplicate_upload(event, user_id):
                return
            await self._accept_media(event, user_id, kind, filename, duration)
            return

        if _is_receipt_image(event.message):
            open_payment = await self.db.open_awaiting_payment_for_user(user_id)
            if open_payment is not None:
                await self._handle_receipt_upload(
                    event, user_id, payment_id=int(open_payment["payment_id"])
                )
                return

        if self.settings.ocr_enabled:
            image_kind, image_name = _image_metadata(event.message)
            if image_kind is not None:
                if await self._is_duplicate_upload(event, user_id):
                    return
                await self._accept_document(event, user_id, image_kind, image_name)
                return

        if _is_unsupported_attachment(event.message):
            if getattr(event.message, "photo", None) is not None:
                await event.reply(DOC_PHOTO_HINT)
                return
            await event.reply(texts.UNSUPPORTED_FILE)
            return
        if text:
            await event.reply(
                texts.UPLOAD_HINT,
                buttons=self._user_menu(is_admin=self._is_admin(user_id)),
            )


__all__ = [
    "ACTIVE_LIMIT_MESSAGE",
    "AUDIO_EXTENSIONS",
    "BotDependencies",
    "CATEGORY_FILTER_LABELS",
    "CURATION_GUIDANCE",
    "CURATION_STALE_ALERT",
    "CallbackRouter",
    "ConversationStateStore",
    "DEFAULT_INTAKE_SUFFIX",
    "DOC_EXTRACTION_STOPPED",
    "DOC_GENERATE_CARDS",
    "DOC_GENERATE_QUESTIONS",
    "DOC_OCR_FAILED",
    "DOC_PHOTO_HINT",
    "DOC_UNSUPPORTED_HINT",
    "DiskSpaceError",
    "DownloadStalledError",
    "FAILURE_KIND_LABELS_FA",
    "FLASHCARD_ASK_BACK",
    "FLASHCARD_ASK_FRONT",
    "FLASHCARD_EMPTY",
    "FREE_TRY_BADGE",
    "FREE_TRY_RESTORED",
    "FREE_TRY_USED_NOTICE",
    "FailureClassificationRule",
    "FloodWaitError",
    "GamasBot",
    "JobProgress",
    "LEADERBOARD_EMPTY",
    "LEADERBOARD_FALLBACK_NAME",
    "LEARNING_BUSY_ALERT",
    "LEARNING_CREDIT_RESTORED",
    "LEARNING_DELIVERY_CTA",
    "LEARNING_GENERATION_FAILED",
    "LEARNING_GENERATION_RETRY",
    "LEARNING_LEGACY_NOTICE",
    "LEARNING_NO_CREDIT",
    "LEARNING_PROGRESS_EDIT_TIMEOUT_SECONDS",
    "LEARNING_REFUND_PENDING_NOTICE",
    "LEARNING_STALE_ALERT",
    "MAX_OFFICE_OUTPUT_BYTES",
    "MAX_OFFICE_SESSIONS",
    "MAX_RAW_PREVIEW_LINES",
    "MAX_RECEIPT_IMAGE_PIXELS",
    "OFFICE_UPLOAD_STOPPED",
    "QUIZ_COUNTS",
    "QUIZ_POINTS_PER_ANSWER",
    "QUIZ_START_GUIDANCE",
    "RAW_FALLBACK_MAX_PAGES",
    "RAW_PREVIEW_CHARS",
    "RECEIPT_IMAGE_EXTENSIONS",
    "SUPPORT_CATEGORY_BUTTONS",
    "SUPPORT_STATUS_LABELS",
    "TELEGRAM_CAPTION_LIMIT",
    "TELEGRAM_DOWNLOAD_CHUNK_BYTES",
    "USER_VISIBLE_ERRORS",
    "UploadTooLargeError",
    "VIDEO_EXTENSIONS",
    "VIEW_LABELS",
    "_command_name",
    "_document_metadata",
    "_download_media_limited",
    "_download_media_ranges",
    "_ensure_disk_headroom",
    "_image_metadata",
    "_is_receipt_image",
    "_is_safe_receipt_path",
    "_is_unsupported_attachment",
    "_max_mtproto_part_size_kb",
    "_media_metadata",
    "_message_file_size",
    "_normalize_receipt_image",
    "_parse_telegram_user_id",
    "_presentation_metadata",
    "_pwrite_all",
    "_reported_upload_bytes",
    "_safe_user_error_text",
    "_sanitize_log_value",
    "_text_file_metadata",
    "_to_int",
    "aiohttp",
    "asyncio",
    "classify_failure",
    "complete_json",
    "extract_audio_track",
    "extract_document",
    "is_open_payment",
    "is_pending_payment_review",
    "load_presentation",
    "needs_transcode",
    "os",
    "plan_download_ranges",
    "prepare_audio",
    "probe_media",
    "raw_parts",
    "register_failure_rule",
    "render_pages",
    "shutil",
    "structure_transcript",
    "time",
    "transcribe",
]
