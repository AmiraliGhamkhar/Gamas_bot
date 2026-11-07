"""Quizzes, flashcards, curation previews, leaderboards, and admin rewards (SRP / DIP).

Encapsulates parallel chunk generation, source-grounded item anchoring,
interactive curation previews, quiz sessions, flashcard study & DOCX export,
and leaderboard gift flows.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from datetime import datetime
import logging
import math
from pathlib import Path
import secrets
import sqlite3
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

from telethon import Button
from telethon.errors import FloodWaitError, MessageNotModifiedError

from . import texts
from .billing import format_duration
from .bot_ui import (
    CURATION_GUIDANCE,
    CURATION_STALE_ALERT,
    FLASHCARD_ASK_BACK,
    FLASHCARD_ASK_FRONT,
    FLASHCARD_EMPTY,
    FREE_TRY_BADGE,
    FREE_TRY_RESTORED,
    LEADERBOARD_EMPTY,
    LEADERBOARD_FALLBACK_NAME,
    LEARNING_BUSY_ALERT,
    LEARNING_CREDIT_RESTORED,
    LEARNING_GENERATION_FAILED,
    LEARNING_GENERATION_RETRY,
    LEARNING_LEGACY_NOTICE,
    LEARNING_NO_CREDIT,
    LEARNING_REFUND_PENDING_NOTICE,
    LEARNING_STALE_ALERT,
    QUIZ_COUNTS,
    QUIZ_POINTS_PER_ANSWER,
    QUIZ_START_GUIDANCE,
    TELEGRAM_CAPTION_LIMIT,
)
from .db import InsufficientBalanceError
from .db_learning import FlashcardLimitError
from .doc_extract import (
    DocChunk,
    chunk_text,
    chunks_from_json,
    preview_text,
)
from .docx_export import DocumentMeta, build_plain_docx, resolve_design, resolve_fonts
from .fileio import run_file_io, run_file_io_resource
from .intake import _to_int
from .learning import (
    FLASHCARD_SCHEMA,
    MAX_MANUAL_BACK_CHARS,
    MAX_MANUAL_FRONT_CHARS,
    MAX_MODEL_INPUT_CHARS,
    MIN_VALID_CARDS,
    MIN_VALID_QUESTIONS,
    OPTION_LABELS,
    QUIZ_SCHEMA,
    SIZE_PRESETS,
    GenerationShortfallError,
    anchor_items,
    dedupe_cards,
    dedupe_questions,
    distribute_counts,
    flashcard_system_prompt,
    parse_flashcard_payload,
    parse_quiz_payload,
    preset_cards,
    quiz_system_prompt,
)
from .llm import LLMContextOverflowError, LLMOutputBudgetError
from .metrics import METRICS
from .progress import (
    WORK_PROGRESS_HEADLINE,
    WorkProgress,
    completed_progress_text,
    failed_progress_text,
    progress_text,
    to_persian_digits,
)
from .telegram_text import button_label
from .titles import (
    FALLBACK_BOOKLET,
    FALLBACK_DOCUMENT,
    FALLBACK_OFFICE,
    short_title,
)

if TYPE_CHECKING:
    from .bot_deps import BotDependencies

logger = logging.getLogger("gamas_bot.bot.learning")


class LearningWorkflowMixin:
    """Learning generation, curation, quiz sessions, flashcards, and leaderboards."""

    db: Any
    settings: Any
    client: Any
    deps: BotDependencies
    _shutting_down: bool
    _learning_locks: MutableMapping[int, asyncio.Lock]

    async def _require_membership(
        self, event: Any, user_id: int, *, kind: str, callback_data: str = ""
    ) -> bool:
        """Provided by the composed bot; gates one value-delivery action."""
        raise NotImplementedError
    _learning_in_flight: set[int]
    _learning_tasks: dict[int, asyncio.Task[Any]]
    _pending_flashcard: dict[int, tuple[int, str]]
    _pending_flashcard_front: dict[int, str]
    _curation: dict[int, dict[str, Any]]
    _curation_tasks: dict[int, asyncio.Task[Any]]

    CURATION_TTL_SECONDS = 1800
    CURATION_PAGE_SIZE = 4
    CHUNK_PROGRESS_START = 6
    CHUNK_PROGRESS_END = 88

    def _learning_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._learning_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._learning_locks[user_id] = lock
        return lock

    def _learning_back_button(self, item_id: int) -> Any:
        return Button.inline("↩️ بازگشت", f"lib:it:{item_id}".encode("ascii"))

    async def _free_try_available(self, user_id: int, kind: str) -> bool:
        if await self.db.is_unlimited(user_id):
            return False
        return bool((await self.db.free_use_state(user_id)).get(kind, False))

    def _preset_cost(self, base_cost: int, cards: int, base_size: int) -> int:
        base_size = max(1, int(base_size))
        return max(1, int(round(int(base_cost) * int(cards) / base_size)))

    MAX_ITEMS_PER_CALL = 20

    def _learning_max_chars(self) -> int:
        context_tokens = int(getattr(self.settings, "note_context_tokens", 131_072) or 131_072)
        output_tokens = int(getattr(self.settings, "note_max_output_tokens", 16_384) or 16_384)
        reserved_output = min(output_tokens, context_tokens // 2)
        available_tokens = max(1_000, context_tokens - reserved_output - 2_000)
        return min(MAX_MODEL_INPUT_CHARS, max(2_000, int(available_tokens * 2.0)))

    def _learning_chunks(self, content: str) -> list[DocChunk]:
        return chunk_text(content, max_chars=self._learning_max_chars())

    async def _source_from_item(
        self, user_id: int, item_id: int
    ) -> dict[str, Any] | None:
        item = await self.db.library_item_detail(user_id, item_id)
        if (
            item is None
            or item.get("kind") != "booklet"
            or not item.get("content_text")
        ):
            return None
        return {
            "item_id": int(item["item_id"]),
            "doc_id": None,
            "title": short_title(
                self._item_display_title(item), fallback=FALLBACK_BOOKLET
            ),
            "chunks": self._learning_chunks(str(item["content_text"])),
        }

    async def _source_from_document(
        self, user_id: int, document_id: int
    ) -> dict[str, Any] | None:
        document = await self.db.document_get(user_id, document_id)
        if document is None or not document.get("text"):
            return None
        max_chars = self._learning_max_chars()
        chunks = await run_file_io(
            chunks_from_json,
            document.get("chunks_json"),
            max_chars=max_chars,
        )
        if not chunks:
            chunks = await run_file_io(
                chunk_text, str(document["text"]), max_chars=max_chars
            )
        item_id = int(document.get("library_item_id") or 0) or None
        if item_id is None:
            item_id = await self._document_library_item(user_id, document)  # type: ignore[attr-defined]
        return {
            "item_id": int(item_id),
            "doc_id": int(document["id"]),
            "title": short_title(
                str(document.get("title") or ""), fallback=FALLBACK_DOCUMENT
            ),
            "chunks": chunks,
        }

    async def _claim_generation_budget(
        self, event: Any, user_id: int, kind: str, cost: int, note: str
    ) -> tuple[bool, bool, str | None] | None:
        if await self.db.is_unlimited(user_id):
            return (False, False, None)
        claim_id = secrets.token_hex(16)
        if await self.db.consume_free_use(
            user_id, kind, claim_id=claim_id, require_limited=True
        ):
            return (True, False, claim_id)
        try:
            charged = await self.db.spend_seconds(
                user_id, cost, note, operation_id=claim_id
            )
        except InsufficientBalanceError:
            await event.respond(
                LEARNING_NO_CREDIT,
                buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
            )
            return None
        return (False, bool(charged), claim_id if charged else None)

    async def _refund_generation(
        self,
        user_id: int,
        *,
        cost: int,
        charged: bool,
        free: bool,
        kind: str,
        note: str,
        claim_id: str | None = None,
    ) -> bool:
        refunded = True
        if charged:
            try:
                if not claim_id:
                    raise ValueError("paid learning charge has no operation id")
                await self.db.refund_learning_charge(user_id, claim_id)
            except Exception:
                logger.exception(
                    "learning refund failed user=%s cost=%s", user_id, cost
                )
                refunded = False
        if free:
            try:
                await self.db.refund_free_use(user_id, kind, claim_id=claim_id)
            except Exception:
                logger.exception("free-use rollback failed user=%s", user_id)
                refunded = False
        return refunded

    async def _progress_edit(
        self,
        placeholder: Any,
        done: float,
        total: int,
        *,
        stage: str,
        headline: str = WORK_PROGRESS_HEADLINE,
    ) -> None:
        done = max(0.0, float(done))
        total = max(1, int(total))
        percent = self.CHUNK_PROGRESS_START + round(
            (self.CHUNK_PROGRESS_END - self.CHUNK_PROGRESS_START) * done / total
        )
        detail = f"بخش {to_persian_digits(int(done))} از {to_persian_digits(total)}"
        try:
            if callable(getattr(placeholder, "update", None)):
                await placeholder.update(percent, stage, detail)
                return
            await placeholder.edit(
                progress_text(percent, stage, detail, headline=headline)
            )
        except Exception:
            logger.debug("generation progress edit failed", exc_info=True)

    async def _generate_chunks(
        self,
        *,
        kind: str,
        chunks: list[DocChunk],
        counts: list[int],
        placeholder: Any,
        stage: str = "در حال تولید محتوا",
    ) -> tuple[list[dict[str, Any]], int]:
        schema = FLASHCARD_SCHEMA if kind == "deck" else QUIZ_SCHEMA
        concurrency = min(8, max(1, int(self.settings.learning_chunk_concurrency)))
        semaphore = asyncio.Semaphore(concurrency)
        active = [index for index, count in enumerate(counts) if count > 0]
        total = len(active)
        state = {"done": 0, "failed": 0}
        fractions = {index: 0.0 for index in active}
        progress_changed = asyncio.Event()
        finished = False
        session = self._http_session()  # type: ignore[attr-defined]
        edit_timeout = self.deps.learning_progress_edit_timeout_seconds

        async def report_progress() -> None:
            while True:
                # Clear before rendering so completions during an edit are not lost.
                progress_changed.clear()
                done = state["done"] + sum(fractions.values())
                try:
                    await asyncio.wait_for(
                        self._progress_edit(placeholder, done, total, stage=stage),
                        timeout=edit_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.debug("learning progress edit timed out")
                except Exception:
                    logger.debug("learning progress edit failed", exc_info=True)
                if finished and not progress_changed.is_set():
                    return
                await progress_changed.wait()

        max_input_chars = self._learning_max_chars()
        dedupe = dedupe_cards if kind == "deck" else dedupe_questions
        stem_key = "front" if kind == "deck" else "question"

        async def _generate_piece(
            target_chunk: DocChunk,
            target_count: int,
            *,
            chunk_idx: int,
            split_depth: int = 0,
        ) -> list[dict[str, Any]]:
            allowed = tuple(
                dict.fromkeys(
                    " ".join(str(locator).split())[:120]
                    for locator in (target_chunk.sources or (target_chunk.locator,))
                    if locator
                )
            )
            fallback_locator = allowed[0] if allowed else ""
            items: list[dict[str, Any]] = []
            max_attempts = max(2, math.ceil(int(target_count) / self.MAX_ITEMS_PER_CALL) + 1)
            use_schema = True
            had_shortfall = False

            for attempt in range(max_attempts):
                missing = int(target_count) - len(items)
                if missing <= 0:
                    break
                batch = min(missing, self.MAX_ITEMS_PER_CALL)
                system = (
                    flashcard_system_prompt(batch, sources=allowed)
                    if kind == "deck"
                    else quiz_system_prompt(batch, sources=allowed)
                )
                if had_shortfall:
                    system += (
                        "\nخروجی قبلی نامعتبر، ناقص یا بدون منبع مجاز بود؛ "
                        "همهٔ فیلدهای لازم و یک منبع دقیق از فهرست را وارد کن."
                    )
                if items:
                    prior_stems = [
                        str(it.get(stem_key) or "").strip()[:90]
                        for it in items[-25:]
                        if str(it.get(stem_key) or "").strip()
                    ]
                    if prior_stems:
                        system += (
                            "\nموردهای زیر قبلاً ساخته شده‌اند؛ هیچ‌کدام را تکرار یا بازنویسی نکن:\n- "
                            + "\n- ".join(prior_stems)
                        )
                try:
                    async with semaphore:
                        payload = await self.deps.complete_json(
                            session,
                            self.settings,
                            system=system,
                            user=target_chunk.text[:max_input_chars],
                            schema=schema if use_schema else None,
                        )
                    if kind == "deck":
                        parsed = parse_flashcard_payload(payload, batch)
                    else:
                        parsed = parse_quiz_payload(
                            payload, batch, require_difficulty=False
                        )
                    anchored = anchor_items(
                        parsed, allowed, fallback_locator, target_chunk.text
                    )
                    # Anchoring assigns a fallback locator; it never drops items.
                    prev_count = len(items)
                    merged = dedupe([*items, *anchored])
                    if len(merged) > prev_count:
                        items = merged[: int(target_count)]
                    if len(items) >= target_count:
                        break
                    if len(items) - prev_count < batch:
                        had_shortfall = True
                        if int(target_count) <= self.MAX_ITEMS_PER_CALL:
                            use_schema = False
                except asyncio.CancelledError:
                    raise
                except (LLMContextOverflowError, LLMOutputBudgetError):
                    if split_depth < 2 and len(target_chunk.text) >= 1_600:
                        sub_raw = chunk_text(
                            target_chunk.text,
                            max_chars=max(800, len(target_chunk.text) // 2),
                            label_prefix=target_chunk.locator or fallback_locator,
                        )
                        if len(sub_raw) >= 2:
                            sub_chunks = [
                                DocChunk(
                                    idx,
                                    sc.locator or fallback_locator,
                                    sc.sources or allowed,
                                    sc.text,
                                )
                                for idx, sc in enumerate(sub_raw)
                            ]
                            sub_counts = distribute_counts(
                                missing, [len(sc.text) for sc in sub_chunks]
                            )
                            for sc, sc_count in zip(sub_chunks, sub_counts, strict=True):
                                if sc_count <= 0:
                                    continue
                                sub_items = await _generate_piece(
                                    sc,
                                    sc_count,
                                    chunk_idx=chunk_idx,
                                    split_depth=split_depth + 1,
                                )
                                items = dedupe([*items, *sub_items])[: int(target_count)]
                            break
                    had_shortfall = True
                    use_schema = False
                    logger.warning(
                        "chunk budget exceeded kind=%s chunk=%s attempt=%s",
                        kind,
                        chunk_idx,
                        attempt + 1,
                        exc_info=True,
                    )
                except Exception:
                    had_shortfall = True
                    use_schema = False
                    logger.warning(
                        "chunk generation failed kind=%s chunk=%s attempt=%s",
                        kind,
                        chunk_idx,
                        attempt + 1,
                        exc_info=True,
                    )
                finally:
                    # Report attempt completion even when the chunk needs a retry.
                    fractions[chunk_idx] = max(
                        fractions[chunk_idx], min(0.9, (attempt + 1) / (max_attempts + 1))
                    )
                    progress_changed.set()
            return items

        async def one(index: int) -> list[dict[str, Any]]:
            chunk = chunks[index]
            count = counts[index]
            items = await _generate_piece(chunk, count, chunk_idx=index)
            if len(items) < count:
                state["failed"] += 1
            fractions[index] = 0.0
            state["done"] += 1
            progress_changed.set()
            return items

        # One reporter owns the placeholder throughout the whole generation.
        progress_task = asyncio.create_task(report_progress())
        tasks = [asyncio.create_task(one(index)) for index in active]
        try:
            results = await asyncio.gather(*tasks)
            finished = True
            progress_changed.set()
            try:
                await asyncio.wait_for(progress_task, timeout=edit_timeout * 2)
            except asyncio.TimeoutError:
                logger.debug("final learning progress edit timed out")
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            finished = True
            progress_changed.set()
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
            raise
        return [item for group in results for item in group], state["failed"]

    async def _generate_learning(
        self,
        event: Any,
        *,
        user_id: int,
        kind: str,
        source: dict[str, Any],
        wanted: int,
        cost: int,
        note: str,
        title: str,
    ) -> None:
        existing = self._curation.get(user_id)
        if existing is not None:
            if self._curation_stale(existing):
                await self._release_curation(user_id, existing)
            else:
                prefix = self._curation_prefix(str(existing["kind"]))
                await event.respond(
                    "یک پیش‌نمایش در انتظار بررسی دارید؛ ابتدا آن را ذخیره یا لغو کنید.",
                    buttons=[
                        [
                            Button.inline(
                                "🧐 ادامهٔ بررسی",
                                f"{prefix}:cur:resume".encode("ascii"),
                            )
                        ]
                    ],
                )
                return
        chunks = [chunk for chunk in source["chunks"] if chunk.text.strip()]
        if not chunks:
            await event.respond("متنی برای ساخت محتوا پیدا نشد. فایل دیگری بفرستید.")
            return
        await self._ack(event)  # type: ignore[attr-defined]
        budget_task = asyncio.create_task(
            self._claim_generation_budget(event, user_id, kind, cost, note)
        )
        try:
            budget = await asyncio.shield(budget_task)
        except asyncio.CancelledError:
            try:
                budget = await budget_task
                if budget is not None:
                    free, charged, claim_id = budget
                    await self._refund_generation(
                        user_id,
                        cost=cost,
                        charged=charged,
                        free=free,
                        kind=kind,
                        note=note,
                        claim_id=claim_id,
                    )
            except Exception:
                logger.exception(
                    "could not compensate cancelled admission user=%s", user_id
                )
            raise
        if budget is None:
            return
        free, charged, claim_id = budget
        placeholder = None
        state = None
        label = "فلش‌کارت" if kind == "deck" else "سؤال"
        stage = f"آماده‌سازی {label}"
        try:
            wanted = max(1, int(wanted))
            counts = distribute_counts(wanted, [len(chunk.text) for chunk in chunks])
            progress = await WorkProgress.start(
                event,
                headline="🧠 در حال ساخت محتوا",
                done_headline="✅ محتوا آماده شد",
                stage=stage,
                detail=(
                    "درخواست شما ثبت شد؛ "
                    f"{to_persian_digits(wanted)} مورد در "
                    f"{to_persian_digits(len(chunks))} بخش ساخته می‌شود."
                ),
            )
            placeholder = progress
            items, failures = await self._generate_chunks(
                kind=kind,
                chunks=chunks,
                counts=counts,
                placeholder=progress,
                stage=f"در حال تولید {label}",
            )
            items = dedupe_cards(items) if kind == "deck" else dedupe_questions(items)
            await progress.update(
                94,
                "اعتبارسنجی و آماده‌سازی پیش‌نمایش",
                f"{to_persian_digits(len(items))} مورد معتبر آماده شد.",
            )
            minimum = MIN_VALID_CARDS if kind == "deck" else MIN_VALID_QUESTIONS
            if len(items) < minimum:
                raise GenerationShortfallError(
                    kind=kind,
                    produced=len(items),
                    required=minimum,
                    requested=wanted,
                    failures=failures,
                    chunks=len(chunks),
                )
            state = {
                "kind": kind,
                "items": items[:wanted],
                "page": 0,
                "title": title[:200],
                "item_id": int(source["item_id"]),
                "doc_id": source.get("doc_id"),
                "cost": cost,
                "free": free,
                "charged": charged,
                "claim_id": claim_id,
                "note": note,
                "failures": failures,
                "skipped": sum(count == 0 for count in counts),
                "requested": wanted,
                "created_at": self.deps.monotonic(),
            }
            self._curation[user_id] = state
            delivered = await self._render_curation(progress.message, user_id)
            self._schedule_curation_expiry(user_id, state)
            if not delivered:
                state["delivery_failed"] = True
                try:
                    await event.respond(
                        texts.CURATION_DELIVERY_RETRY,
                        buttons=[[Button.inline(
                            texts.CURATION_RESUME_BUTTON,
                            f"{self._curation_prefix(kind)}:cur:resume".encode("ascii"),
                        )]],
                    )
                except Exception:
                    METRICS.record_curation_delivery_error()
                    logger.warning("could not send curation retry notice user=%s", user_id)
        except BaseException as exc:
            if state is not None and self._curation.get(user_id) is state:
                self._curation.pop(user_id, None)
            refund_ok = await self._refund_generation(
                user_id,
                cost=cost,
                charged=charged,
                free=free,
                kind=kind,
                note=note,
                claim_id=claim_id,
            )
            if placeholder is not None:
                try:
                    notice = (
                        LEARNING_REFUND_PENDING_NOTICE
                        if not refund_ok
                        else LEARNING_CREDIT_RESTORED
                        if charged
                        else FREE_TRY_RESTORED
                        if free
                        else ""
                    )
                    detail = self._generation_failure_detail(exc)
                    await placeholder.fail(
                        detail + (f"\n{notice}" if notice else ""),
                        headline="❌ ساخت محتوا ناموفق بود.",
                    )
                except Exception:
                    logger.warning(
                        "could not edit generation placeholder", exc_info=True
                    )
            if isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            logger.warning("%s generation failed", kind, exc_info=exc)

    @staticmethod
    def _generation_failure_detail(exc: BaseException) -> str:
        if not isinstance(exc, GenerationShortfallError):
            return LEARNING_GENERATION_RETRY
        if exc.produced <= 0 and exc.failures >= max(1, exc.chunks):
            reason = "سرویس هوش مصنوعی پاسخ معتبری نداد."
        elif exc.produced <= 0:
            reason = "از این بخش‌ها هیچ مورد معتبری ساخته نشد."
        else:
            reason = (
                f"فقط {to_persian_digits(exc.produced)} مورد معتبر از "
                f"{to_persian_digits(exc.requested)} مورد ساخته شد؛ حداقل "
                f"{to_persian_digits(exc.required)} مورد لازم است. یک اندازهٔ "
                "بزرگ‌تر یا جزوهٔ کامل‌تری را امتحان کنید."
            )
        return f"{reason}\n{LEARNING_GENERATION_RETRY}"

    def _curation_prefix(self, kind: str) -> str:
        return "fc" if kind == "deck" else "quiz"

    def _curation_stale(self, state: dict[str, Any] | None) -> bool:
        if not state:
            return True
        age = self.deps.monotonic() - float(state.get("created_at") or 0)
        return age > self.CURATION_TTL_SECONDS

    async def _refund_curation_state(
        self, user_id: int, state: dict[str, Any]
    ) -> bool:
        return await self._refund_generation(
            user_id,
            cost=int(state.get("cost") or 0),
            charged=bool(state.get("charged")),
            free=bool(state.get("free")),
            kind=str(state.get("kind") or "deck"),
            note=str(state.get("note") or ""),
            claim_id=state.get("claim_id"),
        )

    async def _release_curation(
        self, user_id: int, state: dict[str, Any]
    ) -> bool:
        if self._curation.get(user_id) is not state:
            return False
        self._curation.pop(user_id, None)
        timer = self._curation_tasks.pop(user_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        state["refund_ok"] = await self._refund_curation_state(user_id, state)
        return True

    def _schedule_curation_expiry(
        self, user_id: int, state: dict[str, Any]
    ) -> None:
        async def expire() -> None:
            try:
                await asyncio.sleep(self.CURATION_TTL_SECONDS)
                async with self._learning_lock(user_id):
                    await self._release_curation(user_id, state)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("could not expire curation user=%s", user_id)

        self._curation_tasks[user_id] = asyncio.create_task(expire())

    def _curation_keyboard(self, state: dict[str, Any], page: int) -> list[list]:
        prefix = self._curation_prefix(str(state["kind"]))
        items = state["items"]
        size = self.CURATION_PAGE_SIZE
        start = page * size
        buttons: list[list] = []
        row: list = []
        for offset in range(start, min(start + size, len(items))):
            row.append(
                Button.inline(
                    f"🗑 {to_persian_digits(offset + 1)}",
                    f"{prefix}:cur:del:{offset}".encode("ascii"),
                )
            )
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        pages = max(1, (len(items) + size - 1) // size)
        nav: list = []
        if page > 0:
            nav.append(
                Button.inline("◀️", f"{prefix}:cur:p:{page - 1}".encode("ascii"))
            )
        if page + 1 < pages:
            nav.append(
                Button.inline("▶️", f"{prefix}:cur:p:{page + 1}".encode("ascii"))
            )
        if nav:
            buttons.append(nav)
        buttons.append(
            [
                Button.inline("✅ ذخیره", f"{prefix}:cur:save".encode("ascii")),
                Button.inline("❌ انصراف", f"{prefix}:cur:cancel".encode("ascii")),
            ]
        )
        return buttons

    async def _render_curation(self, message: Any, user_id: int) -> bool:
        state = self._curation.get(user_id)
        if state is None:
            return False
        items = state["items"]
        kind = str(state["kind"])
        label = "فلش‌کارت" if kind == "deck" else "سؤال"
        pages = max(
            1, (len(items) + self.CURATION_PAGE_SIZE - 1) // self.CURATION_PAGE_SIZE
        )
        page = min(int(state.get("page") or 0), pages - 1)
        state["page"] = page
        start = page * self.CURATION_PAGE_SIZE
        lines = [
            f"🧐 پیش‌نمایش {label} — {to_persian_digits(len(items))} مورد "
            f"(صفحهٔ {to_persian_digits(page + 1)} از {to_persian_digits(pages)})",
            f"📄 منبع: {state['title']}",
        ]
        if state.get("failures"):
            lines.append(
                f"⚠️ {to_persian_digits(int(state['failures']))} بخش به‌طور کامل پردازش نشد."
            )
        if int(state.get("requested") or len(items)) > len(items):
            lines.append(
                f"ℹ️ از {to_persian_digits(int(state['requested']))} مورد درخواست‌شده، "
                f"{to_persian_digits(len(items))} مورد معتبر و غیرتکراری آماده شد."
            )
        if state.get("skipped"):
            lines.append(
                f"ℹ️ {to_persian_digits(int(state['skipped']))} بخش به‌دلیل اندازهٔ "
                "انتخاب‌شده بررسی نشد؛ برای پوشش بیشتر اندازهٔ بزرگ‌تر را انتخاب کنید."
            )
        lines.append(CURATION_GUIDANCE)
        for offset, item in enumerate(
            items[start : start + self.CURATION_PAGE_SIZE], start=start
        ):
            number = to_persian_digits(offset + 1)
            source = item.get("source") or "—"
            if kind == "deck":
                lines.append(
                    f"\n{number}) {preview_text(item['front'], 150)}\n"
                    f"   ← {preview_text(item['back'], 240)}\n   📍 {source}"
                )
            else:
                options = item["options"]
                option_lines = " | ".join(
                    f"{OPTION_LABELS[i]}) {preview_text(option, 55)}"
                    for i, option in enumerate(options)
                )
                correct = OPTION_LABELS[int(item["correct_index"])]
                difficulty = "سخت" if item.get("difficulty") == "hard" else "متوسط"
                lines.append(
                    f"\n{number}) {preview_text(item['question'], 180)}\n"
                    f"   {option_lines}\n   پاسخ: {correct} | دشواری: {difficulty}\n"
                    f"   {preview_text(item['explanation'], 100)}\n   📍 {source}"
                )
        rendered_text = "\n".join(lines)
        keyboard = self._curation_keyboard(state, page)
        try:
            await message.edit(rendered_text, buttons=keyboard)
            state["delivery_failed"] = False
            return True
        except MessageNotModifiedError:
            state["delivery_failed"] = False
            return True
        except FloodWaitError as exc:
            wait_seconds = float(getattr(exc, "seconds", 0) or 0)
            if 0 < wait_seconds <= 5:
                await asyncio.sleep(wait_seconds)
                try:
                    await message.edit(rendered_text, buttons=keyboard)
                    state["delivery_failed"] = False
                    return True
                except MessageNotModifiedError:
                    state["delivery_failed"] = False
                    return True
                except Exception:
                    METRICS.record_curation_delivery_error()
            else:
                METRICS.record_curation_delivery_error()
        except Exception:
            METRICS.record_curation_delivery_error()
        # A bad edit must not destroy a paid generation; send a fresh message.
        for attempt in range(2):
            try:
                respond = getattr(message, "respond", None)
                if callable(respond):
                    await respond(rendered_text, buttons=keyboard)
                else:
                    await self.client.send_message(user_id, rendered_text, buttons=keyboard)
                state["delivery_failed"] = False
                return True
            except Exception:
                METRICS.record_curation_delivery_error()
                if attempt == 0:
                    await asyncio.sleep(1)
        logger.warning("could not deliver curation preview user=%s", user_id)
        return False

    async def _cancel_curation(self, event: Any, user_id: int) -> None:
        state = self._curation.get(user_id)
        if state is None or not await self._release_curation(user_id, state):
            await self._ack(event, CURATION_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        await self._ack(event, "لغو شد.")  # type: ignore[attr-defined]
        if not state.get("refund_ok", True):
            result = LEARNING_REFUND_PENDING_NOTICE
        elif state.get("charged"):
            result = "اعتبار شما برگشت."
        elif state.get("free"):
            result = "هدیهٔ رایگان شما حفظ شد."
        else:
            result = "هزینه‌ای کسر نشده است."
        await self._edit(  # type: ignore[attr-defined]
            event,
            "❌ ساخت لغو شد؛ " + result,
            [[Button.inline("📚 کتابخانه من", b"library")]],
        )

    def _protected_curation_items(self, user_id: int) -> tuple[int, ...]:
        state = self._curation.get(user_id)
        if not state:
            return ()
        return (int(state["item_id"]),)

    async def _curation_source_missing(self, event: Any, user_id: int, state: dict[str, Any]) -> None:
        METRICS.record_curation_source_missing()
        await self._release_curation(user_id, state)
        if not state.get("refund_ok", True):
            message = LEARNING_REFUND_PENDING_NOTICE
        elif state.get("charged"):
            message = texts.CURATION_SOURCE_REMOVED
        else:
            message = texts.CURATION_SOURCE_REMOVED_FREE
        await self._edit(event, message, [[Button.inline(texts.BTN_BACK_MAIN, b"menu:main")]])

    async def _save_curation(self, event: Any, user_id: int) -> None:
        state = self._curation.get(user_id)
        if self._curation_stale(state):
            if state is not None:
                await self._release_curation(user_id, state)
            await self._ack(event, CURATION_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        assert state is not None
        items = state["items"]
        minimum = MIN_VALID_CARDS if state["kind"] == "deck" else MIN_VALID_QUESTIONS
        if len(items) < minimum:
            await self._ack(  # type: ignore[attr-defined]
                event,
                f"برای ذخیره حداقل {to_persian_digits(minimum)} مورد لازم است.",
                alert=True,
            )
            return
        if await self.db.library_item(user_id, int(state["item_id"])) is None:
            await self._curation_source_missing(event, user_id, state)
            return
        requested = int(state["requested"])
        cost = int(state["cost"])
        refund = ((cost * (requested - len(items)) + requested - 1) // requested
                  if state.get("charged") and len(items) < requested else 0)
        try:
            if state["kind"] == "deck":
                artifact_id = await self.db.deck_create_with_cards(
                    user_id,
                    int(state["item_id"]),
                    str(state["title"])[:200],
                    items,
                    free_claim_id=(
                        state.get("claim_id") if state.get("free") else None
                    ),
                    paid_charge_id=(
                        state.get("claim_id") if state.get("charged") else None
                    ),
                    refund_seconds_value=refund,
                )
            else:
                artifact_id = await self.db.quiz_create_session(
                    user_id,
                    int(state["item_id"]),
                    items,
                    free_claim_id=(
                        state.get("claim_id") if state.get("free") else None
                    ),
                    paid_charge_id=(
                        state.get("claim_id") if state.get("charged") else None
                    ),
                    refund_seconds_value=refund,
                )
        except BaseException as exc:
            if (isinstance(exc, sqlite3.IntegrityError)
                    and await self.db.library_item(user_id, int(state["item_id"])) is None):
                await self._curation_source_missing(event, user_id, state)
                return
            await self._release_curation(user_id, state)
            if isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            logger.exception("could not save curated learning content")
            notice = (
                LEARNING_REFUND_PENDING_NOTICE
                if not state.get("refund_ok", True)
                else LEARNING_CREDIT_RESTORED
                if state.get("charged")
                else FREE_TRY_RESTORED
                if state.get("free")
                else ""
            )
            await self._edit(  # type: ignore[attr-defined]
                event,
                LEARNING_GENERATION_FAILED + (f"\n{notice}" if notice else ""),
                [[Button.inline("📚 کتابخانه من", b"library")]],
            )
            return
        self._curation.pop(user_id, None)
        timer = self._curation_tasks.pop(user_id, None)
        if timer is not None:
            timer.cancel()
        refund_notice = ("\n" + texts.LEARNING_PRORATA_REFUND.format(
            duration=format_duration(refund)
        )) if refund else ""
        if state["kind"] == "deck":
            deck = await self.db.deck_get(artifact_id, user_id)
            await self._ack(event, "✅ مجموعه ذخیره شد." + refund_notice)  # type: ignore[attr-defined]
            if deck is None:
                await self._edit(  # type: ignore[attr-defined]
                    event,
                    "✅ مجموعه ذخیره شد؛ از «کتابخانه من» بازش کنید.",
                    [[Button.inline("↩️ فلش‌کارت‌ها", b"fc:hub")]],
                )
            else:
                await self._edit(  # type: ignore[attr-defined]
                    event,
                    self._deck_screen_text(deck, len(items)),
                    self._deck_keyboard(artifact_id, bool(items)),
                )
        else:
            await self._ack(event, "✅ آزمون آماده است." + refund_notice)  # type: ignore[attr-defined]
            await self._render_question_message(event, user_id, artifact_id)

    async def _handle_curation_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        kind = "deck" if parts[0] == "fc" else "quiz"
        action = parts[2] if len(parts) > 2 else ""
        async with self._learning_lock(user_id):
            state = self._curation.get(user_id)
            if state is None or state["kind"] != kind:
                await self._ack(event, CURATION_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                return
            if self._curation_stale(state):
                await self._release_curation(user_id, state)
                await self._ack(event, CURATION_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                return
            if action == "save":
                await self._save_curation(event, user_id)
                return
            if action == "cancel":
                await self._cancel_curation(event, user_id)
                return
            if action == "resume":
                await self._render_curation(event, user_id)
                return
            if action == "del":
                index = _to_int(parts[3]) if len(parts) > 3 else None
                if index is None or not 0 <= index < len(state["items"]):
                    await self._ack(event, CURATION_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                    return
                state["items"].pop(index)
                if not state["items"]:
                    await self._cancel_curation(event, user_id)
                    return
                await self._ack(  # type: ignore[attr-defined]
                    event,
                    f"حذف شد؛ {to_persian_digits(len(state['items']))} مورد ماند.",
                )
                await self._render_curation(event, user_id)
                return
            if action in ("p", "page"):
                page = _to_int(parts[3]) if len(parts) > 3 else None
                if page is None:
                    await self._ack(event, CURATION_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                    return
                state["page"] = max(0, page)
                await self._ack(event)  # type: ignore[attr-defined]
                await self._render_curation(event, user_id)
                return
            await self._ack(event, CURATION_STALE_ALERT, alert=True)  # type: ignore[attr-defined]

    async def _quiz_start_screen(
        self, event: Any, user_id: int, item_id: int
    ) -> None:
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)  # type: ignore[attr-defined]
            return
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._show_document_generate_screen(  # type: ignore[attr-defined]
                event, user_id, int(document["id"]), "quiz"
            )
            return
        if not item.get("content_text"):
            await self._edit(  # type: ignore[attr-defined]
                event,
                "🧪 آزمون از جزوه\n\n" + LEARNING_LEGACY_NOTICE,
                [[Button.inline("↩️ بازگشت", b"library")]],
            )
            return
        per_question = self.settings.quiz_cost_seconds_per_question
        buttons: list[list] = []
        for _code, label, _cards, questions in SIZE_PRESETS:
            buttons.append(
                [
                    Button.inline(
                        button_label(
                            f"{label} — {to_persian_digits(questions)} سؤال — "
                            f"{format_duration(questions * per_question)} اعتبار"
                        ),
                        f"quiz:go:{item_id}:{questions}".encode("ascii"),
                    )
                ]
            )
        buttons.append([self._learning_back_button(item_id)])
        badge = ""
        if await self._free_try_available(user_id, "quiz"):
            badge = f"\n\n{FREE_TRY_BADGE} — اولین آزمون شما بدون کسر اعتبار ساخته می‌شود."
        await self._edit(  # type: ignore[attr-defined]
            event,
            "🧪 آزمون از جزوه\n\n" + QUIZ_START_GUIDANCE + badge,
            buttons,
        )

    async def _quiz_generate(
        self, event: Any, user_id: int, item_id: int, count: int
    ) -> None:
        if count not in QUIZ_COUNTS:
            await self._ack(event, "تعداد سؤال نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._generate_from_document(  # type: ignore[attr-defined]
                event, user_id, int(document["id"]), "quiz", str(count)
            )
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)  # type: ignore[attr-defined]
            return
        content = item.get("content_text")
        if not content:
            await self._ack(event, LEARNING_LEGACY_NOTICE, alert=True)  # type: ignore[attr-defined]
            return
        if not self.settings.note_configured:
            await event.respond("سرویس ساخت آزمون در حال حاضر فعال نیست.")
            return
        if user_id in self._learning_in_flight:
            await self._ack(event, LEARNING_BUSY_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        if self._shutting_down:
            await self._ack(  # type: ignore[attr-defined]
                event,
                "ربات در حال راه‌اندازی مجدد است؛ دوباره تلاش کنید.",
                alert=True,
            )
            return
        self._learning_in_flight.add(user_id)
        task = asyncio.current_task()
        if task is not None:
            self._learning_tasks[user_id] = task
        try:
            async with self._learning_lock(user_id):
                await self._quiz_generate_locked(
                    event, user_id, item_id, count, content
                )
        finally:
            self._learning_in_flight.discard(user_id)
            if self._learning_tasks.get(user_id) is task:
                self._learning_tasks.pop(user_id, None)

    async def _quiz_generate_locked(
        self,
        event: Any,
        user_id: int,
        item_id: int,
        count: int,
        content: str,
    ) -> None:
        reason = await self.db.quiz_admission(
            user_id,
            max_active_hours=self.settings.quiz_max_active_hours,
            max_sessions_per_hour=self.settings.quiz_max_sessions_per_hour,
        )
        if reason == "active":
            await event.respond(
                "یک آزمون فعال دیگر دارید؛ ابتدا آن را تمام کنید یا "
                "دکمهٔ «پایان آزمون» را بزنید."
            )
            return
        if reason == "rate":
            await event.respond(
                "تعداد آزمون‌های شما در یک ساعت اخیر به حداکثر مجاز "
                "رسیده است؛ کمی بعد دوباره تلاش کنید."
            )
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None:
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)  # type: ignore[attr-defined]
            return
        await self._generate_learning(
            event,
            user_id=user_id,
            kind="quiz",
            source={
                "item_id": int(item_id),
                "doc_id": None,
                "title": self._item_display_title(item),
                "chunks": self._learning_chunks(content),
            },
            wanted=int(count),
            cost=int(count) * self.settings.quiz_cost_seconds_per_question,
            note=f"quiz-pending:{item_id}",
            title=short_title(
                self._item_display_title(item), fallback=FALLBACK_BOOKLET
            )
            + " — آزمون",
        )

    def _question_keyboard(self, session_id: int, question_id: int) -> list[list]:
        option_row = [
            Button.inline(
                OPTION_LABELS[index],
                f"quiz:ans:{session_id}:{question_id}:{index}".encode("ascii"),
            )
            for index in range(4)
        ]
        return [
            option_row,
            [
                Button.inline(
                    "⏹ پایان آزمون", f"quiz:quit:{session_id}".encode("ascii")
                )
            ],
        ]

    async def _render_question_message(
        self, message: Any, user_id: int, session_id: int
    ) -> None:
        question = await self.db.quiz_current_question(session_id)
        if question is None:
            await self._finish_after_question(message, user_id, session_id)
            return
        number = int(question["idx"]) + 1
        lines = [
            f"سؤال {to_persian_digits(number)} از "
            f"{to_persian_digits(question['question_count'])}",
            "",
            str(question["question"]),
            "",
        ]
        for index, option in enumerate(question["options"]):
            lines.append(f"{OPTION_LABELS[index]}) {option}")
        if question.get("source"):
            lines.append(f"\n📍 منبع: {question['source']}")
        await message.edit(
            "\n".join(lines),
            buttons=self._question_keyboard(session_id, int(question["question_id"])),
        )

    async def _finish_after_question(
        self, message: Any, user_id: int, session_id: int
    ) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await message.edit(LEARNING_STALE_ALERT)
            return
        await self._show_finish_screen(message, user_id, session)

    async def _show_finish_screen(
        self, message: Any, user_id: int, session: Any
    ) -> None:
        rank = await self.db.leaderboard_rank(user_id)
        correct = int(session["score"]) // QUIZ_POINTS_PER_ANSWER
        text = (
            "🎉 آزمون تمام شد!\n\n"
            f"پاسخ‌های درست: {to_persian_digits(correct)} از "
            f"{to_persian_digits(session['question_count'])}\n"
            f"امتیاز کسب‌شده: {to_persian_digits(session['score'])}\n"
            "رتبهٔ شما در لیدربورد: "
            f"{to_persian_digits(rank['rank'])}"
        )
        await message.edit(text, buttons=self._learning_done_buttons())

    def _learning_done_buttons(self) -> list[list]:
        return [
            [Button.inline("🏆 لیدربورد", b"board")],
            [Button.inline("📚 کتابخانه من", b"library")],
            [Button.inline("↩️ منوی اصلی", b"menu:main")],
        ]

    async def _quiz_answer(
        self,
        event: Any,
        user_id: int,
        session_id: int,
        question_id: int,
        option: int,
    ) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        result = await self.db.quiz_record_answer(session_id, question_id, option)
        if result["status"] == "stale":
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        if result["status"] == "already":
            await self._ack(event, "پاسخ شما ثبت شده بود.")  # type: ignore[attr-defined]
            return
        correct_text = result["options"][result["correct_index"]]
        if result["correct"]:
            lines = [f"✅ پاسخ درست: {correct_text}"]
        else:
            lines = [
                "❌ پاسخ شما درست نبود.",
                f"✅ پاسخ درست: {correct_text}",
            ]
        if result["explanation"]:
            lines.append(f"💡 {result['explanation']}")
        next_label = "مشاهدهٔ نتیجه ➡️" if result["finished"] else "سؤال بعدی ➡️"
        await self._edit(  # type: ignore[attr-defined]
            event,
            "\n".join(lines),
            [[Button.inline(next_label, f"quiz:next:{session_id}".encode("ascii"))]],
        )

    async def _quiz_next(self, event: Any, user_id: int, session_id: int) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        if session["status"] == "done":
            await self._show_finish_screen(event, user_id, session)
            return
        if session["status"] != "active":
            await self._show_quiz_summary(event, user_id, session)
            return
        await self._render_question_message(event, user_id, session_id)

    async def _quiz_quit(self, event: Any, user_id: int, session_id: int) -> None:
        session = await self.db.quiz_get_session(session_id, user_id)
        if session is None:
            await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        if session["status"] == "active":
            await self.db.quiz_finish(session_id, "quit")
            session = await self.db.quiz_get_session(session_id, user_id)
        await self._show_quiz_summary(event, user_id, session)

    async def _show_quiz_summary(
        self, event: Any, user_id: int, session: Any
    ) -> None:
        answered = min(int(session["current_index"]), int(session["question_count"]))
        correct = int(session["score"]) // QUIZ_POINTS_PER_ANSWER
        rank = await self.db.leaderboard_rank(user_id)
        text = (
            "⏹ آزمون پایان یافت.\n\n"
            f"پاسخ‌های درست: {to_persian_digits(correct)} از "
            f"{to_persian_digits(answered)}\n"
            f"امتیاز کسب‌شده: {to_persian_digits(session['score'])}\n"
            f"رتبهٔ شما در لیدربورد: {to_persian_digits(rank['rank'])}"
        )
        await self._edit(event, text, self._learning_done_buttons())  # type: ignore[attr-defined]

    async def _show_leaderboard(self, event: Any) -> None:
        user_id = event.sender_id
        if user_id is None:
            return
        rows = await self.db.leaderboard_top(20)
        if not rows:
            await self._edit_or_reply(  # type: ignore[attr-defined]
                event,
                LEADERBOARD_EMPTY,
                [[Button.inline("↩️ منوی اصلی", b"menu:main")]],
            )
            return
        medals = ("🥇", "🥈", "🥉")
        lines = ["🏆 لیدربورد", ""]
        for index, row in enumerate(rows):
            rank_label = medals[index] if index < 3 else to_persian_digits(index + 1)
            name = row["first_name"] or LEADERBOARD_FALLBACK_NAME
            lines.append(
                f"{rank_label} {name} — "
                f"{to_persian_digits(row['quiz_points'])} امتیاز"
            )
        rank = await self.db.leaderboard_rank(user_id)
        lines.append("")
        lines.append(
            f"رتبهٔ شما: {to_persian_digits(rank['rank'])} با "
            f"{to_persian_digits(rank['points'])} امتیاز"
        )
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event,
            "\n".join(lines),
            [[Button.inline("↩️ منوی اصلی", b"menu:main")]],
        )

    async def _admin_board(self, event: Any, *, notice: str = "") -> None:
        rows = await self.db.leaderboard_admin_top(5)
        identities = await self.db.identities_for(row["telegram_id"] for row in rows)
        lines = ["🏆 برترین کاربران", ""]
        buttons: list[list] = []
        if not rows:
            lines.append("هنوز کاربری امتیازی کسب نکرده است.")
        for index, row in enumerate(rows):
            identity = identities.get(row["telegram_id"], {})
            name = self._identity_line(  # type: ignore[attr-defined]
                username=identity.get("username"),
                telegram_id=row["telegram_id"],
                display_name=self._user_display_name(  # type: ignore[attr-defined]
                    identity.get("first_name", ""), identity.get("last_name", "")
                ),
            )
            lines.append(
                f"{to_persian_digits(index + 1)}. {name} — "
                f"{to_persian_digits(row['quiz_points'])} امتیاز"
            )
            buttons.append(
                [
                    Button.inline(
                        button_label(
                            f"{to_persian_digits(index + 1)}. {name} — "
                            f"{to_persian_digits(row['quiz_points'])} امتیاز"
                        ),
                        b"adm:board",
                    ),
                    Button.inline(
                        "🎁 هدیه",
                        f"adm:bg:{row['telegram_id']}".encode("ascii"),
                    ),
                ]
            )
        if notice:
            lines.extend(["", notice])
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
        await self._edit(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _admin_gift_pick(self, event: Any, target_id: int) -> None:
        identity = await self.db.user_identity(target_id)
        if identity is None or identity["banned"]:
            await self._ack(event, "این کاربر در دسترس نیست.", alert=True)  # type: ignore[attr-defined]
            return
        plans = [plan for plan in await self.db.plans() if plan["price"] > 0]
        lines = [
            "🎁 هدیه به کاربر",
            f"کاربر: {await self._identity_for_id(target_id)}",  # type: ignore[attr-defined]
            "",
            "یکی از بسته‌های اعتباری را انتخاب کنید:",
        ]
        buttons: list[list] = []
        for plan in plans:
            hours = float(plan["hours"])
            hours_text = to_persian_digits(
                int(hours) if hours.is_integer() else str(hours)
            )
            buttons.append(
                [
                    Button.inline(
                        button_label(
                            f"{hours_text} ساعت — {format_duration(int(round(hours * 3600)))}"
                        ),
                        f"adm:bg:{target_id}:{plan['code']}".encode("utf-8"),
                    )
                ]
            )
        buttons.append([Button.inline("↩️ بازگشت", b"adm:board")])
        await self._edit(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _admin_gift_confirm(
        self, event: Any, target_id: int, plan_code: str
    ) -> None:
        plan = await self._gift_plan(target_id, plan_code)
        identity = await self.db.user_identity(target_id)
        if plan is None or identity is None or identity["banned"]:
            await self._ack(event, "این هدیه دیگر معتبر نیست.", alert=True)  # type: ignore[attr-defined]
            return
        name = await self._identity_for_id(target_id)  # type: ignore[attr-defined]
        seconds = int(round(float(plan["hours"]) * 3600))
        text = f"هدیه به {name} با اعتبار {format_duration(seconds)} ثبت شود؟"
        await self._edit(  # type: ignore[attr-defined]
            event,
            text,
            [
                [
                    Button.inline(
                        "✅ تأیید هدیه",
                        f"adm:bgok:{target_id}:{plan_code}".encode("utf-8"),
                    )
                ],
                [Button.inline("↩️ بازگشت", f"adm:bg:{target_id}".encode("utf-8"))],
            ],
        )

    async def _gift_plan(self, target_id: int, plan_code: str) -> dict | None:
        identity = await self.db.user_identity(target_id)
        if identity is None or identity["banned"]:
            return None
        for plan in await self.db.plans():
            if plan["code"] == plan_code and plan["price"] > 0:
                return plan
        return None

    async def _admin_gift_execute(
        self, event: Any, admin_id: int, target_id: int, plan_code: str
    ) -> None:
        if not await self._check_admin_cooldown(  # type: ignore[attr-defined]
            event, "gift", self.ADMIN_COMMAND_COOLDOWN_SECONDS  # type: ignore[attr-defined]
        ):
            return
        plan = await self._gift_plan(target_id, plan_code)
        if plan is None:
            await self._ack(event, "این هدیه دیگر معتبر نیست.", alert=True)  # type: ignore[attr-defined]
            return
        if self._is_admin(target_id):  # type: ignore[attr-defined]
            await event.respond(
                "برای حفظ تفکیک دسترسی‌ها، امکان افزودن اعتبار به حساب مدیر دیگر وجود ندارد."
            )
            return
        seconds = int(round(float(plan["hours"]) * 3600))
        await self.db.add_credit(
            target_id,
            seconds,
            note="leaderboard gift",
            validity_days=None,
            admin_id=admin_id,
        )
        message = (
            "🎁 تبریک! شما جزو ۵ کاربر برتر گاماس هستید و "
            f"{format_duration(seconds)} اعتبار رایگان به حساب شما اضافه شد."
        )
        try:
            await self.client.send_message(target_id, message)
        except Exception:
            logger.warning("gift DM failed user=%s", target_id, exc_info=True)
        await self._admin_board(event, notice="✅ هدیه ثبت شد.")

    @staticmethod
    def _deck_stale_alert() -> str:
        return "این فلش‌کارت دیگر معتبر نیست."

    @staticmethod
    def _library_icon(kind: str) -> str:
        return {
            "booklet": "📖",
            "transcript": "📝",
            "office": "🗂",
            "document": "📄",
        }.get(kind, "📄")

    def _item_display_title(self, item: dict) -> str:
        kind = str(item.get("kind") or "")
        fallback = {
            "booklet": FALLBACK_BOOKLET,
            "office": FALLBACK_OFFICE,
            "transcript": "متن گفتار",
        }.get(kind, FALLBACK_DOCUMENT)
        raw = " ".join(str(item.get("title") or "").split())
        raw = raw.lstrip("📖📝🗂📄✅ ").strip()
        return short_title(raw, fallback=fallback)

    async def _flashcard_start_screen(
        self, event: Any, user_id: int, item_id: int
    ) -> None:
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)  # type: ignore[attr-defined]
            return
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._show_document_generate_screen(  # type: ignore[attr-defined]
                event, user_id, int(document["id"]), "deck"
            )
            return
        if not item.get("content_text"):
            await self._edit(  # type: ignore[attr-defined]
                event,
                "🃏 فلش‌کارت\n\n" + LEARNING_LEGACY_NOTICE,
                [[Button.inline("↩️ بازگشت", b"library")]],
            )
            return
        size = self.settings.flashcard_ai_deck_size
        cost = self.settings.flashcard_ai_cost_seconds
        text = (
            "ساخت فلش‌کارت با هوش مصنوعی از این جزوه. مجموعه‌ای از "
            f"{to_persian_digits(size)} پرسش و پاسخ ساخته می‌شود.\n"
            f"هزینه: {format_duration(cost)} اعتبار"
        )
        badge = ""
        if await self._free_try_available(user_id, "deck"):
            badge = (
                f"\n\n{FREE_TRY_BADGE} — اولین مجموعهٔ فلش‌کارت شما بدون کسر "
                "اعتبار ساخته می‌شود."
            )
        buttons: list[list] = [
            [Button.inline("🤖 ساخت فلش‌کارت", f"fc:go:{item_id}".encode("ascii"))]
        ]
        for code, label, cards, _questions in SIZE_PRESETS:
            buttons.append(
                [
                    Button.inline(
                        button_label(
                            f"{label} — {to_persian_digits(cards)} کارت — "
                            f"{format_duration(self._preset_cost(cost, cards, size))} اعتبار"
                        ),
                        f"fc:go:{item_id}:{code}".encode("ascii"),
                    )
                ]
            )
        buttons.append([self._learning_back_button(item_id)])
        await self._edit(  # type: ignore[attr-defined]
            event,
            text + badge + "\n\nاندازهٔ مجموعه را انتخاب کنید:",
            buttons,
        )

    async def _flashcard_generate(
        self, event: Any, user_id: int, item_id: int, preset: str | None = None
    ) -> None:
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._generate_from_document(  # type: ignore[attr-defined]
                event, user_id, int(document["id"]), "deck", preset or ""
            )
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)  # type: ignore[attr-defined]
            return
        content = item.get("content_text")
        if not content:
            await self._ack(event, LEARNING_LEGACY_NOTICE, alert=True)  # type: ignore[attr-defined]
            return
        if not self.settings.note_configured:
            await event.respond("سرویس ساخت فلش‌کارت در حال حاضر فعال نیست.")
            return
        if (
            await self.db.deck_count(user_id)
            >= self.settings.flashcard_max_decks_per_user
        ):
            await event.respond(
                "به حداکثر تعداد مجموعه‌های فلش‌کارت رسیده‌اید؛ برای ساخت مجموعهٔ جدید "
                "یک مجموعهٔ قدیمی را حذف کنید."
            )
            return
        if user_id in self._learning_in_flight:
            await self._ack(event, LEARNING_BUSY_ALERT, alert=True)  # type: ignore[attr-defined]
            return
        if self._shutting_down:
            await self._ack(  # type: ignore[attr-defined]
                event,
                "ربات در حال راه‌اندازی مجدد است؛ دوباره تلاش کنید.",
                alert=True,
            )
            return
        self._learning_in_flight.add(user_id)
        task = asyncio.current_task()
        if task is not None:
            self._learning_tasks[user_id] = task
        try:
            async with self._learning_lock(user_id):
                await self._flashcard_generate_locked(
                    event,
                    user_id,
                    item_id,
                    content,
                    self._item_display_title(item),
                    preset,
                )
        finally:
            self._learning_in_flight.discard(user_id)
            if self._learning_tasks.get(user_id) is task:
                self._learning_tasks.pop(user_id, None)

    def _deck_size_and_cost(self, preset: str | None) -> tuple[int, int]:
        base_size = int(self.settings.flashcard_ai_deck_size)
        base_cost = int(self.settings.flashcard_ai_cost_seconds)
        if preset is None:
            return base_size, base_cost
        cards = preset_cards(str(preset))
        if cards is None:
            raise ValueError("invalid flashcard preset")
        return cards, self._preset_cost(base_cost, cards, base_size)

    async def _flashcard_generate_locked(
        self,
        event: Any,
        user_id: int,
        item_id: int,
        content: str,
        item_title: str,
        preset: str | None = None,
    ) -> None:
        size, cost = self._deck_size_and_cost(preset)
        await self._generate_learning(
            event,
            user_id=user_id,
            kind="deck",
            source={
                "item_id": int(item_id),
                "doc_id": None,
                "title": item_title,
                "chunks": self._learning_chunks(content),
            },
            wanted=size,
            cost=cost,
            note=f"fc-pending:{item_id}",
            title=short_title(item_title, fallback=FALLBACK_BOOKLET) + " — فلش‌کارت",
        )

    async def _show_flashcard_hub(self, event: Any, *, page: int = 0) -> None:
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 6
        count = await self.db.deck_count(user_id)
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        decks = await self.db.decks_list(
            user_id, limit=page_size, offset=page * page_size
        )
        if count == 0:
            text = FLASHCARD_EMPTY
            buttons: list[list] = []
        else:
            text = (
                "🃏 فلش‌کارت‌ها\n\n" f"تعداد مجموعه‌ها: {to_persian_digits(count)}"
            )
            buttons = []
            for deck in decks:
                label = button_label(
                    f"{deck['title']} ({to_persian_digits(deck['card_count'])} کارت)"
                )
                buttons.append(
                    [Button.inline(label, f"fc:deck:{deck['deck_id']}".encode("ascii"))]
                )
            nav = []
            if page > 0:
                nav.append(
                    Button.inline("◀️ قبلی", f"fc:hub:{page - 1}".encode("ascii"))
                )
            if (page + 1) * page_size < count:
                nav.append(
                    Button.inline("بعدی ▶️", f"fc:hub:{page + 1}".encode("ascii"))
                )
            if nav:
                buttons.append(nav)
        buttons.extend(
            [
                [Button.inline("✍️ ساخت دستی", b"fc:man")],
                [Button.inline("🤖 از یک جزوه بساز", b"fc:pick:0")],
                [
                    Button.inline("📖 جزوه‌ها", b"library"),
                    Button.inline("↩️ منوی اصلی", b"menu:main"),
                ],
            ]
        )
        await self._edit_or_reply(event, text, buttons)  # type: ignore[attr-defined]

    async def _show_booklet_picker(self, event: Any, *, page: int = 0) -> None:
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 6
        count = await self.db.library_count_kind(user_id, "booklet")
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        items = await self.db.library_list_kind(
            user_id, "booklet", limit=page_size, offset=page * page_size
        )
        buttons: list[list] = []
        if not items:
            text = (
                "🤖 ساخت فلش‌کارت از جزوه\n\n"
                "هنوز جزوه‌ای در کتابخانهٔ شما ذخیره نشده است."
            )
        else:
            text = "یک جزوه را برای ساخت فلش‌کارت انتخاب کنید:"
            for item in items:
                title = self._item_display_title(item)
                buttons.append(
                    [
                        Button.inline(
                            button_label(f"📖 {title}"),
                            f"fc:ai:{item['item_id']}".encode("ascii"),
                        )
                    ]
                )
            nav = []
            if page > 0:
                nav.append(
                    Button.inline("◀️ قبلی", f"fc:pick:{page - 1}".encode("ascii"))
                )
            if (page + 1) * page_size < count:
                nav.append(
                    Button.inline("بعدی ▶️", f"fc:pick:{page + 1}".encode("ascii"))
                )
            if nav:
                buttons.append(nav)
        buttons.append([Button.inline("↩️ بازگشت", b"fc:hub")])
        await self._edit(event, text, buttons)  # type: ignore[attr-defined]

    async def _start_manual_deck(self, event: Any, user_id: int) -> None:
        if (
            await self.db.deck_count(user_id)
            >= self.settings.flashcard_max_decks_per_user
        ):
            await event.respond(
                "به حداکثر تعداد مجموعه‌های فلش‌کارت رسیده‌اید؛ یک مجموعهٔ قدیمی را حذف کنید."
            )
            return
        deck_id = await self.db.manual_deck_get_or_create(user_id)
        self._pending_flashcard[user_id] = (deck_id, "front")
        self._pending_flashcard_front.pop(user_id, None)
        await event.respond(
            FLASHCARD_ASK_FRONT,
            buttons=[[Button.inline("✅ پایان", b"fc:man:done")]],
        )

    async def _handle_flashcard_text(
        self, event: Any, user_id: int, text: str
    ) -> None:
        deck_id, phase = self._pending_flashcard[user_id]
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
            await event.reply("این مجموعه دیگر در دسترس نیست.")
            return
        end_button = [[Button.inline("✅ پایان", b"fc:man:done")]]
        if not text:
            await event.reply("لطفاً متن را در یک پیام متنی بفرستید.")
            return
        if phase == "front":
            self._pending_flashcard_front[user_id] = text[:MAX_MANUAL_FRONT_CHARS]
            self._pending_flashcard[user_id] = (deck_id, "back")
            await event.reply(FLASHCARD_ASK_BACK, buttons=end_button)
            return
        front = self._pending_flashcard_front.pop(user_id, "")
        if not front:
            self._pending_flashcard[user_id] = (deck_id, "front")
            await event.reply(FLASHCARD_ASK_FRONT, buttons=end_button)
            return
        try:
            count = await self.db.deck_add_card(
                deck_id, front, text[:MAX_MANUAL_BACK_CHARS],
                max_cards=self.settings.flashcard_max_cards_per_deck,
            )
        except FlashcardLimitError:
            self._pending_flashcard.pop(user_id, None)
            await event.reply(texts.FLASHCARD_CARD_LIMIT)
            return
        self._pending_flashcard[user_id] = (deck_id, "front")
        await event.reply(
            f"کارت اضافه شد ({to_persian_digits(count)} کارت). "
            "پرسش کارت بعدی را بفرستید.",
            buttons=end_button,
        )

    def _deck_screen_text(self, deck: dict, card_count: int) -> str:
        return (
            f"🃏 {deck['title']}\n\n"
            f"تعداد کارت‌ها: {to_persian_digits(card_count)}"
        )

    def _deck_keyboard(self, deck_id: int, has_cards: bool) -> list[list]:
        buttons: list[list] = []
        if has_cards:
            buttons.append(
                [
                    Button.inline(
                        "▶️ مرور فلش‌کارت‌ها", f"fc:study:{deck_id}".encode("ascii")
                    )
                ]
            )
        buttons.append(
            [Button.inline("📄 خروجی Word", f"fc:exp:{deck_id}".encode("ascii"))]
        )
        buttons.append(
            [Button.inline("🗑 حذف دسته", f"fc:del:{deck_id}".encode("ascii"))]
        )
        buttons.append([Button.inline("↩️ بازگشت", b"fc:hub")])
        return buttons

    async def _show_deck_screen(
        self, event: Any, user_id: int, deck_id: int
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
            return
        cards = await self.db.deck_cards(deck_id)
        await self._edit(  # type: ignore[attr-defined]
            event,
            self._deck_screen_text(deck, len(cards)),
            self._deck_keyboard(deck_id, bool(cards)),
        )

    async def _study_front(
        self, event: Any, user_id: int, deck_id: int, *, restart: bool = False
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
            return
        cards = await self.db.deck_cards(deck_id)
        if not cards:
            await self._ack(  # type: ignore[attr-defined]
                event, "این مجموعه هنوز کارتی ندارد.", alert=True
            )
            return
        cursor = 0 if restart else min(int(deck["study_cursor"]), len(cards) - 1)
        if restart:
            await self.db.deck_set_cursor(deck_id, 0)
        card = cards[cursor]
        source = f"\n\n📍 منبع: {card['source']}" if card.get("source") else ""
        await self._edit(  # type: ignore[attr-defined]
            event,
            "پرسش:\n\n" + card["front"] + source,
            [
                [
                    Button.inline(
                        "👁 نمایش پاسخ",
                        f"fc:flip:{deck_id}:{card['card_id']}".encode("ascii"),
                    )
                ],
                [
                    Button.inline(
                        "⏹ پایان مرور", f"fc:done:{deck_id}".encode("ascii")
                    )
                ],
            ],
        )

    async def _flip_card(
        self, event: Any, user_id: int, deck_id: int, card_id: int
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
            return
        card = await self.db.deck_card(deck_id, card_id)
        if card is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
            return
        source = f"\n\n📍 منبع: {card['source']}" if card.get("source") else ""
        await self._edit(  # type: ignore[attr-defined]
            event,
            "پاسخ:\n\n" + card["back"] + source,
            [
                [
                    Button.inline(
                        "➡️ کارت بعدی", f"fc:next:{deck_id}".encode("ascii")
                    )
                ],
                [
                    Button.inline(
                        "⏹ پایان مرور", f"fc:done:{deck_id}".encode("ascii")
                    )
                ],
            ],
        )

    async def _next_card(
        self, event: Any, user_id: int, deck_id: int
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
            return
        total = len(await self.db.deck_cards(deck_id))
        if total == 0:
            await self._ack(  # type: ignore[attr-defined]
                event, "این مجموعه هنوز کارتی ندارد.", alert=True
            )
            return
        new_cursor = int(deck["study_cursor"]) + 1
        await self.db.deck_set_cursor(deck_id, new_cursor)
        if new_cursor >= total:
            await self._edit(  # type: ignore[attr-defined]
                event,
                ("مرور تمام شد — " f"{to_persian_digits(total)} کارت مرور شد."),
                [
                    [
                        Button.inline(
                            "🔁 از ابتدا", f"fc:restart:{deck_id}".encode("ascii")
                        )
                    ],
                    [Button.inline("↩️ بازگشت", b"fc:hub")],
                ],
            )
            return
        await self._study_front(event, user_id, deck_id)

    async def _export_deck_docx(
        self, event: Any, user_id: int, deck_id: int
    ) -> None:
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
            return
        cards = await self.db.deck_cards(deck_id)
        if not cards:
            await self._ack(  # type: ignore[attr-defined]
                event, "این مجموعه هنوز کارتی ندارد.", alert=True
            )
            return
        progress = WorkProgress.for_editor(
            lambda text: self._edit(event, text, []),  # type: ignore[attr-defined]
            headline="🃏 خروجی فلش‌کارت",
            done_headline="✅ فایل آماده شد",
        )
        await progress.update(
            15,
            "ساخت فایل Word",
            f"{to_persian_digits(len(cards))} کارت در حال قالب‌بندی است…",
        )
        body_parts = [
            f"پرسش {to_persian_digits(index)}: {card['front']}\nپاسخ: {card['back']}"
            + (f"\nمنبع: {card['source']}" if card.get("source") else "")
            for index, card in enumerate(cards, start=1)
        ]
        deck_title = short_title(str(deck["title"] or ""), fallback=FALLBACK_BOOKLET)
        meta = DocumentMeta(
            reference=f"flashcard-{deck_id}",
            source_name="flashcards",
            created_at=datetime.now(),
        )
        docx_bytes = await asyncio.to_thread(
            build_plain_docx,
            deck_title,
            "\n\n".join(body_parts),
            fonts=resolve_fonts(),
            design=resolve_design(),
            meta=meta,
        )
        workdir = Path(
            await run_file_io_resource(
                tempfile.mkdtemp,
                lambda path: shutil.rmtree(path, ignore_errors=True),
                prefix="deck-",
                dir=self.settings.temp_dir,
            )
        )
        path = workdir / f"flashcard-{deck_id}.docx"
        try:
            await run_file_io(path.write_bytes, docx_bytes)
            await progress.update(70, "ارسال فایل", "فایل Word در حال ارسال است…")
            await self._send_telegram_file(  # type: ignore[attr-defined]
                event.chat_id,
                path,
                caption=f"🃏 فلش‌کارت‌ها — {deck_title}"[:TELEGRAM_CAPTION_LIMIT],
                force_document=True,
            )
        except Exception:
            await self._edit(  # type: ignore[attr-defined]
                event,
                failed_progress_text(
                    progress.percent,
                    "ارسال فایل Word ناموفق بود؛ دوباره تلاش کنید.",
                    headline="❌ خروجی فلش‌کارت ناموفق بود.",
                ),
                self._deck_keyboard(deck_id, True),
            )
            raise
        finally:
            await run_file_io(shutil.rmtree, workdir, ignore_errors=True)
        await self._edit(  # type: ignore[attr-defined]
            event,
            completed_progress_text(
                f"✅ فایل Word شامل {to_persian_digits(len(cards))} کارت ارسال شد.",
                headline="✅ فایل آماده شد",
            ),
            self._deck_keyboard(deck_id, True),
        )

    async def _route_flashcard_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "cur":
            await self._handle_curation_callback(event, user_id, data)
            return
        if action == "hub":
            page = _to_int(parts[2]) if len(parts) == 3 else 0
            await self._show_flashcard_hub(event, page=max(0, page or 0))
            return
        if action == "pick":
            page = _to_int(parts[2]) if len(parts) == 3 else 0
            await self._show_booklet_picker(event, page=max(0, page or 0))
            return
        if action == "man" and len(parts) == 2:
            await self._start_manual_deck(event, user_id)
            return
        if action == "man" and len(parts) == 3 and parts[2] == "done":
            state = self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
            if state is None:
                await self._show_flashcard_hub(event)
            else:
                await event.respond("✅ ساخت کارت پایان یافت.")
                await self._show_deck_screen(event, user_id, state[0])
            return
        if action == "ai" and len(parts) == 3:
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
                return
            await self._flashcard_start_screen(event, user_id, item_id)
            return
        if action == "go" and len(parts) in (3, 4):
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
                return
            preset = parts[3] if len(parts) == 4 else None
            if preset is not None and preset_cards(preset) is None:
                await self._ack(event, texts.INVALID_LEARNING_SIZE, alert=True)
                return
            if not await self._require_membership(
                event, user_id, kind="callback", callback_data=data
            ):
                return
            await self._flashcard_generate(event, user_id, item_id, preset)
            return
        if (
            action
            in {
                "deck",
                "study",
                "exp",
                "del",
                "delok",
                "done",
                "restart",
                "next",
            }
            and len(parts) == 3
        ):
            deck_id = _to_int(parts[2])
            if deck_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
                return
            if action == "deck":
                await self._show_deck_screen(event, user_id, deck_id)
            elif action == "study":
                await self._study_front(event, user_id, deck_id)
            elif action == "exp":
                if not await self._require_membership(
                    event, user_id, kind="callback", callback_data=data
                ):
                    return
                await self._export_deck_docx(event, user_id, deck_id)
            elif action == "del":
                deck = await self.db.deck_get(deck_id, user_id)
                if deck is None:
                    await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
                    return
                await self._edit(  # type: ignore[attr-defined]
                    event,
                    "آیا مطمئنید؟",
                    [
                        [
                            Button.inline(
                                "✅ بله، حذف کن",
                                f"fc:delok:{deck_id}".encode("ascii"),
                            )
                        ],
                        [
                            Button.inline(
                                "↩️ بازگشت", f"fc:deck:{deck_id}".encode("ascii")
                            )
                        ],
                    ],
                )
            elif action == "delok":
                deleted = await self.db.deck_delete(deck_id, user_id)
                if not deleted:
                    await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
                else:
                    await self._show_flashcard_hub(event)
            elif action == "done":
                await self._show_deck_screen(event, user_id, deck_id)
            elif action == "restart":
                await self._study_front(event, user_id, deck_id, restart=True)
            elif action == "next":
                await self._next_card(event, user_id, deck_id)
            return
        if action == "flip" and len(parts) == 4:
            deck_id = _to_int(parts[2])
            card_id = _to_int(parts[3])
            if deck_id is None or card_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]
                return
            await self._flip_card(event, user_id, deck_id, card_id)
            return
        await self._ack(event, self._deck_stale_alert(), alert=True)  # type: ignore[attr-defined]

    async def _route_quiz_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "cur":
            await self._handle_curation_callback(event, user_id, data)
            return
        if action == "new" and len(parts) == 3:
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                return
            await self._quiz_start_screen(event, user_id, item_id)
            return
        if action == "go" and len(parts) == 4:
            item_id = _to_int(parts[2])
            count = _to_int(parts[3])
            if item_id is None or count is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                return
            if not await self._require_membership(
                event, user_id, kind="callback", callback_data=data
            ):
                return
            await self._quiz_generate(event, user_id, item_id, count)
            return
        if action == "ans" and len(parts) == 5:
            session_id = _to_int(parts[2])
            question_id = _to_int(parts[3])
            option = _to_int(parts[4])
            if session_id is None or question_id is None or option is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                return
            await self._quiz_answer(
                event, user_id, session_id, question_id, option
            )
            return
        if action in ("next", "quit") and len(parts) == 3:
            session_id = _to_int(parts[2])
            if session_id is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
                return
            if action == "next":
                await self._quiz_next(event, user_id, session_id)
            else:
                await self._quiz_quit(event, user_id, session_id)
            return
        await self._ack(event, LEARNING_STALE_ALERT, alert=True)  # type: ignore[attr-defined]
