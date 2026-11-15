"""Interactive HTML quizzes and flashcards (SRP / DIP).

Encapsulates parallel chunk generation, source-grounded item anchoring,
deterministic offline HTML rendering, direct Telegram delivery + SQLite
library persistence, and manual flashcard deck building.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
import io
import logging
import math
import secrets
import sqlite3
from typing import TYPE_CHECKING, Any

from telethon import Button
from telethon.tl.types import DocumentAttributeFilename

from . import texts
from .artifact_validation import validate_learning_html_artifact
from .billing import format_seconds_as_credits
from .bot_ui import (
    FLASHCARD_ASK_BACK,
    FLASHCARD_ASK_FRONT,
    FREE_TRY_BADGE,
    FREE_TRY_RESTORED,
    LEARNING_BUSY_ALERT,
    LEARNING_CREDIT_RESTORED,
    LEARNING_GENERATION_FAILED,
    LEARNING_GENERATION_RETRY,
    LEARNING_LEGACY_NOTICE,
    LEARNING_NO_CREDIT,
    LEARNING_REFUND_PENDING_NOTICE,
    LEARNING_STALE_ALERT,
    QUIZ_COUNTS,
    QUIZ_START_GUIDANCE,
    TELEGRAM_CAPTION_LIMIT,
)
from .db import InsufficientBalanceError
from .db_learning import FlashcardLimitError
from .doc_extract import (
    DocChunk,
    chunk_text,
    chunks_from_json,
)
from .fileio import run_file_io
from .intake import _to_int
from .learning import (
    FLASHCARD_SCHEMA,
    MAX_MANUAL_BACK_CHARS,
    MAX_MANUAL_FRONT_CHARS,
    MAX_MODEL_INPUT_CHARS,
    MIN_VALID_CARDS,
    MIN_VALID_QUESTIONS,
    QUIZ_SCHEMA,
    SIZE_PRESETS,
    GenerationShortfallError,
    anchor_items,
    dedupe_cards,
    dedupe_questions,
    distribute_counts,
    flashcard_system_prompt,
    learning_user_prompt,
    parse_flashcard_payload,
    parse_quiz_payload,
    preset_cards,
    quiz_system_prompt,
    source_key,
    topic_importance_weight,
)
from .learning_html import (
    LearningHtmlArtifact,
    render_flashcards_html,
    render_quiz_html,
)
from .llm import LLMContextOverflowError, LLMOutputBudgetError
from .notes import SENSITIVITY_UNKNOWN, resolve_note_sensitivity
from .progress import (
    WORK_PROGRESS_HEADLINE,
    WorkProgress,
    completed_progress_text,
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


def _resolve_learning_sensitivity(
    settings: Any, sensitivity_text: object, chunks: list[DocChunk]
) -> str:
    """Build one source view and classify it without logging source material."""
    if not isinstance(sensitivity_text, str) or not sensitivity_text.strip():
        sensitivity_text = "\n".join(str(chunk.text) for chunk in chunks)
    return resolve_note_sensitivity(settings, sensitivity_text)


def _log_quiz_dedupe_diagnostics(
    input_count: int, accepted_count: int, stage: str
) -> None:
    """Log only item counts when strict quiz deduplication removes repeats."""
    removed = max(0, int(input_count) - int(accepted_count))
    if removed:
        logger.info(
            "quiz deduplication removed duplicate items stage=%s count=%d",
            stage,
            removed,
            extra={
                "event": "quiz_dedupe_diagnostics",
                "dedupe_stage": stage,
                "dedupe_input_count": max(0, int(input_count)),
                "dedupe_accepted_count": max(0, int(accepted_count)),
                "dedupe_rejected_duplicate_count": removed,
            },
        )


class LearningWorkflowMixin:
    """Direct interactive HTML generation for quizzes & flashcards plus manual card entry."""

    db: Any
    settings: Any
    client: Any
    deps: BotDependencies
    _shutting_down: bool
    _learning_locks: MutableMapping[int, asyncio.Lock]
    _learning_global_semaphore: Any
    _upload_semaphore: Any
    _edit: Any
    _ack: Any
    _learning_in_flight: set[int]
    _learning_tasks: dict[int, asyncio.Task[Any]]
    _learning_commit_tasks: set[asyncio.Task[Any]]
    _pending_flashcard: dict[int, tuple[int, str]]
    _pending_flashcard_front: dict[int, str]

    CHUNK_PROGRESS_START = 6
    CHUNK_PROGRESS_END = 86
    MAX_ITEMS_PER_CALL = 20

    async def _require_membership(
        self, event: Any, user_id: int, *, kind: str, callback_data: str = ""
    ) -> bool:
        """Provided by the composed bot; gates one value-delivery action."""
        raise NotImplementedError

    def _learning_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._learning_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._learning_locks[user_id] = lock
        return lock

    async def _learning_back_button_for_item(self, user_id: int, item_id: int) -> Any:
        item = await self.db.library_item(user_id, item_id)
        if item is not None and int(item.get("delivered") or 0) == 1:
            return Button.inline("↩️ بازگشت", f"lib:it:{item_id}".encode("ascii"))
        return Button.inline("📚 کتابخانه", b"library")

    async def _free_try_available(self, user_id: int, kind: str) -> bool:
        if await self.db.is_unlimited(user_id):
            return False
        return bool((await self.db.free_use_state(user_id)).get(kind, False))

    def _preset_cost(self, base_cost: int, cards: int, base_size: int) -> int:
        base_size = max(1, int(base_size))
        quotient, remainder = divmod(int(base_cost) * int(cards), base_size)
        rounded = quotient + int(
            2 * remainder > base_size
            or (2 * remainder == base_size and quotient % 2 == 1)
        )
        return max(1, rounded)

    def _learning_max_chars(self) -> int:
        context_tokens = int(
            getattr(self.settings, "note_context_tokens", 131_072) or 131_072
        )
        output_tokens = int(
            getattr(self.settings, "learning_max_output_tokens", 4096) or 4096
        )
        reserved_output = min(output_tokens, context_tokens // 2)
        available_tokens = max(1_000, context_tokens - reserved_output - 2_000)
        return min(MAX_MODEL_INPUT_CHARS, max(2_000, int(available_tokens * 2.0)))

    def _learning_chunks(self, content: str) -> list[DocChunk]:
        return chunk_text(content, max_chars=self._learning_max_chars())

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
            "sensitivity_text": str(document.get("text") or ""),
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
        del note
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
        detail = texts.LEARNING_CHUNK_PROGRESS.format(
            done=to_persian_digits(int(done)), total=to_persian_digits(total)
        )
        try:
            if callable(getattr(placeholder, "update", None)):
                await placeholder.update(percent, stage, detail)
                return
            await placeholder.edit(
                progress_text(percent, stage, detail, headline=headline)
            )
        except Exception:
            logger.debug("generation progress edit failed", exc_info=True)

    async def _send_learning_html(
        self,
        event: Any,
        user_id: int,
        artifact: LearningHtmlArtifact,
        *,
        caption: str,
    ) -> tuple[int, int]:
        """Deliver one validated interactive HTML file and return ``(chat_id, message_id)``."""
        chat: Any = user_id
        get_input_chat = getattr(event, "get_input_chat", None)
        if callable(get_input_chat):
            try:
                resolved = await get_input_chat()
                if resolved is not None:
                    chat = resolved
            except Exception:
                chat = user_id
        stream = io.BytesIO(artifact.content)
        stream.name = artifact.filename
        async with self._upload_semaphore:
            sent = await asyncio.wait_for(
                self.client.send_file(
                    chat,
                    stream,
                    caption=caption[:TELEGRAM_CAPTION_LIMIT],
                    force_document=True,
                    mime_type="text/html",
                    attributes=[DocumentAttributeFilename(artifact.filename)],
                ),
                timeout=self.settings.upload_timeout,
            )
        raw_chat_id = getattr(sent, "chat_id", None)
        if not isinstance(raw_chat_id, int) or isinstance(raw_chat_id, bool):
            raw_chat_id = getattr(event, "chat_id", None)
        sent_chat_id = (
            raw_chat_id
            if isinstance(raw_chat_id, int) and not isinstance(raw_chat_id, bool)
            else int(user_id)
        )
        raw_msg_id = getattr(sent, "id", None)
        sent_message_id = (
            raw_msg_id
            if isinstance(raw_msg_id, int) and not isinstance(raw_msg_id, bool)
            else 0
        )
        return sent_chat_id, sent_message_id

    async def _generate_chunks(
        self,
        *,
        kind: str,
        chunks: list[DocChunk],
        counts: list[int],
        placeholder: Any,
        stage: str = "در حال تولید محتوا",
        requested_count: int | None = None,
        data_sensitivity: str = SENSITIVITY_UNKNOWN,
    ) -> tuple[list[dict[str, Any]], int]:
        job_requested_count = max(
            1,
            int(requested_count if requested_count is not None else sum(counts)),
        )
        job_task = (
            "FLASHCARD_GENERATION"
            if kind == "deck"
            else "QUIZ_20" if job_requested_count >= 20 else "QUIZ_GENERATION"
        )
        schema = FLASHCARD_SCHEMA if kind == "deck" else QUIZ_SCHEMA
        concurrency = (
            1
            if kind == "deck"
            else min(8, max(1, int(self.settings.learning_chunk_concurrency)))
        )
        prior_job_items: list[dict[str, Any]] = []
        active = [index for index, count in enumerate(counts) if count > 0]
        total = len(active)
        state = {"done": 0, "failed": 0}
        fractions = dict.fromkeys(active, 0.0)
        progress_changed = asyncio.Event()
        finished = False
        session = self._http_session()  # type: ignore[attr-defined]
        edit_timeout = self.deps.learning_progress_edit_timeout_seconds

        async def report_progress() -> None:
            while True:
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
            prior_items: list[dict[str, Any]] | None = None,
        ) -> list[dict[str, Any]]:
            allowed = tuple(
                dict.fromkeys(
                    " ".join(str(locator).split())[:120]
                    for locator in (target_chunk.sources or (target_chunk.locator,))
                    if locator
                )
            )
            fallback_locator = allowed[0] if allowed else ""
            text_key = await run_file_io(source_key, target_chunk.text)
            items: list[dict[str, Any]] = []
            batch_cap = (
                10
                if kind == "deck" and int(target_count) >= 40
                else self.MAX_ITEMS_PER_CALL
            )
            planned_batches = max(1, math.ceil(int(target_count) / batch_cap))
            max_attempts = planned_batches + 1
            had_shortfall = False

            for attempt in range(max_attempts):
                missing = int(target_count) - len(items)
                if missing <= 0:
                    break
                batch = min(missing, batch_cap)
                system = (
                    flashcard_system_prompt(batch, sources=allowed)
                    if kind == "deck"
                    else quiz_system_prompt(batch, sources=allowed)
                )
                user_prompt = learning_user_prompt(
                    kind, batch, allowed, target_chunk.text[:max_input_chars]
                )
                if had_shortfall:
                    user_prompt += (
                        "\nخروجی قبلی نامعتبر، ناقص یا بدون منبع مجاز بود؛ "
                        "همهٔ فیلدهای لازم و یک منبع دقیق از فهرست را وارد کن."
                    )
                previous = [*(prior_items or [])[-25:], *items[-25:]]
                if previous:
                    prior_stems = [
                        str(it.get(stem_key) or "").strip()[:90]
                        for it in previous
                        if str(it.get(stem_key) or "").strip()
                    ]
                    if prior_stems:
                        user_prompt += (
                            "\nموردهای زیر قبلاً ساخته شده‌اند؛ هیچ‌کدام را تکرار یا بازنویسی نکن:\n- "
                            + "\n- ".join(prior_stems)
                        )
                try:
                    payload = await self.deps.complete_json(
                        session,
                        self.settings,
                        system=system,
                        user=user_prompt,
                        schema=schema,
                        task=job_task,
                        data_sensitivity=data_sensitivity,
                        max_output_tokens=min(
                            int(self.settings.learning_max_output_tokens),
                            (2000 if batch <= 10 else 4000)
                            if kind == "deck"
                            else (
                                2500
                                if batch <= 5
                                else 5000 if batch <= 10 else 10000
                            ),
                        ),
                        wait_for_capacity=False,
                    )

                    def validate_and_merge(
                        payload: dict[str, Any] = payload,
                        batch: int = batch,
                        items: list[dict[str, Any]] = items,
                    ) -> list[dict[str, Any]]:
                        if kind == "deck":
                            parsed = parse_flashcard_payload(
                                payload, batch, require_source=True
                            )
                        else:
                            parsed = parse_quiz_payload(
                                payload,
                                batch,
                                require_difficulty=True,
                                require_source=True,
                            )
                        anchored = anchor_items(
                            parsed,
                            allowed,
                            fallback_locator,
                            target_chunk.text,
                            text_key=text_key,
                        )
                        candidates = [*items, *anchored]
                        merged_items = dedupe(candidates)
                        if kind != "deck":
                            _log_quiz_dedupe_diagnostics(
                                len(candidates), len(merged_items), "chunk_batch"
                            )
                        return merged_items

                    prev_count = len(items)
                    merged = await run_file_io(validate_and_merge)
                    if len(merged) > prev_count:
                        items = merged[: int(target_count)]
                    if len(items) >= target_count:
                        break
                    if len(items) - prev_count < batch:
                        had_shortfall = True
                except asyncio.CancelledError:
                    raise
                except (LLMContextOverflowError, LLMOutputBudgetError):
                    if split_depth < 2 and len(target_chunk.text) >= 1_600:
                        sub_raw = await run_file_io(
                            chunk_text,
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
                                missing,
                                [
                                    topic_importance_weight(sc.text)
                                    for sc in sub_chunks
                                ],
                            )
                            for sc, sc_count in zip(
                                sub_chunks, sub_counts, strict=True
                            ):
                                if sc_count <= 0:
                                    continue
                                sub_items = await _generate_piece(
                                    sc,
                                    sc_count,
                                    chunk_idx=chunk_idx,
                                    split_depth=split_depth + 1,
                                    prior_items=prior_items,
                                )
                                items = (
                                    await run_file_io(dedupe, [*items, *sub_items])
                                )[: int(target_count)]
                            break
                    had_shortfall = True
                    logger.warning(
                        "chunk budget exceeded kind=%s chunk=%s attempt=%s",
                        kind,
                        chunk_idx,
                        attempt + 1,
                        exc_info=True,
                    )
                except Exception:
                    had_shortfall = True
                    logger.warning(
                        "chunk generation failed kind=%s chunk=%s attempt=%s",
                        kind,
                        chunk_idx,
                        attempt + 1,
                        exc_info=True,
                    )
                finally:
                    fractions[chunk_idx] = max(
                        fractions[chunk_idx],
                        min(0.9, (attempt + 1) / (max_attempts + 1)),
                    )
                    progress_changed.set()
            return items

        async def one(index: int) -> list[dict[str, Any]]:
            chunk = chunks[index]
            count = counts[index]
            items = await _generate_piece(
                chunk,
                count,
                chunk_idx=index,
                prior_items=prior_job_items if kind == "deck" else None,
            )
            if kind == "deck":
                prior_job_items.extend(items)
            if len(items) < count:
                state["failed"] += 1
            fractions[index] = 0.0
            state["done"] += 1
            progress_changed.set()
            return items

        progress_task = asyncio.create_task(report_progress())
        results: list[list[dict[str, Any]]] = [[] for _ in active]
        next_index = iter(enumerate(active))

        async def worker() -> None:
            for position, index in next_index:
                results[position] = await one(index)

        tasks = [
            asyncio.create_task(worker()) for _ in range(min(concurrency, total))
        ]
        try:
            await asyncio.gather(*tasks)
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
        """Generate quiz or flashcards directly as an interactive HTML file (no curation)."""
        chunks = [chunk for chunk in source["chunks"] if chunk.text.strip()]
        if not chunks:
            await event.respond("متنی برای ساخت محتوا پیدا نشد. فایل دیگری بفرستید.")
            return
        requested_count = max(1, int(wanted))
        data_sensitivity = await run_file_io(
            _resolve_learning_sensitivity,
            self.settings,
            source.get("sensitivity_text"),
            source["chunks"],
        )
        logger.info(
            "learning source sensitivity resolved kind=%s sensitivity=%s chunks=%d",
            kind,
            data_sensitivity,
            len(chunks),
            extra={
                "event": "learning_source_sensitivity",
                "learning_kind": kind,
                "sensitivity": data_sensitivity,
                "chunk_count": len(chunks),
            },
        )
        await self._ack(event)
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
        committed = False
        placeholder = None
        label = "فلش‌کارت" if kind == "deck" else "سؤال"
        stage = f"آماده‌سازی {label}"
        try:
            counts = distribute_counts(
                requested_count,
                [topic_importance_weight(chunk.text) for chunk in chunks],
            )
            progress = await WorkProgress.start(
                event,
                headline="🧠 در حال ساخت فایل تعاملی",
                done_headline="✅ فایل تعاملی آماده شد",
                stage=stage,
                detail=(
                    "درخواست شما ثبت شد؛ "
                    f"{to_persian_digits(requested_count)} مورد در "
                    f"{to_persian_digits(len(chunks))} بخش ساخته می‌شود."
                ),
            )
            placeholder = progress
            async with self._learning_global_semaphore:
                items, failures = await self._generate_chunks(
                    kind=kind,
                    chunks=chunks,
                    counts=counts,
                    placeholder=progress,
                    stage=texts.LEARNING_PRODUCING.format(label=label),
                    requested_count=requested_count,
                    data_sensitivity=data_sensitivity,
                )
            generated_count = len(items)
            items = await run_file_io(
                dedupe_cards if kind == "deck" else dedupe_questions, items
            )
            if kind != "deck":
                _log_quiz_dedupe_diagnostics(generated_count, len(items), "job_merge")

            minimum = MIN_VALID_CARDS if kind == "deck" else MIN_VALID_QUESTIONS
            if len(items) < minimum:
                raise GenerationShortfallError(
                    kind=kind,
                    produced=len(items),
                    required=minimum,
                    requested=requested_count,
                    failures=failures,
                    chunks=len(chunks),
                )
            final_items = items[:requested_count]
            clean_title = str(title)[:200]
            source_title = str(source.get("title") or "")

            await progress.update(
                90,
                "ساخت فایل تعاملی HTML",
                f"{to_persian_digits(len(final_items))} {label} در حال بسته‌بندی در فایل آفلاین است…",
            )
            if kind == "deck":
                artifact = await run_file_io(
                    render_flashcards_html,
                    clean_title,
                    final_items,
                    source_title=source_title,
                )
                await run_file_io(
                    validate_learning_html_artifact,
                    artifact.content,
                    expected_kind="flashcards",
                )
                caption = (
                    f"🃏 {clean_title}\n"
                    f"{to_persian_digits(len(final_items))} فلش‌کارت تعاملی آفلاین "
                    "(روی فایل بزنید تا در مرورگر باز شود)"
                )
            else:
                artifact = await run_file_io(
                    render_quiz_html,
                    clean_title,
                    final_items,
                    source_title=source_title,
                )
                await run_file_io(
                    validate_learning_html_artifact,
                    artifact.content,
                    expected_kind="quiz",
                )
                caption = (
                    f"🧪 {clean_title}\n"
                    f"{to_persian_digits(len(final_items))} سؤال چهارگزینه‌ای تعاملی آفلاین "
                    "(روی فایل بزنید تا در مرورگر باز شود)"
                )

            await progress.update(
                95,
                "ارسال فایل تعاملی",
                "فایل HTML در حال ارسال در چت و ذخیره در کتابخانه است…",
            )
            sent_chat_id, sent_message_id = await self._send_learning_html(
                event, user_id, artifact, caption=caption
            )

            refund = (
                (cost * (requested_count - len(final_items)) + requested_count - 1)
                // requested_count
                if charged and len(final_items) < requested_count
                else 0
            )
            current_task = asyncio.current_task()
            if (
                current_task is not None
                and self._learning_tasks.get(user_id) is current_task
            ):
                self._learning_tasks.pop(user_id, None)
            if current_task is not None:
                self._learning_commit_tasks.add(current_task)
            try:
                source_item_id = int(source["item_id"])
                if await self.db.library_item(user_id, source_item_id) is None:
                    # Source item disappeared mid-run; persist a lightweight internal
                    # anchor so FK-backed quiz_sessions / flashcard_decks never fail
                    # after the user already received the HTML file.
                    source_item_id = await self.db.library_add(
                        user_id,
                        "booklet",
                        0,
                        0,
                        title=clean_title,
                        delivered=False,
                        artifact_kind="learning_source",
                    )

                if kind == "deck":
                    await self.db.deck_create_with_cards(
                        user_id,
                        source_item_id,
                        clean_title,
                        final_items,
                        free_claim_id=claim_id if free else None,
                        paid_charge_id=claim_id if charged else None,
                        refund_seconds_value=refund,
                    )
                    library_title = (
                        f"{short_title(source_title or clean_title, fallback=FALLBACK_BOOKLET)} "
                        f"({to_persian_digits(len(final_items))} کارت)"
                    )
                    await self.db.library_record_delivered_file(
                        user_id,
                        "flashcards",
                        title=library_title,
                        chat_id=sent_chat_id,
                        message_id=sent_message_id,
                        artifact_kind="flashcards_html",
                        filename=artifact.filename,
                        content_type="text/html; charset=utf-8",
                        content=artifact.content,
                    )
                else:
                    await self.db.quiz_create_session(
                        user_id,
                        source_item_id,
                        final_items,
                        status="done",
                        free_claim_id=claim_id if free else None,
                        paid_charge_id=claim_id if charged else None,
                        refund_seconds_value=refund,
                    )
                    library_title = (
                        f"{short_title(source_title or clean_title, fallback=FALLBACK_BOOKLET)} "
                        f"({to_persian_digits(len(final_items))} سؤال)"
                    )
                    await self.db.library_record_delivered_file(
                        user_id,
                        "quiz",
                        title=library_title,
                        chat_id=sent_chat_id,
                        message_id=sent_message_id,
                        artifact_kind="quiz_html",
                        filename=artifact.filename,
                        content_type="text/html; charset=utf-8",
                        content=artifact.content,
                    )
                committed = True
                await self.db.library_prune(user_id, self.settings.library_max_items)
            finally:
                if current_task is not None:
                    self._learning_commit_tasks.discard(current_task)

            lines = [
                texts.LEARNING_HTML_SENT_DECK.format(
                    count=to_persian_digits(len(final_items))
                )
                if kind == "deck"
                else texts.LEARNING_HTML_SENT_QUIZ.format(
                    count=to_persian_digits(len(final_items))
                )
            ]
            if failures:
                lines.append(
                    f"⚠️ {to_persian_digits(failures)} بخش به‌طور کامل پردازش نشد."
                )
            if len(final_items) < requested_count:
                lines.append(
                    f"ℹ️ از {to_persian_digits(requested_count)} مورد درخواست‌شده، "
                    f"{to_persian_digits(len(final_items))} مورد معتبر و غیرتکراری تولید شد."
                )
            skipped = sum(count == 0 for count in counts)
            if skipped:
                lines.append(
                    f"ℹ️ {to_persian_digits(skipped)} بخش به‌دلیل اندازهٔ "
                    "انتخاب‌شده بررسی نشد؛ برای پوشش بیشتر اندازهٔ بزرگ‌تر را انتخاب کنید."
                )
            if refund:
                lines.append(
                    texts.LEARNING_PRORATA_REFUND.format(
                        duration=format_seconds_as_credits(refund)
                    )
                )
            done_buttons = [
                [
                    Button.inline(
                        texts.BTN_LIB_FLASHCARDS_SHORT
                        if kind == "deck"
                        else texts.BTN_LIB_QUIZZES_SHORT,
                        b"lib:cat:flashcards:0"
                        if kind == "deck"
                        else b"lib:cat:quiz:0",
                    ),
                    Button.inline(texts.BTN_LIBRARY, b"library"),
                ],
                [Button.inline(texts.BTN_BACK_MAIN, b"menu:main")],
            ]
            done_text = completed_progress_text(
                "\n".join(lines), headline="✅ فایل تعاملی آماده شد"
            )
            if progress.message is not None:
                try:
                    await progress.message.edit(done_text, buttons=done_buttons)
                except Exception:
                    await progress.complete("\n".join(lines))
            else:
                await progress.complete("\n".join(lines))
        except BaseException as exc:
            if committed:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning(
                    "post-commit learning status update failed user=%s kind=%s: %s",
                    user_id,
                    kind,
                    exc,
                )
                return
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
                        headline="❌ ساخت فایل تعاملی ناموفق بود.",
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
            if isinstance(exc, sqlite3.Error):
                return LEARNING_GENERATION_FAILED
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

    async def _quiz_start_screen(
        self, event: Any, user_id: int, item_id: int
    ) -> None:
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._show_document_generate_screen(  # type: ignore[attr-defined]
                event, user_id, int(document["id"]), "quiz"
            )
            return
        if not item.get("content_text"):
            await self._edit(
                event,
                "🧪 آزمون تعاملی HTML\n\n" + LEARNING_LEGACY_NOTICE,
                [[Button.inline("↩️ بازگشت", b"library")]],
            )
            return
        if not self.settings.note_configured:
            await self._edit(
                event,
                texts.LEARNING_GENERATION_UNAVAILABLE,
                [[await self._learning_back_button_for_item(user_id, item_id)]],
            )
            return
        per_question = self.settings.quiz_cost_seconds_per_question
        free = await self._free_try_available(user_id, "quiz")
        buttons: list[list] = []
        for _code, label, _cards, questions in SIZE_PRESETS:
            cost_label = (
                texts.LEARNING_FREE_CHOICE_LABEL
                if free
                else format_seconds_as_credits(questions * per_question)
            )
            buttons.append(
                [
                    Button.inline(
                        button_label(
                            f"{label} — {to_persian_digits(questions)} سؤال — {cost_label}"
                        ),
                        f"quiz:go:{item_id}:{questions}".encode("ascii"),
                    )
                ]
            )
        buttons.append([await self._learning_back_button_for_item(user_id, item_id)])
        badge = ""
        if free:
            badge = f"\n\n{FREE_TRY_BADGE} — نخستین آزمون رایگان است؛ اندازه را انتخاب کنید."
        await self._edit(
            event,
            "🧪 آزمون تعاملی HTML از جزوه\n\n" + QUIZ_START_GUIDANCE + badge,
            buttons,
        )

    async def _quiz_generate(
        self, event: Any, user_id: int, item_id: int, count: int
    ) -> None:
        if count not in QUIZ_COUNTS:
            await self._ack(event, "تعداد سؤال نامعتبر است.", alert=True)
            return
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._generate_from_document(  # type: ignore[attr-defined]
                event, user_id, int(document["id"]), "quiz", str(count)
            )
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        content = item.get("content_text")
        if not content:
            await self._ack(event, LEARNING_LEGACY_NOTICE, alert=True)
            return
        if not self.settings.note_configured:
            await event.respond(texts.LEARNING_GENERATION_UNAVAILABLE)
            return
        if user_id in self._learning_in_flight:
            await self._ack(event, LEARNING_BUSY_ALERT, alert=True)
            return
        if self._shutting_down:
            await self._ack(
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
        if reason == "rate":
            await event.respond(
                "تعداد آزمون‌های شما در یک ساعت اخیر به حداکثر مجاز "
                "رسیده است؛ کمی بعد دوباره تلاش کنید."
            )
            return
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None:
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        await self._generate_learning(
            event,
            user_id=user_id,
            kind="quiz",
            source={
                "item_id": int(item_id),
                "doc_id": None,
                "title": self._item_display_title(item),
                "chunks": await run_file_io(self._learning_chunks, content),
                "sensitivity_text": content,
            },
            wanted=int(count),
            cost=int(count) * self.settings.quiz_cost_seconds_per_question,
            note=f"quiz-pending:{item_id}",
            title=short_title(
                self._item_display_title(item), fallback=FALLBACK_BOOKLET
            )
            + " — آزمون",
        )

    @staticmethod
    def _deck_stale_alert() -> str:
        return "این فلش‌کارت دیگر معتبر نیست."

    @staticmethod
    def _library_icon(kind: str) -> str:
        return {
            "booklet": "📖",
            "quiz": "🧪",
            "flashcards": "🃏",
            "transcript": "📝",
            "office": "🗂",
            "document": "📄",
        }.get(kind, "📄")

    def _item_display_title(self, item: dict[str, Any]) -> str:
        kind = str(item.get("kind") or "")
        fallback = {
            "booklet": FALLBACK_BOOKLET,
            "quiz": "آزمون تعاملی",
            "flashcards": "فلش‌کارت تعاملی",
            "office": FALLBACK_OFFICE,
            "transcript": "متن گفتار",
        }.get(kind, FALLBACK_DOCUMENT)
        raw = " ".join(str(item.get("title") or "").split())
        raw = raw.lstrip("📖📝🗂📄🧪🃏✅ ").strip()
        return short_title(raw, fallback=fallback)

    async def _flashcard_start_screen(
        self, event: Any, user_id: int, item_id: int
    ) -> None:
        item = await self.db.library_item_detail(user_id, item_id)
        if item is None or item.get("kind") != "booklet":
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        document = await self.db.document_by_library_item(user_id, item_id)
        if document:
            await self._show_document_generate_screen(  # type: ignore[attr-defined]
                event, user_id, int(document["id"]), "deck"
            )
            return
        if not item.get("content_text"):
            await self._edit(
                event,
                "🃏 فلش‌کارت تعاملی HTML\n\n" + LEARNING_LEGACY_NOTICE,
                [[Button.inline("↩️ بازگشت", b"library")]],
            )
            return
        if not self.settings.note_configured:
            await self._edit(
                event,
                texts.LEARNING_GENERATION_UNAVAILABLE,
                [[await self._learning_back_button_for_item(user_id, item_id)]],
            )
            return
        size = self.settings.flashcard_ai_deck_size
        cost = self.settings.flashcard_ai_cost_seconds
        free = await self._free_try_available(user_id, "deck")
        regular_cost = format_seconds_as_credits(cost)
        text = (
            "🃏 ساخت فلش‌کارت تعاملی HTML از این جزوه\n\n"
            "فایل HTML آفلاین شامل کارت‌گردان، حالت بلدم/مرور و جستجو به‌صورت مستقیم برایتان ارسال می‌شود.\n"
            + (
                f"هزینهٔ معمول: {regular_cost}؛ نخستین مجموعه رایگان است."
                if free
                else f"هزینهٔ پایه: {regular_cost}"
            )
        )
        badge = ""
        if free:
            badge = f"\n\n{FREE_TRY_BADGE} — این بار بدون کسر اعتبار ساخته می‌شود."
        buttons: list[list] = []
        for code, label, cards, _questions in SIZE_PRESETS:
            buttons.append(
                [
                    Button.inline(
                        button_label(
                            f"{label} — {to_persian_digits(cards)} کارت — "
                            + (
                                texts.LEARNING_FREE_CHOICE_LABEL
                                if free
                                else format_seconds_as_credits(
                                    self._preset_cost(cost, cards, size)
                                )
                            )
                        ),
                        f"fc:go:{item_id}:{code}".encode("ascii"),
                    )
                ]
            )
        buttons.append([await self._learning_back_button_for_item(user_id, item_id)])
        await self._edit(
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
            await self._ack(event, "این فایل دیگر در دسترس نیست.", alert=True)
            return
        content = item.get("content_text")
        if not content:
            await self._ack(event, LEARNING_LEGACY_NOTICE, alert=True)
            return
        if not self.settings.note_configured:
            await event.respond(texts.LEARNING_GENERATION_UNAVAILABLE)
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
            await self._ack(event, LEARNING_BUSY_ALERT, alert=True)
            return
        if self._shutting_down:
            await self._ack(
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
                "chunks": await run_file_io(self._learning_chunks, content),
                "sensitivity_text": content,
            },
            wanted=size,
            cost=cost,
            note=f"fc-pending:{item_id}",
            title=short_title(item_title, fallback=FALLBACK_BOOKLET) + " — فلش‌کارت",
        )

    async def _show_flashcard_hub(self, event: Any, *, page: int = 0) -> None:
        """Route legacy flashcard-hub buttons straight to the categorized Flashcards tab."""
        await self._show_library_category(event, "flashcards", page=page)  # type: ignore[attr-defined]

    async def _show_booklet_picker(
        self, event: Any, *, kind: str = "deck", page: int = 0
    ) -> None:
        user_id = event.sender_id
        if user_id is None:
            return
        back_cb = b"lib:cat:flashcards:0" if kind == "deck" else b"lib:cat:quiz:0"
        if not self.settings.note_configured:
            await self._edit(
                event,
                texts.LEARNING_GENERATION_UNAVAILABLE,
                [[Button.inline("↩️ بازگشت", back_cb)]],
            )
            return
        page_size = 6
        count = await self.db.library_source_booklets_count(user_id)
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        items = await self.db.library_source_booklets(
            user_id, limit=page_size, offset=page * page_size
        )
        action_prefix = "fc:ai" if kind == "deck" else "quiz:new"
        pick_prefix = "fc:pick" if kind == "deck" else "quiz:pick"
        buttons: list[list] = []
        if not items:
            text = (
                "🤖 ساخت فلش‌کارت از جزوه\n\n"
                if kind == "deck"
                else "🤖 ساخت آزمون از جزوه\n\n"
            ) + "هنوز جزوه‌ای از ربات دریافت نکرده‌اید که متن آن برای تولید موجود باشد."
        else:
            text = (
                "یک جزوه را برای ساخت فلش‌کارت تعاملی HTML انتخاب کنید:"
                if kind == "deck"
                else "یک جزوه را برای ساخت آزمون تعاملی HTML انتخاب کنید:"
            )
            for item in items:
                title = self._item_display_title(item)
                buttons.append(
                    [
                        Button.inline(
                            button_label(f"📖 {title}"),
                            f"{action_prefix}:{item['item_id']}".encode("ascii"),
                        )
                    ]
                )
            nav = []
            if page > 0:
                nav.append(
                    Button.inline(
                        "◀️ قبلی", f"{pick_prefix}:{page - 1}".encode("ascii")
                    )
                )
            if (page + 1) * page_size < count:
                nav.append(
                    Button.inline(
                        "بعدی ▶️", f"{pick_prefix}:{page + 1}".encode("ascii")
                    )
                )
            if nav:
                buttons.append(nav)
        buttons.append([Button.inline("↩️ بازگشت", back_cb)])
        await self._edit(event, text, buttons)

    async def _start_manual_deck(self, event: Any, user_id: int) -> None:
        if (
            await self.db.deck_count(user_id)
            >= self.settings.flashcard_max_decks_per_user
        ):
            await event.respond(
                "به حداکثر تعداد مجموعه‌های فلش‌کارت رسیده‌اید؛ یک مجموعهٔ قدیمی را حذف کنید."
            )
            return
        deck_id = await self.db.deck_create(
            user_id, None, "فلش‌کارت دستی من", "manual"
        )
        self._pending_flashcard[user_id] = (deck_id, "front")
        self._pending_flashcard_front.pop(user_id, None)
        await event.respond(
            FLASHCARD_ASK_FRONT,
            buttons=[[Button.inline("✅ پایان و ساخت فایل HTML", b"fc:man:done")]],
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
        end_button = [[Button.inline("✅ پایان و ساخت فایل HTML", b"fc:man:done")]]
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
                deck_id,
                front,
                text[:MAX_MANUAL_BACK_CHARS],
                max_cards=self.settings.flashcard_max_cards_per_deck,
            )
        except FlashcardLimitError:
            self._pending_flashcard.pop(user_id, None)
            await event.reply(texts.FLASHCARD_CARD_LIMIT)
            return
        self._pending_flashcard[user_id] = (deck_id, "front")
        await event.reply(
            f"کارت اضافه شد ({to_persian_digits(count)} کارت). "
            "پرسش کارت بعدی را بفرستید یا «پایان و ساخت فایل HTML» را بزنید.",
            buttons=end_button,
        )

    async def _finish_manual_deck(
        self, event: Any, user_id: int, deck_id: int
    ) -> None:
        """Export a completed manual flashcard deck as an interactive HTML file."""
        deck = await self.db.deck_get(deck_id, user_id)
        if deck is None:
            await self._show_flashcard_hub(event)
            return
        cards = await self.db.deck_cards(deck_id)
        if not cards:
            await self.db.deck_delete(deck_id, user_id)
            await event.respond(
                "کارتی اضافه نشد؛ ساخت فلش‌کارت دستی بسته شد.",
                buttons=[[Button.inline(texts.BTN_LIB_FLASHCARDS_SHORT, b"lib:cat:flashcards:0")]],
            )
            return
        first_front = str(cards[0].get("front") or "")
        deck_title = short_title(first_front, fallback="فلش‌کارت دستی من", max_words=4)
        try:
            artifact = await run_file_io(
                render_flashcards_html,
                deck_title,
                cards,
                source_title="ساخته‌شده به‌صورت دستی",
            )
            await run_file_io(
                validate_learning_html_artifact,
                artifact.content,
                expected_kind="flashcards",
            )
            caption = (
                f"🃏 {deck_title}\n"
                f"{to_persian_digits(len(cards))} فلش‌کارت دستی تعاملی آفلاین "
                "(روی فایل بزنید تا در مرورگر باز شود)"
            )
            sent_chat_id, sent_message_id = await self._send_learning_html(
                event, user_id, artifact, caption=caption
            )
            library_title = f"{deck_title} ({to_persian_digits(len(cards))} کارت دستی)"
            await self.db.library_record_delivered_file(
                user_id,
                "flashcards",
                title=library_title,
                chat_id=sent_chat_id,
                message_id=sent_message_id,
                artifact_kind="flashcards_html",
                filename=artifact.filename,
                content_type="text/html; charset=utf-8",
                content=artifact.content,
            )
            await self.db.library_prune(user_id, self.settings.library_max_items)
        except Exception:
            logger.exception("manual flashcard HTML export failed user=%s", user_id)
            await event.respond(
                "❌ ساخت یا ارسال فایل HTML فلش‌کارت دستی ناموفق بود؛ دوباره تلاش کنید.",
                buttons=[[Button.inline(texts.BTN_LIB_FLASHCARDS_SHORT, b"lib:cat:flashcards:0")]],
            )
            return
        await event.respond(
            texts.LEARNING_MANUAL_HTML_SENT.format(
                count=to_persian_digits(len(cards))
            ),
            buttons=[
                [
                    Button.inline(
                        texts.BTN_LIB_FLASHCARDS_SHORT, b"lib:cat:flashcards:0"
                    ),
                    Button.inline(texts.BTN_LIBRARY, b"library"),
                ]
            ],
        )

    async def _route_flashcard_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "hub":
            page = _to_int(parts[2]) if len(parts) == 3 else 0
            await self._show_flashcard_hub(event, page=max(0, page or 0))
            return
        if action == "pick":
            page = _to_int(parts[2]) if len(parts) == 3 else 0
            await self._show_booklet_picker(event, kind="deck", page=max(0, page or 0))
            return
        if action == "man" and len(parts) == 2:
            await self._start_manual_deck(event, user_id)
            return
        if action == "man" and len(parts) == 3 and parts[2] == "done":
            state = self._pending_flashcard.pop(user_id, None)
            self._pending_flashcard_front.pop(user_id, None)
            await self._ack(event)
            if state is None:
                await self._show_flashcard_hub(event)
            else:
                await self._finish_manual_deck(event, user_id, state[0])
            return
        if action == "ai" and len(parts) == 3:
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)
                return
            await self._flashcard_start_screen(event, user_id, item_id)
            return
        if action == "go" and len(parts) in (3, 4):
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, self._deck_stale_alert(), alert=True)
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
        await self._ack(event, self._deck_stale_alert(), alert=True)

    async def _route_quiz_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "pick":
            page = _to_int(parts[2]) if len(parts) == 3 else 0
            await self._show_booklet_picker(event, kind="quiz", page=max(0, page or 0))
            return
        if action == "new" and len(parts) == 3:
            item_id = _to_int(parts[2])
            if item_id is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)
                return
            await self._quiz_start_screen(event, user_id, item_id)
            return
        if action == "go" and len(parts) == 4:
            item_id = _to_int(parts[2])
            count = _to_int(parts[3])
            if item_id is None or count is None:
                await self._ack(event, LEARNING_STALE_ALERT, alert=True)
                return
            if not await self._require_membership(
                event, user_id, kind="callback", callback_data=data
            ):
                return
            await self._quiz_generate(event, user_id, item_id, count)
            return
        await self._ack(event, LEARNING_STALE_ALERT, alert=True)
