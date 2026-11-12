"""Media & presentation processing pipelines, upload admission, and artifact delivery (SRP / DIP).

Encapsulates upload deduplication, submission admission, worker execution,
audio/video & PowerPoint pipelines, progressive booklet generation, raw
artifact delivery, and library indexing.  External collaborators are
invoked via :attr:`deps` (:class:`~gamas_bot.bot_deps.BotDependencies`).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
import logging
import math
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

from telethon.tl.custom import Message

from . import texts
from .billing import format_credit_duration, format_seconds_as_credits
from .bot_deps import deps_for
from .bot_ui import (
    ACTIVE_LIMIT_MESSAGE,
    TELEGRAM_CAPTION_LIMIT,
)
from .db import InsufficientBalanceError, TooManyActiveJobsError
from .docx_export import (
    DocumentMeta,
    build_notes_docx,
    notes_docx_filename,
    resolve_design,
    resolve_fonts,
)
from .notes_pdf import build_notes_pdf, notes_pdf_filename
from .fileio import AsyncFileReader, run_file_io, run_file_io_resource
from .intake import (
    USER_VISIBLE_ERRORS,
    _safe_user_error_text,
    _sanitize_log_value,
    classify_failure,
)
from .jobs import Job, QueueFullError
from .learning import bound_content_text
from .logging_config import log_job_id
from .media import MediaInfo, convert_to_pptx
from .metrics import METRICS
from .notes import (
    StructuredNotes,
    build_presentation_document,
    structure_presentation,
)
from .presentations import (
    PresentationError,
    align_narration_to_slides,
    slides_outline,
)
from .progress import JobProgress, to_persian_digits
from .stt import STTError, largest_configured_upload
from .telegram_text import render_pages
from .telegram_transfer import UploadTooLargeError, _reported_upload_bytes
from .titles import FALLBACK_BOOKLET, short_title
from .transcript import build_document

if TYPE_CHECKING:
    from .bot_deps import BotDependencies

logger = logging.getLogger("gamas_bot.bot.jobs")

class JobPipelineMixin:
    """Queued media/presentation processing and progressive Telegram delivery."""

    db: Any
    settings: Any
    client: Any
    queue: Any
    deps: BotDependencies
    _shutting_down: bool
    _admission_tasks: set[asyncio.Task[Any]]
    _active_upload_keys: dict[tuple[int, str], str]
    _seen_messages: dict[tuple[int, int], float]
    _media_semaphore: asyncio.Semaphore
    _protected_curation_items: Any
    _ack: Any

    async def _require_membership(
        self, event: Any, user_id: int, *, kind: str, callback_data: str = ""
    ) -> bool:
        """Provided by the composed bot; gates one value-delivery action."""
        raise NotImplementedError

    _download_semaphore: asyncio.Semaphore
    _upload_semaphore: asyncio.Semaphore

    async def _guard_upload_admission(
        self, event: Any, user_id: int, operation: Any
    ) -> None:
        """Own a file fingerprint from intake through queued/active completion."""
        if self._shutting_down:
            await event.reply("ربات در حال راه‌اندازی مجدد است؛ دوباره تلاش کنید.")
            return
        task = asyncio.current_task()
        if task is not None:
            self._admission_tasks.add(task)
        try:
            fp = self._upload_fingerprint(getattr(event, "message", None))
            if fp is None or self.settings.dedup_window_seconds <= 0:
                await operation(None)
                return
            key = (user_id, fp)
            if key in self._active_upload_keys:
                METRICS.record_deduplicated()
                await event.reply(
                    "⏳ این فایل هم‌اکنون در صف پردازش شما قرار دارد؛ لطفاً تا پایان پردازش منتظر بمانید."
                )
                return
            token = f"intake-{secrets.token_hex(8)}"
            self._active_upload_keys[key] = token
            try:
                await operation(token)
            finally:
                if self._active_upload_keys.get(key) == token:
                    self._active_upload_keys.pop(key, None)
        finally:
            if task is not None:
                self._admission_tasks.discard(task)

    def _track_queued_upload(self, job: Job, token: str | None) -> None:
        if token is None:
            return
        fp = self._upload_fingerprint(job.message)
        key = (job.user_id, fp) if fp is not None else None
        if key is not None and self._active_upload_keys.get(key) == token:
            self._active_upload_keys[key] = job.job_id

    async def _abandon_admission(self, submission_id: int) -> None:
        try:
            await self.db.set_submission_status(
                submission_id,
                "failed",
                error="interrupted during admission",
                error_kind="cancelled",
            )
        except Exception:
            logger.warning(
                "failed to close interrupted admission=%s", submission_id, exc_info=True
            )

    async def _create_admission_submission(
        self, user_id: int, kind: str, filename: str | None
    ) -> int:
        task = asyncio.create_task(
            self.db.create_submission(
                user_id,
                kind,
                filename,
                active_limit=self.settings.max_active_jobs_per_user,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                submission_id = await task
            except Exception:
                logger.debug("cancelled admission did not create a row", exc_info=True)
            else:
                await self._abandon_admission(submission_id)
            raise

    async def _queue_admitted_submission(
        self,
        event: Any,
        user_id: int,
        submission_id: int,
        *,
        kind: str,
        filename: str | None,
        duration: float | None,
        token: str | None,
        presentation_kind: str | None = None,
    ) -> None:
        progress = None
        enqueued = False
        try:
            progress = await self.deps.create_job_progress(
                event, submission_id=submission_id
            )
            meta: dict[str, Any] = {"progress": progress}
            if presentation_kind is not None:
                meta["presentation_kind"] = presentation_kind
            job = Job(
                submission_id=submission_id,
                user_id=user_id,
                kind=kind,
                filename=filename,
                message=event.message,
                is_admin=self._is_admin(user_id),  # type: ignore[attr-defined]
                duration_hint=duration,
                meta=meta,
            )
            try:
                self.queue.submit(job)
                enqueued = True
                self._track_queued_upload(job, token)
            except QueueFullError as exc:
                await self.db.set_submission_status(
                    submission_id, "rejected", error=str(exc), error_kind="capacity"
                )
                await progress.fail(str(submission_id))
                await event.reply(f"⏸ {exc}")
                return
            logger.info(
                "accepted %s submission=%s", _sanitize_log_value(kind), submission_id
            )
        except BaseException:
            if not enqueued:
                await self._abandon_admission(submission_id)
                if progress is not None:
                    await progress.aclose()
            raise

    async def _accept_media(
        self,
        event: Any,
        user_id: int,
        kind: str,
        filename: str | None,
        duration: float | None,
    ) -> None:
        if not await self._require_membership(event, user_id, kind="media"):
            return
        await self._guard_upload_admission(
            event,
            user_id,
            lambda token: self._accept_media_claimed(
                event, user_id, kind, filename, duration, token
            ),
        )

    async def _accept_media_claimed(
        self,
        event: Any,
        user_id: int,
        kind: str,
        filename: str | None,
        duration: float | None,
        token: str | None,
    ) -> None:
        reported_size = getattr(getattr(event.message, "file", None), "size", None)
        if (
            isinstance(reported_size, int)
            and reported_size > self.settings.max_file_size
        ):
            max_mb = to_persian_digits(self.settings.max_file_size // (1024 * 1024))
            await event.reply(
                f"❌ حجم فایل ارسالی از سقف مجاز ربات بیشتر است. حد مجاز: {max_mb} مگابایت"
            )
            return
        if (
            duration is not None
            and duration > self.settings.max_media_duration_seconds
        ):
            limit_minutes = int(self.settings.max_media_duration_seconds // 60)
            await event.reply(
                "❌ مدت فایل صوتی یا ویدیویی از سقف مجاز ربات بیشتر است "
                f"(حداکثر {to_persian_digits(limit_minutes)} دقیقه)."
            )
            return
        available = await self.db.available_seconds(user_id)
        needed = self._reserve_estimate(duration)
        unlimited = await self.db.is_unlimited(user_id)
        if not unlimited and available < needed:
            has_duration = duration is not None and duration > 0
            measured = (
                "مدت فایل: " + format_credit_duration(duration)
                if has_duration
                else "مدت فایل نامشخص است"
            )
            await event.reply(
                f"💳 اعتبار شما کافی نیست.\n"
                f"{measured} | لازم: {format_seconds_as_credits(needed)} "
                f"| شما: {format_seconds_as_credits(available)}\n"
                "از «🛒 خرید اشتراک» اعتبار بگیرید."
            )
            return
        try:
            submission_id = await self._create_admission_submission(
                user_id, kind, filename
            )
        except TooManyActiveJobsError as exc:
            await event.reply(
                ACTIVE_LIMIT_MESSAGE.format(limit=to_persian_digits(exc.limit))
            )
            return
        await self._queue_admitted_submission(
            event,
            user_id,
            submission_id,
            kind=kind,
            filename=filename,
            duration=duration,
            token=token,
        )

    async def _accept_presentation(
        self, event: Any, user_id: int, kind: str, filename: str | None
    ) -> None:
        if not await self._require_membership(
            event, user_id, kind="presentation"
        ):
            return
        await self._guard_upload_admission(
            event,
            user_id,
            lambda token: self._accept_presentation_claimed(
                event, user_id, kind, filename, token
            ),
        )

    async def _accept_presentation_claimed(
        self,
        event: Any,
        user_id: int,
        kind: str,
        filename: str | None,
        token: str | None,
    ) -> None:
        if kind == "unsupported":
            await event.reply(
                "⚠️ فرمت ODP/OTP پشتیبانی نمی‌شود؛ لطفاً ارائه را با PowerPoint "
                "به‌صورت pptx ذخیره و دوباره بفرستید."
            )
            return
        reported_size = getattr(getattr(event.message, "file", None), "size", None)
        if (
            isinstance(reported_size, int)
            and reported_size > self.settings.max_file_size
        ):
            max_mb = to_persian_digits(self.settings.max_file_size // (1024 * 1024))
            await event.reply(
                f"❌ حجم فایل ارسالی از سقف مجاز ربات بیشتر است. حد مجاز: {max_mb} مگابایت"
            )
            return
        if not await self.db.is_unlimited(user_id):
            available = await self.db.available_seconds(user_id)
            needed = self._reserve_estimate(None)
            if available < needed:
                await event.reply(
                    f"💳 اعتبار شما کافی نیست.\n"
                    f"لازم: {format_seconds_as_credits(needed)} "
                    f"| شما: {format_seconds_as_credits(available)}\n"
                    "از «🛒 خرید اشتراک» اعتبار بگیرید."
                )
                return
        try:
            submission_id = await self._create_admission_submission(
                user_id, "presentation", filename
            )
        except TooManyActiveJobsError as exc:
            await event.reply(
                ACTIVE_LIMIT_MESSAGE.format(limit=to_persian_digits(exc.limit))
            )
            return
        await self._queue_admitted_submission(
            event,
            user_id,
            submission_id,
            kind="presentation",
            filename=filename,
            duration=None,
            token=token,
            presentation_kind=kind,
        )

    def _prune_dedup_maps(self, now: float) -> None:
        expired = [key for key, exp in self._seen_messages.items() if exp <= now]
        for key in expired:
            self._seen_messages.pop(key, None)
        while len(self._seen_messages) > 2048:
            oldest = next(iter(self._seen_messages))
            self._seen_messages.pop(oldest, None)

    @staticmethod
    def _upload_fingerprint(message: object) -> str | None:
        if message is None:
            return None
        doc = getattr(message, "document", None)
        doc_id = getattr(doc, "id", None) if doc is not None else None
        if isinstance(doc_id, int) and doc_id > 0:
            return f"doc:{doc_id}"
        photo = getattr(message, "photo", None)
        photo_id = getattr(photo, "id", None) if photo is not None else None
        if isinstance(photo_id, int) and photo_id > 0:
            return f"photo:{photo_id}"
        return None

    async def _is_duplicate_upload(self, event: Any, user_id: int) -> bool:
        window = float(self.settings.dedup_window_seconds)
        if window <= 0:
            return False
        now = self.deps.monotonic()
        self._prune_dedup_maps(now)
        msg = getattr(event, "message", None)
        msg_id = getattr(msg, "id", None)
        chat_id = getattr(event, "chat_id", None) or user_id
        if isinstance(msg_id, int) and msg_id > 0:
            msg_key = (int(chat_id), msg_id)
            if self._seen_messages.get(msg_key, 0.0) > now:
                METRICS.record_deduplicated()
                logger.info("deduplicated repeated telegram update")
                return True
            self._seen_messages[msg_key] = now + window

        fp = self._upload_fingerprint(msg)
        if fp is not None and (user_id, fp) in self._active_upload_keys:
            METRICS.record_deduplicated()
            logger.info(
                "deduplicated concurrent identical file existing_job=%s",
                self._active_upload_keys[(user_id, fp)],
            )
            await event.reply(
                "⏳ این فایل هم‌اکنون در صف پردازش شما قرار دارد؛ لطفاً تا پایان پردازش منتظر بمانید."
            )
            return True
        return False

    def runtime_metrics(self) -> dict[str, Any]:
        snap = METRICS.snapshot()
        snap["queue_pending"] = self.queue.pending
        snap["queue_active"] = self.queue.active_count
        snap["in_flight_jobs"] = self.queue.active_snapshot()
        return snap

    @staticmethod
    def _reserve_estimate(duration: float | None) -> int:
        if duration is None or duration <= 0:
            return 600
        return int(math.ceil(duration))

    async def _close_cancelled_progress(self, job: Job) -> None:
        progress = job.meta.get("progress")
        if progress is None:
            return
        try:
            await asyncio.wait_for(progress.fail(str(job.submission_id)), timeout=2.0)
        except Exception:
            logger.warning(
                "could not close cancelled progress submission=%s", job.submission_id
            )
        finally:
            await progress.aclose()

    async def _run_job(self, job: Job) -> None:
        progress: JobProgress | None = job.meta.get("progress")
        log_token = log_job_id.set(job.job_id)
        fp = self._upload_fingerprint(job.message)
        key = (job.user_id, fp) if fp is not None else None
        if key is not None:
            self._active_upload_keys.setdefault(key, job.job_id)
        workdir: Path | None = None
        try:
            if await self.db.is_banned(job.user_id):
                job.transition_to("cancelled")
                await self._release(job, "user banned before processing")
                await self.db.set_submission_status(
                    job.submission_id,
                    "rejected",
                    error="user banned",
                    error_kind="banned",
                )
                if progress:
                    await progress.fail(str(job.submission_id))
                await self._reply(job, "⛔ درخواست صف‌شده به دلیل مسدودی حساب لغو شد.")
                return
            await self.db.set_submission_status(job.submission_id, "processing")
            await run_file_io(self.settings.temp_dir.mkdir, parents=True, exist_ok=True)
            workdir = Path(
                await run_file_io_resource(
                    tempfile.mkdtemp,
                    lambda path: shutil.rmtree(path, ignore_errors=True),
                    prefix=f"submission-{job.submission_id}-",
                    dir=self.settings.temp_dir,
                )
            )
            if job.kind == "presentation":
                await self._process_presentation(job, workdir, progress)
            else:
                await self._process_media(job, workdir, progress)
            job.transition_to("completed")
        except InsufficientBalanceError as exc:
            job.transition_to("failed")
            await self.db.set_submission_status(
                job.submission_id,
                "failed",
                error=f"insufficient balance: {exc}",
                error_kind="billing",
            )
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(
                job,
                (
                    f"💳 اعتبار شما کافی نیست.\n"
                    f"لازم: {format_seconds_as_credits(exc.required_seconds)} "
                    f"| شما: {format_seconds_as_credits(exc.available_seconds)}\n"
                    "از «🛒 خرید اشتراک» اعتبار بگیرید."
                ),
            )
        except USER_VISIBLE_ERRORS as exc:
            kind = classify_failure(job.state, exc)
            job.transition_to("failed")
            await self._release(job, "user-visible failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500], error_kind=kind
            )
            if progress:
                await progress.fail(str(job.submission_id))
            safe_msg = _safe_user_error_text(
                exc,
                "درخواست قابل پردازش نبود؛ لطفاً فایل را بررسی و دوباره ارسال کنید.",
            )
            await self._reply(job, f"❌ {safe_msg}")
        except STTError as exc:
            job.transition_to("failed")
            await self._release(job, "stt failure")
            await self.db.set_submission_status(
                job.submission_id, "failed", error=str(exc)[:500], error_kind="stt"
            )
            if progress:
                await progress.fail(str(job.submission_id))
            safe_msg = _safe_user_error_text(
                exc, "خطای موقت در سرویس تبدیل گفتار رخ داد؛ لطفاً دوباره تلاش کنید."
            )
            await self._reply(job, f"❌ تبدیل گفتار به متن انجام نشد: {safe_msg}")
        except asyncio.CancelledError:
            job.transition_to("cancelled")
            await self._release(job, "cancelled")
            await self.db.set_submission_status(
                job.submission_id, "failed", error="cancelled", error_kind="cancelled"
            )
            await self._close_cancelled_progress(job)
            raise
        except Exception as exc:
            kind = classify_failure(job.state, exc)
            job.transition_to("failed")
            await self._release(job, f"unexpected: {type(exc).__name__}")
            await self.db.set_submission_status(
                job.submission_id,
                "failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
                error_kind=kind,
            )
            logger.exception(
                "job failed job_id=%s submission=%s", job.job_id, job.submission_id
            )
            if progress:
                await progress.fail(str(job.submission_id))
            await self._reply(
                job, "❌ خطای داخلی رخ داد و پردازش تکمیل نشد. لطفاً دوباره تلاش کنید."
            )
        finally:
            if key is not None and self._active_upload_keys.get(key) == job.job_id:
                self._active_upload_keys.pop(key, None)
            try:
                if progress:
                    await progress.aclose()
            finally:
                try:
                    if workdir is not None:
                        await run_file_io(shutil.rmtree, workdir, True)
                except OSError:
                    logger.warning("could not remove job work directory")
                finally:
                    log_job_id.reset(log_token)

    async def _release(self, job: Job, reason: str) -> bool:
        return await self.db.release_hold(job.submission_id, reason)

    async def _reply(self, job: Job, text: str, *, buttons: Any = None) -> None:
        if job.message is None:
            return
        try:
            await job.message.reply(text, buttons=buttons)
        except Exception:
            logger.warning("could not reply to user submission=%s", job.submission_id)

    async def _process_media(
        self, job: Job, workdir: Path, progress: Any
    ) -> None:
        assert progress is not None
        await progress.update(10, "آماده‌سازی پردازش")
        suffix = (
            Path(job.filename).suffix.lower()
            if job.filename
            else (".mp4" if job.kind == "video" else ".ogg")
        )
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".media"
        source_path = workdir / f"source{suffix}"
        job.transition_to("downloading")
        await progress.update(20, "دریافت فایل از تلگرام")
        download_started = self.deps.monotonic()
        try:
            await self._download_telegram_media(
                job.message, source_path, self.settings.max_file_size
            )
        except asyncio.TimeoutError as exc:
            raise ValueError(
                "مهلت دریافت فایل از تلگرام به پایان رسید؛ لطفاً دوباره تلاش کنید."
            ) from exc
        except UploadTooLargeError as exc:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.") from exc
        source_size = (await run_file_io(source_path.stat)).st_size
        if source_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
        download_seconds = max(0.001, self.deps.monotonic() - download_started)
        egress_bytes_per_second = source_size / download_seconds

        job.transition_to("extracting")
        await progress.update(35, "بررسی و آماده‌سازی صدا")
        source_info = await self._run_media_operation(
            self.deps.probe_media, source_path, self.settings
        )
        if (
            source_info.duration is not None
            and source_info.duration > self.settings.max_media_duration_seconds
        ):
            limit_minutes = int(self.settings.max_media_duration_seconds // 60)
            raise ValueError(
                "مدت فایل صوتی یا ویدیویی از سقف مجاز ربات بیشتر است "
                f"(حداکثر {to_persian_digits(limit_minutes)} دقیقه)."
            )
        audio_path = await self._run_media_operation(
            self._prepare_audio,
            source_path,
            workdir,
            job.filename,
            job.kind,
            info=source_info,
            egress_bytes_per_second=egress_bytes_per_second,
        )
        audio_size = (await run_file_io(audio_path.stat)).st_size
        if audio_size > self.settings.max_file_size:
            raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")

        if source_info.duration is not None or audio_path == source_path:
            info = source_info
        else:
            info = await self._run_media_operation(
                self.deps.probe_media, audio_path, self.settings
            )
        duration = info.duration or job.duration_hint
        if not duration or duration <= 0:
            raise ValueError("مدت فایل مشخص نشد؛ امکان پردازش آن وجود ندارد.")
        if duration > self.settings.max_media_duration_seconds:
            limit_minutes = int(self.settings.max_media_duration_seconds // 60)
            raise ValueError(
                "مدت فایل صوتی یا ویدیویی از سقف مجاز ربات بیشتر است "
                f"(حداکثر {to_persian_digits(limit_minutes)} دقیقه)."
            )
        needed = self._reserve_estimate(duration)
        await self.db.reserve(job.user_id, job.submission_id, needed)

        job.transition_to("transcribing")
        await progress.update(50, "تبدیل گفتار به متن")
        try:
            raw = await self.deps.transcribe(
                audio_path, self.settings, session=self._http_session()  # type: ignore[attr-defined]
            )
        except BaseException:
            await self._release(job, "transcription failed")
            raise
        document = build_document(raw)

        job.transition_to("uploading")
        await progress.update(75, "ارسال متن گفتار")
        await self.db.set_submission_status(
            job.submission_id, "processing", duration_seconds=duration
        )
        reference = f"GMS-{job.submission_id}"
        meta = DocumentMeta(
            reference=reference,
            source_name=job.filename,
            created_at=datetime.now(),
        )
        # The transcript is persisted, not written to the scratch directory and
        # pushed: the results button rebuilds it from this row on demand (see
        # ``artifacts.transcript_artifact``, which re-applies the profile).
        turns_json, attempted, overlaps = self._speaker_metadata(document)
        await self.db.save_transcription(
            job.submission_id,
            engine=raw.engine,
            text=document.plain(),
            notes_json="",
            speaker_turns_json=turns_json,
            diarization_attempted=attempted,
            overlap_count=overlaps,
        )
        await self._settle(job, duration)
        if not self.settings.note_configured:
            await self.db.set_submission_status(job.submission_id, "done")
            await self._deliver_results(
                job,
                document,
                reference,
                has_booklet=False,
                has_keypoints=False,
                progress=progress,
            )
            return
        job.transition_to("generating_notes")
        await progress.update(85, "تولید جزوهٔ ساختارمند")
        notes = await self._generate_notes(
            document.llm_document(),
            job,
            progress,
            workdir=workdir,
            reference=reference,
            meta=meta,
        )
        if notes is not None:
            speakers, timeline = self._booklet_context(document)
            notes = replace(
                notes, speakers=speakers, timeline=timeline
            )

        await self.db.save_transcription(
            job.submission_id,
            engine=raw.engine,
            text=document.plain(),
            notes_json=notes.to_json() if notes else "",
            speaker_turns_json=turns_json,
            diarization_attempted=attempted,
            overlap_count=overlaps,
        )
        job.transition_to("uploading")
        await self._deliver_booklet(
            job, workdir, notes, document, reference, meta, progress
        )

    async def _prepare_audio(
        self,
        source_path: Path,
        workdir: Path,
        filename: str | None,
        kind: str,
        *,
        info: MediaInfo | None = None,
        egress_bytes_per_second: float | None = None,
    ) -> Path:
        deps = deps_for(self)
        if info is None:
            info = await deps.probe_media(source_path, self.settings)
        if not info.has_audio:
            raise ValueError("این فایل شاخهٔ صوتی ندارد؛ چیزی برای پیاده‌سازی نیست.")
        ceiling = largest_configured_upload(self.settings)
        budget = (
            self.settings.max_file_size
            if ceiling is None
            else min(self.settings.max_file_size, ceiling)
        )
        try:
            oversized = (await run_file_io(source_path.stat)).st_size > budget
        except OSError:
            oversized = False
        # Preprocessing is mandatory in Gamas 2.0: even a perfectly valid MP3
        # goes through the bounded PyAV chain, so the engine always hears the
        # same cleaned signal. ``STT_PREPROCESS=0`` restores the old fast path
        # for A/B comparisons. A stereo source is preserved only when that is
        # explicitly enabled for diarization.
        must_prepare = bool(
            kind == "video"
            or deps.needs_transcode(filename, info)
            or oversized
            or self.settings.stt_preprocess
        )
        if must_prepare:
            return await deps.extract_audio_track(
                source_path,
                workdir,
                self.settings,
                total_duration=info.duration,
                max_output_bytes=budget,
                egress_bytes_per_second=egress_bytes_per_second,
                channels=getattr(info, "channels", None),
            )
        return source_path

    async def _run_media_operation(
        self, operation: Any, *args: Any, **kwargs: Any
    ) -> Any:
        async with self._media_semaphore:
            return await operation(*args, **kwargs)

    async def _download_telegram_media(
        self, message: Any, target: Path, limit_bytes: int
    ) -> Path:
        target = Path(target)
        await run_file_io(target.parent.mkdir, parents=True, exist_ok=True)
        async with self._download_semaphore:
            reported_bytes = _reported_upload_bytes(message, limit_bytes)
            if reported_bytes > limit_bytes:
                raise UploadTooLargeError(f"file exceeds limit ({limit_bytes} bytes)")
            await run_file_io(
                self.deps.ensure_disk_headroom,
                target.parent,
                reported_bytes * 2,
                reserve_bytes=self.settings.temp_min_free_bytes,
            )
            return await asyncio.wait_for(
                self.deps.download_media_limited(
                    message,
                    target,
                    limit_bytes,
                    stall_seconds=self.settings.download_stall_seconds,
                    client=self.client,
                    connections=self.settings.telegram_download_connections,
                ),
                timeout=self.settings.download_timeout,
            )

    async def _settle(self, job: Job, duration: float | None) -> None:
        measured = int(math.ceil(duration)) if duration else 0
        await self.db.settle_hold(job.submission_id, measured)

    async def _process_presentation(
        self, job: Job, workdir: Path, progress: Any
    ) -> None:
        assert progress is not None
        await progress.update(10, "آماده‌سازی پردازش")
        suffix = Path(job.filename).suffix.lower() if job.filename else ".pptx"
        source_path = workdir / f"source{suffix}"
        job.transition_to("downloading")
        await progress.update(20, "دریافت فایل از تلگرام")
        try:
            await self._download_telegram_media(
                job.message, source_path, self.settings.max_file_size
            )
        except asyncio.TimeoutError as exc:
            raise ValueError(
                "مهلت دریافت فایل از تلگرام به پایان رسید؛ لطفاً دوباره تلاش کنید."
            ) from exc
        except UploadTooLargeError as exc:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.") from exc
        deck_size = (await run_file_io(source_path.stat)).st_size
        if deck_size > self.settings.max_file_size:
            raise ValueError("این فایل از سقف حجم ربات بزرگ‌تر است.")
        if deck_size > self.settings.presentation_max_temp_bytes:
            raise PresentationError("حجم فایل موقت ارائه از سقف تعیین‌شده بیشتر است.")

        job.transition_to("extracting")
        conversion_warnings: list[str] = []
        original_source_path = source_path
        if job.meta.get("presentation_kind") == "legacy":
            await progress.update(30, "تبدیل قالب قدیمی ارائه")
            source_path = await self._run_media_operation(
                convert_to_pptx,
                source_path,
                workdir,
                self.settings,
                warnings_out=conversion_warnings,
            )
            if (
                await run_file_io(source_path.stat)
            ).st_size > self.settings.max_file_size:
                raise PresentationError("ارائهٔ تبدیل‌شده از سقف حجم فایل بزرگ‌تر است.")

        await progress.update(40, "بررسی اسلایدها")
        content = await self._run_media_operation(
            self.deps.load_presentation, source_path, workdir, self.settings
        )
        outline = slides_outline(content.slides)
        await self._store_presentation_document(  # type: ignore[attr-defined]
            job, original_source_path, content
        )
        logger.info(
            "presentation read submission=%s slides=%d outline_chars=%d clips=%d",
            job.submission_id,
            len(content.slides),
            len(outline),
            len(content.clips),
        )
        skipped: list[str] = []
        if conversion_warnings:
            skipped.append(
                "در تبدیل قالب قدیمی این موارد از ارائه حذف شدند: "
                + "، ".join(dict.fromkeys(conversion_warnings))
            )
        prepared = await self._run_media_operation(
            self.deps.prepare_audio,
            content,
            workdir,
            self.settings,
            skipped_out=skipped,
        )
        if prepared is not None:
            logger.info(
                "presentation audio prepared submission=%s source_seconds=%.3f "
                "output_seconds=%.3f clips=%d merged=%s skipped=%d",
                job.submission_id,
                prepared.source_duration,
                prepared.output_duration,
                len(prepared.clips),
                prepared.merged,
                len(prepared.skipped),
            )
        if (
            job.meta.get("presentation_kind") == "legacy"
            and not content.clips
            and outline.strip()
            and not skipped
        ):
            skipped.append(
                "برخی رسانه‌های قالب قدیمی در تبدیل به pptx حذف شدند (MEDIA_ACTION_OMITTED)؛ "
                "برای حفظ صدا، فایل را با PowerPoint جدید به‌صورت pptx ذخیره و دوباره بفرستید."
            )

        raw_text_parts: list[str] = []
        if outline.strip():
            raw_text_parts.append("متن اسلایدها:\n\n" + outline.strip())
        document = None
        raw_engine: str | None = None
        duration: float | None = None
        narration_text = ""
        if prepared is not None:
            if (
                await run_file_io(prepared.path.stat)
            ).st_size > self.settings.max_file_size:
                raise ValueError("صدای آماده‌شده از سقف حجم ربات بزرگ‌تر است.")
            duration = prepared.source_duration
            needed = self._reserve_estimate(duration)
            await self.db.reserve(job.user_id, job.submission_id, needed)
            job.transition_to("transcribing")
            await progress.update(55, "تبدیل صدای ارائه به متن")
            try:
                raw = await self.deps.transcribe(
                    prepared.path, self.settings, session=self._http_session()  # type: ignore[attr-defined]
                )
            except BaseException:
                await self._release(job, "transcription failed")
                raise
            raw_engine = raw.engine
            alignment = align_narration_to_slides(raw, prepared, content.slides)
            narration_text = alignment.as_text()
            logger.info(
                "presentation narration aligned submission=%s aligned=%s slides=%d "
                "unassigned=%s",
                job.submission_id,
                alignment.aligned,
                len(alignment.slide_text),
                bool(alignment.unassigned_text),
            )
            raw_text_parts.append(
                "متن پیاده‌سازی‌شدهٔ صدای ارائه:\n\n" + narration_text
            )
            await self.db.set_submission_status(
                job.submission_id, "processing", duration_seconds=duration
            )
        elif not outline.strip():
            raise ValueError("در این ارائه نه متن اسلایدی پیدا شد و نه صدای ضبط‌شده.")

        reference = f"GMS-{job.submission_id}"
        meta = DocumentMeta(
            reference=reference, source_name=job.filename, created_at=datetime.now()
        )

        job.transition_to("uploading")
        await progress.update(70, "ارسال متن خام")
        raw_text = "\n\n\n".join(part for part in raw_text_parts if part.strip())
        if skipped:
            raw_text += (
                "\n\n---\nنکته: این موارد در پردازش نادیده گرفته شدند:\n"
                + "\n".join(f"• {reason}" for reason in skipped)
            )
        engine_label = raw_engine or ("slides" if prepared is None else "stt")
        await self.db.save_transcription(
            job.submission_id,
            engine=engine_label,
            text=raw_text,
            notes_json="",
        )

        settled_duration = prepared.source_duration if prepared is not None else 0
        await self._settle(job, settled_duration)
        if not self.settings.note_configured:
            await self.db.set_submission_status(job.submission_id, "done")
            await self._deliver_results(
                job,
                document,
                reference,
                has_booklet=False,
                has_keypoints=False,
                progress=progress,
            )
            return
        job.transition_to("generating_notes")
        await progress.update(85, "تولید جزوهٔ ساختارمند")
        transcript_text = narration_text
        presentation_text = build_presentation_document(outline, transcript_text)
        notes = await self._generate_presentation_notes(
            outline, transcript_text, job, progress, document=presentation_text
        )
        if notes is not None:
            speakers, timeline = self._booklet_context(document)
            notes = replace(
                notes, speakers=speakers, timeline=timeline
            )
        await self.db.save_transcription(
            job.submission_id,
            engine=engine_label,
            text=raw_text,
            notes_json=notes.to_json() if notes else "",
        )
        job.transition_to("uploading")
        await self._deliver_booklet(
            job,
            workdir,
            notes,
            document,
            reference,
            meta,
            progress,
            plain_title="جزوهٔ ارائه",
            plain_text=presentation_text,
        )

    async def _generate_notes(
        self,
        document_text: str,
        job: Job,
        progress: Any,
        *,
        workdir: Path | None = None,
        reference: str = "",
        meta: DocumentMeta | None = None,
    ) -> StructuredNotes | None:
        if not self.settings.note_configured:
            logger.info("note provider not configured; delivering raw only")
            return None
        # A single completion screen is the delivery contract.  In particular,
        # do not push an intermediate Word draft while a later editorial pass
        # may still change the same StructuredNotes model.
        del workdir, reference, meta
        try:
            return await self.deps.structure_transcript(
                document_text,
                self.settings,
                self._http_session(),  # type: ignore[attr-defined]
                mode=self.settings.note_mode,
                on_merged=None,
            )
        except Exception as exc:
            logger.warning("note generation failed; raw transcript remains available: %s", exc)
            await self._reply(
                job,
                "ℹ️ تولید جزوهٔ ساختارمند انجام نشد؛ متن خام از دکمهٔ خروجی‌ها در دسترس است.",
            )
            return None

    async def _generate_presentation_notes(
        self,
        outline: str,
        transcript_text: str,
        job: Job,
        progress: Any,
        *,
        document: str | None = None,
    ) -> StructuredNotes | None:
        if not self.settings.note_configured:
            return None
        if not outline.strip() and not transcript_text.strip():
            return None
        try:
            return await structure_presentation(
                outline,
                transcript_text,
                self.settings,
                self._http_session(),  # type: ignore[attr-defined]
                mode=self.settings.note_mode,
                document=document,
            )
        except Exception as exc:
            logger.warning("presentation note generation failed: %s", exc)
            await self._reply(
                job,
                "ℹ️ تولید جزوهٔ ساختارمند انجام نشد؛ فایل سادهٔ Word و متن خام ارسال می‌شود.",
            )
            return None

    async def _deliver_booklet(
        self,
        job: Job,
        workdir: Path,
        notes: StructuredNotes | None,
        document: Any,
        reference: str,
        meta: DocumentMeta,
        progress: Any,
        *,
        plain_title: str = "جزوهٔ کلاس",
        plain_text: str | None = None,
    ) -> None:
        """Persist the final booklet files, then show one results message.

        PDF and DOCX are rendered exactly once from the same validated
        ``StructuredNotes`` object.  Their bytes are kept in the database before
        the job work directory is removed, so every later button press merely
        sends a stored file and can never spend credits or invoke a renderer.
        ``plain_*`` remains in this signature for source compatibility with the
        presentation pipeline; an unstructured fallback is deliberately not
        advertised as a polished booklet.
        """
        del workdir, plain_title, plain_text  # legacy call compatibility only
        booklet_title = short_title(
            getattr(notes, "title", "") if notes is not None else "",
            fallback=FALLBACK_BOOKLET,
        )
        pdf_ready = False
        docx_ready = False
        notice = ""
        notice_kind = ""

        if notes is None or not notes.has_content:
            notice = "\n\nنکته: جزوهٔ ساختارمند برای این فایل ساخته نشد؛ متن خام در دسترس است."
            notice_kind = "notes"
        else:
            duration_seconds = (
                int(job.duration_hint)
                if job.duration_hint is not None and job.duration_hint > 0
                else None
            )
            pdf_bytes: bytes | None = None
            docx_bytes: bytes | None = None
            try:
                await progress.update(93, "آماده‌سازی جزوه PDF")
                pdf_bytes = await asyncio.to_thread(
                    build_notes_pdf,
                    notes,
                    meta=meta,
                    duration_seconds=duration_seconds,
                )
            except Exception:
                logger.exception("structured PDF generation failed submission=%s", job.submission_id)
                notice = "\n\nنکته: فایل PDF جزوه ساخته نشد؛ نسخه‌های دیگر در دسترس‌اند."
                notice_kind = "pdf"
            try:
                await progress.update(94, "آماده‌سازی فایل Word")
                docx_bytes = await asyncio.to_thread(
                    build_notes_docx,
                    notes,
                    fonts=resolve_fonts(),
                    design=resolve_design(),
                    meta=meta,
                )
            except Exception:
                logger.exception("structured DOCX generation failed submission=%s", job.submission_id)
                if not notice:
                    notice = "\n\nنکته: فایل Word جزوه ساخته نشد؛ نسخه‌های دیگر در دسترس‌اند."
                    notice_kind = "docx"

            if pdf_bytes is not None:
                try:
                    await self.db.binary_artifact_upsert(
                        job.submission_id,
                        job.user_id,
                        "booklet",
                        filename=notes_pdf_filename(notes, reference),
                        content_type="application/pdf",
                        content=pdf_bytes,
                    )
                    pdf_ready = True
                except Exception:
                    logger.exception("could not persist PDF booklet submission=%s", job.submission_id)
                    notice = "\n\nنکته: فایل PDF جزوه ذخیره نشد؛ لطفاً بعداً دوباره تلاش کنید."
                    notice_kind = "pdf_storage"
            if docx_bytes is not None:
                try:
                    await self.db.binary_artifact_upsert(
                        job.submission_id,
                        job.user_id,
                        "docx",
                        filename=notes_docx_filename(notes, reference),
                        content_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        content=docx_bytes,
                    )
                    docx_ready = True
                except Exception:
                    logger.exception("could not persist DOCX booklet submission=%s", job.submission_id)
                    if not notice:
                        notice = "\n\nنکته: فایل Word جزوه ذخیره نشد؛ لطفاً بعداً دوباره تلاش کنید."
                        notice_kind = "docx_storage"

            # The Library retains the editable source text and learning entry
            # points.  It is a virtual record (no automatic Telegram upload);
            # re-download routes use the persisted PDF rather than message ids.
            if pdf_ready or docx_ready:
                learning_source = document.llm_document() if document is not None else ""
                learning_text = bound_content_text(learning_source)
                try:
                    existing = await self.db.library_list_kind(
                        job.user_id, "booklet", limit=50
                    )
                    match = next(
                        (
                            row
                            for row in existing
                            if int(row.get("submission_id") or 0) == job.submission_id
                            and not row.get("unavailable")
                        ),
                        None,
                    )
                    if match is None:
                        item_id = await self.db.library_add(
                            job.user_id,
                            "booklet",
                            0,
                            0,
                            job.submission_id,
                            title=booklet_title,
                        )
                        if learning_text:
                            await self.db.library_set_content(item_id, learning_text)
                    else:
                        item_id = int(match["item_id"])
                    # The structured model the renderers used, stored next to
                    # the text cache so Gamas Studio can edit the real
                    # structure (the submission's notes_json stays the
                    # immutable original). Refreshed on every delivery so a
                    # re-processed submission never serves a stale structure.
                    await self.db.library_set_structured(item_id, notes.to_json())
                except Exception:
                    logger.warning(
                        "could not create virtual booklet library record submission=%s",
                        job.submission_id,
                        exc_info=True,
                    )

        await self.db.set_submission_status(
            job.submission_id,
            "partial" if notice else "done",
            error=notice.strip() or None,
            error_kind=notice_kind or None,
        )
        if notice:
            job.meta["outcome"] = "partial"
        await progress.complete()
        await self._deliver_results(
            job,
            document,
            reference,
            has_booklet=pdf_ready,
            has_docx=docx_ready,
            has_keypoints=bool(getattr(notes, "key_points", ()) or ()),
        )

    async def _send_telegram_file(
        self, entity: Any, path: Path, **kwargs: Any
    ) -> Message | None:
        path = Path(path)
        file_size = (await run_file_io(path.stat)).st_size
        async with self._upload_semaphore:
            stream = await AsyncFileReader.open(path)
            try:
                return await asyncio.wait_for(
                    self.client.send_file(
                        entity, stream, file_size=file_size, **kwargs
                    ),
                    timeout=self.settings.upload_timeout,
                )
            finally:
                await stream.aclose()

    async def _send_file(
        self, job: Job, path: Path, *, caption: str = ""
    ) -> Message | None:
        if job.message is None:
            raise RuntimeError("پیام کاربر برای ارسال فایل در دسترس نیست.")
        target = await job.message.get_input_chat()
        caption_arg = caption[:TELEGRAM_CAPTION_LIMIT] or None
        flood_err = deps_for(self).flood_wait_error
        for attempt in range(2):
            try:
                return await self._send_telegram_file(
                    target,
                    path,
                    caption=caption_arg,
                    force_document=True,
                    reply_to=job.message.id,
                )
            except flood_err as exc:
                wait_seconds = float(getattr(exc, "seconds", 0) or 0)
                if attempt == 0 and 0 < wait_seconds <= 15.0:
                    logger.warning(
                        "Telegram FloodWait %.1fs on send_file; retrying once",
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                raise
        return None

    @staticmethod
    def _speaker_metadata(document: Any) -> tuple[str | None, bool, int]:
        """Durable, anonymous speaker turns for a submission, when available.

        Only facts the engine actually produced are stored: a turn with unknown
        attribution keeps ``speaker_label=None``, and a recording without
        diarization yields no turns at all. No name or count is inferred here.
        """
        if not hasattr(document, "turns_to_json"):
            return None, False, 0
        try:
            turns = document.turns_to_json()
        except Exception:  # pragma: no cover - a malformed document is not fatal
            logger.warning("speaker turns could not be serialized", exc_info=True)
            return None, False, 0
        return (
            turns,
            bool(getattr(document, "diarization_attempted", False)),
            max(0, int(getattr(document, "overlap_count", 0) or 0)),
        )

    @staticmethod
    def _booklet_context(document: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Source-derived speaker list and chapter stamps for the booklet.

        Only real turns and real paragraph timings are used. An unknown
        attribution is skipped rather than given a plausible-sounding name,
        and a document without timings gets no invented timeline.
        """
        if not hasattr(document, "speaker_turns"):
            return (), ()
        try:
            turns = document.speaker_turns()
        except Exception:  # pragma: no cover - a malformed document is not fatal
            return (), ()
        words_by_speaker: dict[str, int] = {}
        for turn in turns:
            if not turn.display_name:
                continue
            key = str(turn.display_name).strip()
            words_by_speaker[key] = words_by_speaker.get(key, 0) + len(turn.text.split())
        speakers = tuple(
            f"{name} — {to_persian_digits(count)} واژه"
            for name, count in sorted(
                words_by_speaker.items(), key=lambda pair: (-pair[1], pair[0])
            )[:12]
        )
        timeline: list[str] = []
        seen_minutes: set[int] = set()
        for paragraph in getattr(document, "paragraphs", ()) or ():
            start = getattr(paragraph, "start", None)
            text = str(getattr(paragraph, "text", "") or "").strip()
            if start is None or not text:
                continue
            minute = int(float(start)) // 60
            if minute in seen_minutes:
                continue
            seen_minutes.add(minute)
            minutes, seconds = divmod(int(float(start)), 60)
            stamp = to_persian_digits(f"{minutes:02d}:{seconds:02d}")
            snippet = " ".join(text.split())[:70]
            timeline.append(f"[{stamp}] {snippet}")
            if len(timeline) >= 40:
                break
        return speakers, tuple(timeline)

    # ----------------------------------------------------- button-driven delivery
    async def _deliver_results(
        self,
        job: Job,
        document: Any,
        reference: str,
        *,
        has_booklet: bool,
        has_keypoints: bool,
        has_docx: bool = False,
        progress: Any | None = None,
    ) -> None:
        """End the job with one message and a button per artifact.

        This replaces the old two-push ending (transcript file, a chat preview,
        then the booklet). Settlement already happened by the time this runs, so
        every button below is free and may be pressed any number of times.
        """
        from .artifacts import (
            KIND_BOOKLET,
            KIND_DOCX,
            KIND_FLASHCARDS,
            KIND_KEYPOINTS,
            KIND_QUIZ,
            KIND_TRANSCRIPT,
            KIND_TXT,
            delivery_markup,
            reference_for,
        )

        speakers = int(getattr(document, "speakers", 0) or 0)
        kinds = {KIND_TRANSCRIPT, KIND_TXT}
        if has_keypoints:
            kinds.add(KIND_KEYPOINTS)
        if has_booklet:
            kinds.update({KIND_BOOKLET, KIND_QUIZ, KIND_FLASHCARDS})
        if has_docx:
            kinds.add(KIND_DOCX)

        # The library is one central submission with independent artifacts:
        # register exactly what exists, so the buttons and the results hub can
        # never advertise a download that was not produced.
        ref = reference_for(job.submission_id)
        # Migration 026's compact registry predates DOCX and has a CHECK
        # constraint for its original kinds. PDF/DOCX availability is recorded
        # in the additive binary-artifact table instead, so never attempt to
        # insert ``docx`` into the legacy registry.
        for kind in kinds - {KIND_DOCX}:
            try:
                await self.db.artifact_upsert(
                    job.submission_id, job.user_id, kind, reference=ref
                )
            except Exception:
                logger.warning(
                    "artifact registry write failed submission=%s kind=%s",
                    job.submission_id, kind, exc_info=True,
                )
        item_id: int | None = None
        if has_booklet:
            try:
                books = await self.db.library_list_kind(
                    job.user_id, "booklet", limit=50
                )
                match = next(
                    (
                        row
                        for row in books
                        if int(row.get("submission_id") or 0) == job.submission_id
                        and not row.get("unavailable")
                    ),
                    None,
                )
                if match is not None:
                    item_id = int(match["item_id"])
            except Exception:
                logger.warning("booklet lookup failed for delivery buttons", exc_info=True)
        buttons = delivery_markup(
            reference=ref,
            speakers=speakers,
            has_booklet=has_booklet,
            kinds=kinds,
            item_id=item_id,
            mini_app_url=self.settings.mini_app_url,
            learning_available=self.settings.note_configured,
        )
        body = texts.delivery_screen(
            reference=reference,
            speakers=speakers,
            has_booklet=has_booklet,
            has_keypoints=has_keypoints,
            learning_available=self.settings.note_configured,
        )
        try:
            await self._reply(job, body, buttons=buttons)
        except Exception:
            logger.warning(
                "results message could not be delivered submission=%s",
                job.submission_id,
                exc_info=True,
            )
        if progress is not None:
            await progress.complete(
                texts.DELIVERY_READY_HEADER.format(reference=reference)
            )

    async def _fetch_artifact(self, event: Any, user_id: int, data: str) -> None:
        """Send one owner-scoped artifact without regenerating a booklet.

        Transcript/key-point views are deterministic projections of their stored
        source rows. PDF and DOCX final bytes were saved at completion, so this
        method only retrieves and sends them.
        """
        from .artifacts import (
            KIND_BOOKLET,
            KIND_DOCX,
            KIND_TRANSCRIPT,
            KIND_TXT,
            keypoints_artifact,
            parse_callback,
            submission_from_callback,
            transcript_artifact,
        )

        parsed = parse_callback(data)
        submission_id = submission_from_callback(data)
        if parsed is None or submission_id is None:
            await self._ack(event)
            return
        submission = await self.db.submission(submission_id)
        if submission is None or int(submission.user_id) != int(user_id):
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return
        reference, kind = parsed
        if kind in {KIND_BOOKLET, KIND_DOCX}:
            if await self._fetch_persisted_booklet(
                event, user_id, submission_id, kind, reference
            ):
                return
            # Existing installations may contain pre-persistence Word library
            # rows. They remain readable as a legacy booklet, but no new file
            # is generated or mislabelled as a PDF.
            if kind == KIND_BOOKLET:
                await self._fetch_booklet(event, user_id, submission_id)
                return
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return

        chat = await event.get_input_chat()
        if kind == KIND_TXT:
            import io

            from telethon.tl.types import DocumentAttributeFilename

            body = await transcript_artifact(
                self.db, submission_id, self.settings.raw_text_profile
            )
            if not body:
                await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
                return
            filename = f"{reference}-transcript.txt"
            try:
                await self.client.send_file(
                    chat,
                    io.BytesIO(body.encode("utf-8")),
                    caption=texts.DELIVERY_TRANSCRIPT_READY.format(reference=reference),
                    force_document=True,
                    attributes=[DocumentAttributeFilename(filename)],
                )
            except Exception:
                logger.warning(
                    "txt artifact delivery failed submission=%s", submission_id,
                    exc_info=True,
                )
                await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
                return
            await self._ack(event)
            return
        if kind == KIND_TRANSCRIPT:
            body = await transcript_artifact(
                self.db, submission_id, self.settings.raw_text_profile
            )
            heading = texts.DELIVERY_TRANSCRIPT_READY.format(reference=reference)
        else:
            body = await keypoints_artifact(self.db, submission_id)
            heading = texts.DELIVERY_KEYPOINTS_READY.format(reference=reference)
        if not body:
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return
        body = f"{heading}\n\n{body}"
        try:
            for page in render_pages(body):
                if not page.strip():
                    continue
                await self.client.send_message(chat, page, parse_mode="html")
        except Exception:
            logger.warning(
                "could not deliver %s artifact submission=%s",
                kind,
                submission_id,
                exc_info=True,
            )
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return
        await self._ack(event)

    async def _fetch_persisted_booklet(
        self,
        event: Any,
        user_id: int,
        submission_id: int,
        kind: str,
        reference: str,
    ) -> bool:
        """Send one exact PDF/DOCX blob; return whether an artifact existed."""
        artifact = await self.db.binary_artifact(submission_id, user_id, kind)
        if artifact is None:
            return False
        import io

        from telethon.tl.types import DocumentAttributeFilename

        raw_content = artifact.get("content")
        content = bytes(raw_content) if raw_content is not None else b""
        filename = Path(str(artifact.get("filename") or "")).name
        if not content or not filename:
            logger.warning(
                "stored %s artifact is malformed submission=%s", kind, submission_id
            )
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return True
        label = "📘 جزوه PDF" if kind == "booklet" else "📄 نسخه Word"
        try:
            await self.client.send_file(
                await event.get_input_chat(),
                io.BytesIO(content),
                caption=f"{label} — {reference}",
                force_document=True,
                attributes=[DocumentAttributeFilename(filename)],
            )
        except Exception:
            logger.warning(
                "stored %s delivery failed submission=%s", kind, submission_id,
                exc_info=True,
            )
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return True
        await self._ack(event)
        return True

    async def _fetch_booklet(self, event: Any, user_id: int, submission_id: int) -> None:
        """Forward a legacy pre-persistence Word booklet for its owner only."""
        submission = await self.db.submission(submission_id)
        if submission is None or int(submission.user_id) != int(user_id):
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return
        rows = await self.db.library_list_kind(user_id, "booklet", limit=50)
        match = next(
            (
                row
                for row in rows
                if int(row.get("submission_id") or 0) == submission_id
                and int(row.get("chat_id") or 0)
                and int(row.get("message_id") or 0)
                and not row.get("unavailable")
            ),
            None,
        )
        if match is None:
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return
        try:
            await self.client.forward_messages(
                await event.get_input_chat(),
                [int(match["message_id"])],
                from_peer=int(match["chat_id"]),
            )
        except Exception:
            logger.warning(
                "could not forward legacy booklet submission=%s", submission_id,
                exc_info=True,
            )
            await self.db.library_mark_unavailable(int(match["item_id"]))
            await self._ack(event, texts.DELIVERY_ARTIFACT_GONE, alert=True)
            return
        await self._ack(event)

