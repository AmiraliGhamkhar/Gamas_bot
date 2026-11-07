"""Document extraction/OCR library and Free Office workspace workflows (SRP / DIP).

Encapsulates PDF/Word/PowerPoint/image document ingestion, i2OCR integration,
owner-scoped document exports & source-grounded booklets, and the ephemeral
Free Office multi-message text composer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import replace
from datetime import datetime
import logging
from pathlib import Path
import secrets
import shutil
import tempfile
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from telethon import Button
from telethon.tl.custom import Message

from . import texts
from .billing import format_duration
from .bot_ui import (
    DEFAULT_INTAKE_SUFFIX,
    DOC_EXTRACTION_STOPPED,
    DOC_GENERATE_CARDS,
    DOC_GENERATE_QUESTIONS,
    DOC_OCR_FAILED,
    FREE_TRY_BADGE,
    LEARNING_BUSY_ALERT,
    MAX_OFFICE_OUTPUT_BYTES,
    MAX_OFFICE_SESSIONS,
    OFFICE_UPLOAD_STOPPED,
    QUIZ_COUNTS,
    TELEGRAM_CAPTION_LIMIT,
)
from .doc_extract import (
    SOURCE_TYPE_LABELS_FA,
    DocExtractError,
    EmptyPresentationTextError,
    ExtractedDoc,
    ScannedPdfError,
    chunk_sections,
    chunks_from_json,
    chunks_to_json,
    document_title,
    preview_text,
    sections_from_slides,
    sha256_file,
)
from .docx_export import (
    OFFICE_HEADING_OFFSET,
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    resolve_design,
    resolve_fonts,
)
from .fileio import run_file_io, run_file_io_resource
from .intake import _presentation_metadata, _to_int
from .jobs import Job
from .learning import MAX_MODEL_INPUT_CHARS, SIZE_PRESETS, bound_content_text, preset_cards
from .media import MediaToolError, convert_to_pptx
from .notes import StructuringError
from .ocr import I2OCRClient, OcrError, extract_text_problem
from .office import (
    FORMAT_LABELS,
    OFFICE_FORMATS,
    OfficeError,
    OfficeSession,
    build_office_document,
    build_office_pptx,
    build_office_xlsx,
    fallback_slides,
    office_document_tables,
    office_filename,
    office_title_from_filename,
    read_text_file,
)
from .office_ai import MAX_DECK_CHUNK_CHARS, infer_table, plan_deck
from .office_notes import (
    MAX_OFFICE_NOTE_SOURCE_CHARS,
    generate_compact_notes,
    generate_full_booklet,
    office_source_chunks,
)
from .pdf_export import build_office_pdf
from .progress import WorkProgress, completed_progress_text, failed_progress_text, to_persian_digits
from .telegram_text import button_label
from .telegram_transfer import UploadTooLargeError
from .textnorm import normalize_for_compare
from .titles import FALLBACK_BOOKLET, FALLBACK_DOCUMENT, FALLBACK_OFFICE, short_title

if TYPE_CHECKING:
    from .bot_deps import BotDependencies

logger = logging.getLogger("gamas_bot.bot.documents")


class DocumentOfficeWorkflowMixin:
    """Document ingestion/OCR/exports and Free Office draft sessions."""

    db: Any
    settings: Any
    client: Any
    deps: BotDependencies
    _shutting_down: bool
    _ocr_client: I2OCRClient | None
    _document_locks: MutableMapping[tuple[int, str], asyncio.Lock]
    _document_semaphore: asyncio.Semaphore

    async def _require_membership(
        self, event: Any, user_id: int, *, kind: str, callback_data: str = ""
    ) -> bool:
        """Provided by the composed bot; gates one value-delivery action."""
        raise NotImplementedError
    _office_sessions: dict[int, OfficeSession]
    _office_semaphore: asyncio.Semaphore
    _office_in_flight: set[int]
    _office_last_use: dict[int, float]
    _office_tasks: set[asyncio.Task[Any]]
    _intake_tasks: set[asyncio.Task[Any]]
    _learning_in_flight: set[int]
    _learning_tasks: dict[int, asyncio.Task[Any]]

    OFFICE_BUILD_PERCENT = 55
    OFFICE_DELIVERY_PERCENT = 40

    async def _document_library_item(self, user_id: int, document: dict) -> int:
        existing = document.get("library_item_id")
        if existing:
            return int(existing)
        content = await run_file_io(bound_content_text, str(document.get("text") or ""))
        return await self.db.document_ensure_library_item(
            user_id,
            int(document["id"]),
            content,
            title=short_title(
                str(document.get("title") or ""), fallback=FALLBACK_DOCUMENT
            ),
        )

    async def _save_document(
        self,
        user_id: int,
        *,
        source_type: str,
        title: str,
        file_msg_ref: str,
        sha256: str,
        page_count: int,
        text: str,
        chunks: Any,
        library_item_id: int | None = None,
    ) -> tuple[dict, bool]:
        document_id, created = await self.db.document_add(
            user_id,
            file_msg_ref=file_msg_ref,
            title=title,
            source_type=source_type,
            page_count=page_count,
            sha256=sha256,
            text=text,
            chunks_json=await run_file_io(chunks_to_json, chunks),
            library_item_id=library_item_id,
        )
        document = await self.db.document_get(user_id, document_id)
        if document is None:  # pragma: no cover - just written
            raise RuntimeError("document vanished after insert")
        return document, created

    def _ocr_client_instance(self) -> I2OCRClient:
        if self._ocr_client is None:
            self._ocr_client = I2OCRClient(max_bytes=int(self.settings.doc_max_bytes))
        return self._ocr_client

    def _ocr_extractor(
        self,
    ) -> Callable[[Path, Sequence[int]], Sequence[str | None]] | None:
        if not self.settings.ocr_enabled:
            return None
        settings = self.settings

        def run(path: Path, pages: Sequence[int]) -> list[str | None]:
            client = self._ocr_client_instance()
            recognised = client.extract_pages(
                path,
                pages,
                language=settings.ocr_language,
                budget_seconds=settings.ocr_timeout_seconds,
            )
            by_page = {page.number: page.text for page in recognised}
            return [by_page.get(number) for number in pages]

        return run

    def _document_lock(self, user_id: int, digest: str) -> asyncio.Lock:
        key = (user_id, digest)
        lock = self._document_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._document_locks[key] = lock
        return lock

    async def _ingest_document(
        self,
        user_id: int,
        path: Path,
        *,
        source_type: str,
        filename: str | None,
        file_msg_ref: str,
    ) -> tuple[dict, bool]:
        digest = await run_file_io(sha256_file, path)
        async with self._document_lock(user_id, digest):
            existing = await self.db.document_by_hash(user_id, digest)
            if existing is not None:
                await self.db.document_update_reference(
                    user_id, int(existing["id"]), file_msg_ref
                )
                await self._document_library_item(user_id, existing)
                return (
                    await self.db.document_get(user_id, int(existing["id"]))
                ) or existing, False
            extract_path = path
            if source_type == "pptx" and path.suffix.lower() in {
                ".ppt",
                ".pps",
                ".pot",
            }:
                try:
                    extract_path = await self._run_media_operation(  # type: ignore[attr-defined]
                        convert_to_pptx, path, path.parent / "converted", self.settings
                    )
                except MediaToolError as exc:
                    raise DocExtractError(
                        "تبدیل ارائهٔ قدیمی ناموفق بود؛ لطفاً نسخهٔ pptx را بفرستید."
                    ) from exc
            async with self._document_semaphore:
                extracted: ExtractedDoc = await run_file_io(
                    self.deps.extract_document,
                    extract_path,
                    source_type,
                    ocr=self._ocr_extractor(),
                    ocr_max_pages=int(self.settings.ocr_max_pages),
                    max_unpacked_bytes=int(self.settings.doc_unpacked_max_bytes),
                )
            text = await run_file_io(lambda: extracted.text)
            if not text.strip():
                raise DocExtractError(
                    "متنی از این سند استخراج نشد؛ OCR پشتیبانی نمی‌شود."
                )
            chunks = await run_file_io(
                chunk_sections, extracted.sections, max_chars=MAX_MODEL_INPUT_CHARS
            )
            title = document_title(filename, source_type)
            document, created = await self._save_document(
                user_id,
                source_type=source_type,
                title=title,
                file_msg_ref=file_msg_ref,
                sha256=digest,
                page_count=int(extracted.page_count),
                text=text,
                chunks=chunks,
            )
            if not created:
                await self.db.document_update_reference(
                    user_id, int(document["id"]), file_msg_ref
                )
            await self._document_library_item(user_id, document)
            document = (
                await self.db.document_get(user_id, int(document["id"]))
            ) or document
            logger.info(
                "document ingested user=%s type=%s pages=%s chars=%s chunks=%s created=%s",
                user_id,
                source_type,
                extracted.page_count,
                len(text),
                len(chunks),
                created,
            )
            return document, created

    def _document_card_text(self, document: dict) -> str:
        label = SOURCE_TYPE_LABELS_FA.get(str(document["source_type"]), "سند")
        unit = {"pdf": "صفحه", "docx": "بخش", "pptx": "اسلاید", "image": "صفحه"}.get(
            document["source_type"], "بخش"
        )
        lines = [
            f"📄 {document.get('title') or 'سند'}",
            f"نوع: {label} | {to_persian_digits(int(document.get('page_count') or 0))} {unit}",
            f"متن استخراج‌شده: {to_persian_digits(int(document.get('text_chars', len(str(document.get('text') or '')))))} نویسه",
        ]
        if self.settings.office_enabled:
            lines.append(
                "جزوه، مرور سریع و خروجی‌های Office رایگان‌اند و از اعتبار کم نمی‌کنند."
            )
        if document.get("source_type") == "pptx":
            lines.append(
                "پردازش صدای ارائه عملیات جداگانه‌ای است و فقط با انتخاب دکمهٔ صوت آغاز می‌شود."
            )
        lines.extend(("", "پیش‌نمایش متن:", preview_text(str(document.get("text_preview", document.get("text")) or ""))))
        return "\n".join(lines)

    def _document_buttons(self, document_id: int, source_type: str = "") -> list[list]:
        buttons: list[list] = []
        if self.settings.office_enabled:
            buttons.extend(
                [
                    [
                        Button.inline(
                            "📘 جزوهٔ کامل — Word + PDF",
                            f"doc:note:{document_id}:full".encode("ascii"),
                        ),
                        Button.inline(
                            "⚡ مرور سریع — Word + PDF",
                            f"doc:note:{document_id}:compact".encode("ascii"),
                        ),
                    ],
                    [
                        Button.inline(
                            "📦 خروجی از متن استخراج‌شده",
                            f"doc:export:{document_id}".encode("ascii"),
                        )
                    ],
                ]
            )
        buttons.extend(
            [
                [
                    Button.inline(
                        DOC_GENERATE_CARDS, f"doc:fc:{document_id}".encode("ascii")
                    ),
                    Button.inline(
                        DOC_GENERATE_QUESTIONS,
                        f"doc:quiz:{document_id}".encode("ascii"),
                    ),
                ],
                [
                    Button.inline(
                        "📥 دریافت فایل اصلی", f"doc:f:{document_id}".encode("ascii")
                    )
                ],
            ]
        )
        if source_type == "pptx":
            buttons.append(
                [
                    Button.inline(
                        "🎧 پردازش صدای ارائه",
                        f"doc:lecture:{document_id}".encode("ascii"),
                    )
                ]
            )
        buttons.append([Button.inline("📚 کتابخانه", b"lib:docs:0")])
        return buttons

    def _office_sweep_sessions(self, now: float | None = None) -> None:
        current = self.deps.monotonic() if now is None else now
        ttl = max(1, int(self.settings.office_session_ttl_minutes)) * 60
        for owner, session in list(self._office_sessions.items()):
            if current - float(session.updated_at or 0.0) > ttl:
                self._office_sessions.pop(owner, None)

    def _office_get_session(self, user_id: int) -> OfficeSession | None:
        self._office_sweep_sessions()
        return self._office_sessions.get(int(user_id))

    async def _office_session_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            self._office_sweep_sessions()

    def _office_new_session(
        self, user_id: int, message: Any = None
    ) -> OfficeSession | None:
        existing = self._office_get_session(user_id)
        if existing is not None:
            return existing
        if len(self._office_sessions) >= MAX_OFFICE_SESSIONS:
            return None
        session = OfficeSession(message=message, updated_at=self.deps.monotonic())
        self._office_sessions[int(user_id)] = session
        return session

    @staticmethod
    def _office_session_keyboard() -> list[list]:
        return [
            [
                Button.inline("➕ افزودن پیام", b"office:continue"),
                Button.inline("📦 انتخاب خروجی", b"office:formats"),
            ],
            [Button.inline("❌ پایان و پاک‌کردن متن", b"office:cancel")],
        ]

    @staticmethod
    def _office_format_keyboard() -> list[list]:
        return [
            [
                Button.inline("📝 Word", b"office:fmt:word"),
                Button.inline("📊 Excel", b"office:fmt:excel"),
            ],
            [
                Button.inline("📖 PowerPoint", b"office:fmt:ppt"),
                Button.inline("📕 PDF", b"office:fmt:pdf"),
            ],
            [Button.inline("↩️ افزودن متن", b"office:continue")],
            [Button.inline("❌ پایان و پاک‌کردن متن", b"office:cancel")],
        ]

    def _office_add_source(
        self,
        session: OfficeSession,
        content: str,
        *,
        filename: str | None = None,
        message: Any = None,
    ) -> None:
        if not content or not content.strip():
            raise OfficeError("متن خالی است؛ پیام یا فایل متنی دیگری بفرستید.")
        existing = session.text
        added_chars = len(content) + (1 if existing else 0)
        limit = int(self.settings.office_max_chars)
        if len(existing) + added_chars > limit:
            raise OfficeError(
                f"حجم متن از سقف {to_persian_digits(limit)} نویسه بیشتر می‌شود؛ "
                "متن را به چند سند کوچک‌تر تقسیم کنید. هیچ بخشی حذف نشده است."
            )
        session.add(content)
        if filename and not session.filename and not existing:
            safe_name = Path(filename).name[:255]
            session.filename = safe_name
            session.title = session.title or office_title_from_filename(safe_name)
        if message is not None:
            session.message = message
        session.updated_at = self.deps.monotonic()

    async def _office_start(self, event: Any, user_id: int) -> None:
        if not self.settings.office_enabled:
            await self._ack(  # type: ignore[attr-defined]
                event, "آفیس رایگان در تنظیمات سرور غیرفعال است.", alert=True
            )
            return
        session = self._office_new_session(user_id, getattr(event, "message", None))
        if session is None:
            await self._ack(  # type: ignore[attr-defined]
                event,
                "آفیس رایگان مشغول است؛ چند دقیقهٔ دیگر دوباره تلاش کنید.",
                alert=True,
            )
            return
        session.updated_at = self.deps.monotonic()
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event,
            "🗂 آفیس رایگان\n\n"
            "متن را اینجا بچسبانید یا چند پیام بفرستید؛ فایل‌های .txt، .md، .markdown، .csv و .tsv هم پذیرفته می‌شوند.\n"
            f"سقف هر سند {to_persian_digits(self.settings.office_max_chars)} نویسه است.\n"
            "پس از پایان، قالب Word، Excel، PowerPoint یا PDF را انتخاب کنید. "
            "هر خروجی جداگانه قابل ساخت است و از اعتبار یا سهمیهٔ رایگان کم نمی‌کند.",
            self._office_session_keyboard(),
        )

    async def _office_show_formats(self, event: Any, user_id: int) -> None:
        session = self._office_get_session(user_id)
        if session is None or not session.text.strip():
            await self._ack(  # type: ignore[attr-defined]
                event, "ابتدا متن یا یک فایل متنی به آفیس رایگان بفرستید.", alert=True
            )
            return
        session.updated_at = self.deps.monotonic()
        await self._edit_or_reply(  # type: ignore[attr-defined]
            event,
            "قالب خروجی را انتخاب کنید. متن کامل سند در خروجی می‌ماند؛ "
            "برای Excel/PowerPoint اگر محدودیت ساختاری وجود داشته باشد، پیام روشن می‌گیرید و محتوا کوتاه نمی‌شود.\n\n"
            f"عنوان: {session.title or 'از متن انتخاب می‌شود'} | "
            f"{to_persian_digits(len(session.text))} نویسه",
            self._office_format_keyboard(),
        )

    async def _office_add_message(
        self, event: Any, user_id: int, text: str
    ) -> None:
        session = self._office_get_session(user_id)
        if session is None:
            await event.reply(
                "زمان آفیس رایگان تمام شده است؛ از منو دوباره شروع کنید."
            )
            return
        try:
            self._office_add_source(
                session, text, message=getattr(event, "message", None)
            )
        except OfficeError as exc:
            await event.reply(str(exc), buttons=self._office_session_keyboard())
            return
        await event.reply(
            f"✅ پیام اضافه شد؛ مجموع متن {to_persian_digits(len(session.text))} نویسه است.\n"
            "می‌توانید پیام دیگری بفرستید یا قالب خروجی را انتخاب کنید.",
            buttons=self._office_session_keyboard(),
        )

    async def _accept_office_text_file(
        self, event: Any, user_id: int, filename: str, suffix: str
    ) -> None:
        max_bytes = int(self.settings.office_max_input_bytes)
        reported_size = getattr(getattr(event.message, "file", None), "size", None)
        if (
            isinstance(reported_size, int)
            and not isinstance(reported_size, bool)
            and reported_size > max_bytes
        ):
            await event.reply(
                "❌ فایل متنی از سقف حجم آفیس رایگان بیشتر است. حد: "
                f"{to_persian_digits(max_bytes // 1024)} کیلوبایت"
            )
            return

        async def operation() -> None:
            progress = await WorkProgress.start(
                event,
                headline="🗂 آفیس رایگان",
                done_headline="✅ فایل متنی افزوده شد",
                stage="دریافت فایل متنی",
                detail="فایل بررسی می‌شود و به متن شما افزوده می‌شود…",
            )
            workdir = Path(
                await run_file_io_resource(
                    tempfile.mkdtemp,
                    lambda path: shutil.rmtree(path, ignore_errors=True),
                    dir=self.settings.temp_dir,
                    prefix="office-",
                )
            )
            try:
                source = workdir / f"source{suffix}"
                try:
                    await self._download_telegram_media(  # type: ignore[attr-defined]
                        event.message, source, max_bytes
                    )
                    await progress.update(60, "خواندن متن فایل")
                    content = await run_file_io(
                        read_text_file, source, max_bytes=max_bytes
                    )
                except UploadTooLargeError:
                    await self._edit(  # type: ignore[attr-defined]
                        progress,
                        failed_progress_text(
                            progress.percent,
                            "❌ فایل متنی از سقف حجم آفیس رایگان بیشتر است.",
                            headline=OFFICE_UPLOAD_STOPPED,
                        ),
                    )
                    return
                except asyncio.TimeoutError:
                    await self._edit(  # type: ignore[attr-defined]
                        progress,
                        failed_progress_text(
                            progress.percent,
                            "❌ دریافت فایل متنی ناموفق بود؛ دوباره بفرستید.",
                            headline=OFFICE_UPLOAD_STOPPED,
                        ),
                    )
                    return
                if not content.strip():
                    await self._edit(  # type: ignore[attr-defined]
                        progress,
                        failed_progress_text(
                            progress.percent,
                            "فایل متن قابل‌استفاده‌ای ندارد؛ فایل دیگری بفرستید.",
                            headline=OFFICE_UPLOAD_STOPPED,
                        ),
                    )
                    return
                session = self._office_get_session(
                    user_id
                ) or self._office_new_session(user_id, event.message)
                if session is None:
                    await self._edit(  # type: ignore[attr-defined]
                        progress,
                        failed_progress_text(
                            progress.percent,
                            "آفیس رایگان مشغول است؛ چند دقیقهٔ دیگر دوباره تلاش کنید.",
                            headline=OFFICE_UPLOAD_STOPPED,
                        ),
                    )
                    return
                try:
                    self._office_add_source(
                        session,
                        content,
                        filename=filename,
                        message=event.message,
                    )
                except OfficeError as exc:
                    await self._edit(  # type: ignore[attr-defined]
                        progress, str(exc), self._office_session_keyboard()
                    )
                    return
                await self._edit(  # type: ignore[attr-defined]
                    progress,
                    completed_progress_text(
                        "✅ فایل متنی بررسی شد و افزوده شد؛ مجموع متن "
                        f"{to_persian_digits(len(session.text))} نویسه است.\n"
                        "فایل اصلیِ پیام شما دست‌نخورده می‌ماند. حالا قالب خروجی را "
                        "انتخاب کنید یا متن دیگری بفرستید.",
                        headline="✅ فایل متنی افزوده شد",
                    ),
                    self._office_session_keyboard(),
                )
            except OfficeError as exc:
                await self._edit(  # type: ignore[attr-defined]
                    progress,
                    failed_progress_text(
                        progress.percent, str(exc), headline=OFFICE_UPLOAD_STOPPED
                    ),
                )
            finally:
                await run_file_io(shutil.rmtree, workdir, ignore_errors=True)

        await self._guard_file_intake(event, operation)

    async def _build_office_export(
        self,
        source_text: str,
        *,
        title: str,
        source_name: str | None,
        kind: str,
        reference: str,
    ) -> tuple[str, bytes, str, str]:
        if kind not in OFFICE_FORMATS:
            raise OfficeError("قالب خروجی آفیس رایگان نامعتبر است.")
        suffix = Path(source_name).suffix.lower() if source_name else ""
        delimiter = "," if suffix == ".csv" else "\t" if suffix == ".tsv" else None
        artifact_title = short_title(title, max_words=3, fallback=FALLBACK_OFFICE)
        document = await run_file_io(
            build_office_document,
            source_text,
            title=artifact_title,
            delimiter=delimiter,
        )
        meta = DocumentMeta(
            reference=reference,
            source_name=source_name,
            created_at=datetime.now(),
        )
        label = FORMAT_LABELS[kind]
        filename = office_filename(document.title, kind, reference)
        if kind == "word":
            payload = await run_file_io(
                build_plain_docx,
                document.title,
                document.text,
                fonts=resolve_fonts(),
                design=resolve_design(),
                meta=meta,
                mode_label=f"آفیس رایگان — {label}",
                heading_offset=OFFICE_HEADING_OFFSET,
            )
            caption = f"🗂 سند آزاد — {label} | {document.title}"
        elif kind == "excel":
            tables = await run_file_io(office_document_tables, document)
            source_has_table = any(block.kind == "table" for block in document.blocks)
            if (
                not source_has_table
                and self.settings.note_configured
                and len(document.text) <= MAX_DECK_CHUNK_CHARS
            ):
                inferred = await infer_table(
                    document.text,
                    settings=self.settings,
                    session=self._http_session(),  # type: ignore[attr-defined]
                )
                if inferred is not None:
                    inferred_cells = " ".join(
                        [
                            *inferred.headers,
                            *(cell for row in inferred.rows for cell in row),
                        ]
                    )
                    corpus = normalize_for_compare(inferred_cells)
                    source_lines = [
                        block.text for block in document.blocks if block.text.strip()
                    ]
                    if source_lines and all(
                        normalize_for_compare(line) in corpus
                        for line in source_lines
                    ):
                        tables = [inferred]
            payload = await run_file_io(build_office_xlsx, document, tables)
            caption = f"🗂 سند آزاد — {label} | {document.title}"
        elif kind == "ppt":
            slides = None
            max_content_slides = self.settings.office_max_slides - 3
            if self.settings.note_configured:
                slides = await plan_deck(
                    document.text,
                    settings=self.settings,
                    session=self._http_session(),  # type: ignore[attr-defined]
                    max_sections=max_content_slides,
                )
            if slides is None:
                slides = await run_file_io(
                    fallback_slides,
                    document,
                    max_slides=max_content_slides,
                )
            payload = await run_file_io(
                build_office_pptx,
                document,
                slides,
                subtitle="ارائهٔ تازه از متن؛ قالب‌بندی فایل اصلی تبدیل نمی‌شود",
                max_slides=self.settings.office_max_slides,
            )
            caption = (
                f"🗂 ارائهٔ تازه از متن «{document.title}»؛ "
                "فایل اصلی دست‌نخورده مانده است."
            )
        else:
            payload = await run_file_io(
                build_office_pdf,
                document,
                meta=meta,
                mode_label=f"آفیس رایگان — {label}",
            )
            caption = f"🗂 سند آزاد — {label} | {document.title}"
        library_title = f"{document.title} — {label}"
        return filename, payload, caption, library_title

    async def _save_office_library_item(
        self, user_id: int, message: Message, title: str
    ) -> None:
        chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            logger.warning("Office file delivered without a library message reference")
            return
        try:
            await self.db.library_add(
                user_id,
                "office",
                chat_id,
                message_id,
                None,
                title=title[:200],
            )
            await self.db.library_prune(
                user_id, self.settings.library_max_items,
                protected_item_ids=self._protected_curation_items(user_id),
            )
        except Exception as exc:
            logger.warning("could not index Office file error=%s", type(exc).__name__)

    async def _office_run_operation(
        self,
        event: Any,
        user_id: int,
        *,
        stage: str,
        success_text: str,
        buttons: list[list],
        operation: Any,
    ) -> None:
        if not self.settings.office_enabled:
            await self._ack(  # type: ignore[attr-defined]
                event, "آفیس رایگان در تنظیمات سرور غیرفعال است.", alert=True
            )
            return
        if user_id in self._office_in_flight:
            await self._ack(  # type: ignore[attr-defined]
                event, "یک خروجی آفیس رایگان برای شما در حال ساخت است.", alert=True
            )
            return
        now = self.deps.monotonic()
        interval = max(0.0, float(self.settings.office_min_interval_seconds))
        throttle_ttl = max(60.0, interval * 2)
        for owner, used_at in list(self._office_last_use.items()):
            if now - used_at > throttle_ttl:
                self._office_last_use.pop(owner, None)
        if (
            user_id not in self._office_last_use
            and len(self._office_last_use) >= MAX_OFFICE_SESSIONS
        ):
            oldest = min(
                self._office_last_use, key=lambda owner: self._office_last_use[owner]
            )
            self._office_last_use.pop(oldest, None)
        last = self._office_last_use.get(user_id)
        if interval and last is not None and now - last < interval:
            remaining = max(1, int(interval - (now - last) + 0.999))
            await self._ack(  # type: ignore[attr-defined]
                event,
                f"برای حفظ ظرفیت سرویس، {to_persian_digits(remaining)} ثانیه دیگر دوباره تلاش کنید.",
                alert=True,
            )
            return
        self._office_last_use[user_id] = now
        self._office_in_flight.add(user_id)
        task = asyncio.current_task()
        if task is not None:
            self._office_tasks.add(task)
        workdir: Path | None = None
        delivered = 0
        progress = WorkProgress.for_editor(
            lambda text: self._edit(event, text, []),  # type: ignore[attr-defined]
            headline="🗂 آفیس رایگان",
            done_headline="✅ فایل آماده شد",
            submission_id=None,
        )
        try:
            await progress.update(
                4, stage, "درخواست ثبت شد؛ ساخت به‌زودی آغاز می‌شود."
            )
            async with self._office_semaphore:
                workdir = Path(
                    await run_file_io_resource(
                        tempfile.mkdtemp,
                        lambda path: shutil.rmtree(path, ignore_errors=True),
                        dir=self.settings.temp_dir,
                        prefix="office-",
                    )
                )
                outputs = await operation(workdir, progress)
                reply_to = getattr(event, "message_id", None)
                if not isinstance(reply_to, int):
                    reply_to = getattr(getattr(event, "message", None), "id", None)
                total = max(1, len(outputs))
                for index, (
                    filename,
                    payload,
                    caption,
                    library_title,
                ) in enumerate(outputs):
                    if not payload or len(payload) > MAX_OFFICE_OUTPUT_BYTES:
                        raise OfficeError(
                            "حجم خروجی از سقف آفیس رایگان بیشتر است؛ سند را کوچک‌تر کنید."
                        )
                    path = workdir / filename
                    await progress.update(
                        self.OFFICE_BUILD_PERCENT
                        + round(self.OFFICE_DELIVERY_PERCENT * index / total),
                        "ارسال فایل خروجی",
                        f"{Path(filename).suffix.lower()} در حال ارسال است…",
                    )
                    await run_file_io(path.write_bytes, payload)
                    try:
                        sent = await self._send_telegram_file(  # type: ignore[attr-defined]
                            user_id,
                            path,
                            caption=caption[:TELEGRAM_CAPTION_LIMIT],
                            reply_to=reply_to,
                            force_document=True,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Office Telegram delivery failed format=%s error=%s",
                            Path(filename).suffix.lower(),
                            type(exc).__name__,
                        )
                        if delivered:
                            await self._edit(  # type: ignore[attr-defined]
                                event,
                                failed_progress_text(
                                    progress.percent,
                                    "⚠️ بخشی از خروجی‌ها ارسال و در کتابخانه ذخیره شد؛ "
                                    "ارسال فایل بعدی ناموفق بود.",
                                    headline="⚠️ ارسال کامل نشد.",
                                ),
                                buttons,
                            )
                            return
                        raise OfficeError(
                            "ارسال فایل خروجی ناموفق بود؛ دوباره تلاش کنید."
                        ) from exc
                    if sent is None:
                        raise OfficeError(
                            "تلگرام فایل خروجی را تأیید نکرد؛ دوباره تلاش کنید."
                        )
                    delivered += 1
                    await self._save_office_library_item(user_id, sent, library_title)
            await self._edit(  # type: ignore[attr-defined]
                event,
                completed_progress_text(success_text, headline="✅ فایل آماده شد"),
                buttons,
            )
        except (OfficeError, StructuringError) as exc:
            await self._edit(  # type: ignore[attr-defined]
                event,
                failed_progress_text(
                    progress.percent, str(exc), headline="❌ ساخت فایل ناموفق بود."
                ),
                buttons,
            )
        finally:
            if workdir is not None:
                await run_file_io(shutil.rmtree, workdir, ignore_errors=True)
            self._office_in_flight.discard(user_id)
            if task is not None:
                self._office_tasks.discard(task)

    async def _run_office_session_export(
        self, event: Any, user_id: int, kind: str
    ) -> None:
        session = self._office_get_session(user_id)
        if session is None or not session.text.strip():
            await self._ack(  # type: ignore[attr-defined]
                event,
                "متن آفیس رایگان پیدا نشد؛ از منو دوباره شروع کنید.",
                alert=True,
            )
            return
        if kind not in OFFICE_FORMATS:
            await self._ack(event, "قالب خروجی نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        source_text = session.text
        title = session.title
        source_name = session.filename
        reference = f"OFF-{secrets.token_hex(4).upper()}"

        async def build(_workdir: Path, progress: WorkProgress) -> list[Any]:
            await progress.update(
                self.OFFICE_BUILD_PERCENT // 2,
                f"ساخت فایل {FORMAT_LABELS[kind]}",
                "متن شما در حال قالب‌بندی است…",
            )
            return [
                await self._build_office_export(
                    source_text,
                    title=title,
                    source_name=source_name,
                    kind=kind,
                    reference=reference,
                )
            ]

        session.updated_at = self.deps.monotonic()
        await self._office_run_operation(
            event,
            user_id,
            stage=f"ساخت فایل {FORMAT_LABELS[kind]} از متن شما",
            success_text=(
                "✅ فایل ساخته شد و در کتابخانهٔ شما ذخیره شد.\n"
                "می‌توانید قالب دیگری هم از همین متن بسازید یا متن بیشتری اضافه کنید."
            ),
            buttons=self._office_session_keyboard(),
            operation=build,
        )

    def _document_export_keyboard(self, document_id: int) -> list[list]:
        return [
            [
                Button.inline("📝 Word", f"doc:ex:{document_id}:word".encode("ascii")),
                Button.inline(
                    "📊 Excel", f"doc:ex:{document_id}:excel".encode("ascii")
                ),
            ],
            [
                Button.inline(
                    "📖 PowerPoint", f"doc:ex:{document_id}:ppt".encode("ascii")
                ),
                Button.inline("📕 PDF", f"doc:ex:{document_id}:pdf".encode("ascii")),
            ],
            [
                Button.inline(
                    "↩️ بازگشت به سند", f"doc:show:{document_id}".encode("ascii")
                )
            ],
        ]

    async def _show_document_export_screen(
        self, event: Any, user_id: int, document_id: int
    ) -> None:
        if not self.settings.office_enabled:
            await self._ack(  # type: ignore[attr-defined]
                event, "آفیس رایگان در تنظیمات سرور غیرفعال است.", alert=True
            )
            return
        document = await self.db.document_meta(user_id, document_id)
        if document is None:
            await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        await self._edit(  # type: ignore[attr-defined]
            event,
            "قالب خروجی را از متن استخراج‌شده انتخاب کنید. فایل اصلی دست‌نخورده می‌ماند.\n"
            "PowerPoint تازه‌ای از متن ساخته می‌شود و تبدیل پیکسلی/ظاهری ارائهٔ اصلی نیست.\n"
            "همهٔ عملیات رایگان‌اند و از اعتبار کم نمی‌کنند.",
            self._document_export_keyboard(document_id),
        )

    async def _run_document_export(
        self, event: Any, user_id: int, document_id: int, kind: str
    ) -> None:
        if not self.settings.office_enabled:
            await self._ack(  # type: ignore[attr-defined]
                event, "آفیس رایگان در تنظیمات سرور غیرفعال است.", alert=True
            )
            return
        document = await self.db.document_get(user_id, document_id)
        if document is None:
            await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        if kind not in OFFICE_FORMATS:
            await self._ack(event, "قالب خروجی نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        original_text = str(document.get("text") or "")
        if len(original_text) > MAX_OFFICE_NOTE_SOURCE_CHARS:
            await self._ack(  # type: ignore[attr-defined]
                event,
                "متن از سقف خروجی آفیس رایگان بیشتر است؛ فایل اصلی را دریافت و به چند سند تقسیم کنید. "
                "هیچ بخشی از متن در خروجی کوتاه نمی‌شود.",
                alert=True,
            )
            return
        cached = await run_file_io(
            chunks_from_json,
            document.get("chunks_json"),
            max_chars=MAX_MODEL_INPUT_CHARS,
        )
        source = "\n\n".join(chunk.text for chunk in cached) if cached else original_text
        if len(source) > MAX_OFFICE_NOTE_SOURCE_CHARS:
            await self._ack(  # type: ignore[attr-defined]
                event,
                "متن کامل با مکان‌یاب‌ها از سقف خروجی آفیس رایگان بیشتر است؛ فایل اصلی را دریافت کنید و سند را تقسیم کنید. هیچ بخشی کوتاه نمی‌شود.",
                alert=True,
            )
            return
        reference = f"DOC-{document_id}-{secrets.token_hex(3).upper()}"
        title = str(document.get("title") or "سند")

        async def build(_workdir: Path, progress: WorkProgress) -> list[Any]:
            await progress.update(
                self.OFFICE_BUILD_PERCENT // 2,
                f"ساخت خروجی {FORMAT_LABELS[kind]}",
                "متن استخراج‌شدهٔ سند در حال قالب‌بندی است…",
            )
            return [
                await self._build_office_export(
                    source,
                    title=title,
                    source_name=None,
                    kind=kind,
                    reference=reference,
                )
            ]

        buttons = [
            [
                Button.inline(
                    "↩️ بازگشت به سند", f"doc:show:{document_id}".encode("ascii")
                )
            ],
            [Button.inline("📚 بازگشت به کتابخانه", b"lib:docs:0")],
        ]
        await self._office_run_operation(
            event,
            user_id,
            stage=f"ساخت خروجی {FORMAT_LABELS[kind]} از متن سند",
            success_text=(
                "✅ خروجی ساخته شد و در کتابخانه ذخیره شد.\n"
                "فایل اصلی و مکان‌یاب‌های متن دست‌نخورده‌اند؛ ارائهٔ تازه جایگزین ارائهٔ اصلی نیست."
            ),
            buttons=buttons,
            operation=build,
        )

    async def _run_document_notes(
        self, event: Any, user_id: int, document_id: int, mode: str
    ) -> None:
        if not self.settings.office_enabled:
            await self._ack(  # type: ignore[attr-defined]
                event, "آفیس رایگان در تنظیمات سرور غیرفعال است.", alert=True
            )
            return
        document = await self.db.document_get(user_id, document_id)
        if document is None:
            await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        if mode not in {"full", "compact"}:
            await self._ack(event, "نوع جزوه نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        source_text = str(document.get("text") or "")
        title = short_title(
            str(document.get("title") or ""), fallback=FALLBACK_OFFICE, max_words=2
        )
        if not source_text.strip():
            await self._ack(  # type: ignore[attr-defined]
                event, "متن قابل‌استفاده‌ای برای جزوه پیدا نشد.", alert=True
            )
            return
        if len(source_text) > MAX_OFFICE_NOTE_SOURCE_CHARS:
            await self._ack(  # type: ignore[attr-defined]
                event,
                "متن استخراج‌شده از سقف تولید جزوهٔ آزاد بیشتر است؛ فایل اصلی را دریافت کنید و سند را به چند بخش کوچک‌تر تقسیم کنید. هیچ بخشی کوتاه یا حذف نشده است.",
                alert=True,
            )
            return
        cached = await run_file_io(
            chunks_from_json,
            document.get("chunks_json"),
            max_chars=MAX_MODEL_INPUT_CHARS,
        )
        chunks = await run_file_io(
            office_source_chunks,
            source_text,
            cached or None,
            max_chars=MAX_MODEL_INPUT_CHARS,
        )
        if not chunks:
            await self._ack(  # type: ignore[attr-defined]
                event, "متن قابل‌استفاده‌ای برای جزوه پیدا نشد.", alert=True
            )
            return
        reference = f"DOC-{document_id}-{mode.upper()}-{secrets.token_hex(3).upper()}"
        meta = DocumentMeta(
            reference=reference,
            source_name=title,
            created_at=datetime.now(),
        )
        mode_label = (
            "جزوهٔ کاملِ منبع‌محور" if mode == "full" else "یادداشت مرور سریع"
        )
        buttons = [
            [
                Button.inline(
                    "↩️ بازگشت به سند", f"doc:show:{document_id}".encode("ascii")
                )
            ],
            [Button.inline("📚 بازگشت به کتابخانه", b"lib:docs:0")],
        ]

        async def build(_workdir: Path, progress: WorkProgress) -> list[Any]:
            fallback_used = False
            await progress.update(
                10,
                "آماده‌سازی بخش‌های سند",
                f"{to_persian_digits(len(chunks))} بخش منبع آماده شد.",
            )
            if mode == "full":
                await progress.update(
                    20,
                    "تولید جزوهٔ منبع‌محور",
                    "ساخت متن ممکن است چند دقیقه طول بکشد…",
                )
                notes, fallback_used = await generate_full_booklet(
                    chunks,
                    title=title,
                    settings=self.settings,
                    session=(
                        self._http_session()  # type: ignore[attr-defined]
                        if self.settings.note_configured
                        else None
                    ),
                )
                notes = replace(notes, title=title)
                markdown = notes.to_markdown()
                word_payload = await run_file_io(
                    build_notes_docx,
                    notes,
                    fonts=resolve_fonts(),
                    design=resolve_design(),
                    meta=meta,
                )
            else:
                await progress.update(
                    20,
                    "استخراج نکته‌های فشرده",
                    "ساخت یادداشت ممکن است چند دقیقه طول بکشد…",
                )
                compact, fallback_used = await generate_compact_notes(
                    chunks,
                    title=title,
                    settings=self.settings,
                    session=(
                        self._http_session()  # type: ignore[attr-defined]
                        if self.settings.note_configured
                        else None
                    ),
                )
                markdown = compact.to_markdown()
                word_payload = await run_file_io(
                    build_plain_docx,
                    title,
                    markdown,
                    fonts=resolve_fonts(),
                    design=resolve_design(),
                    meta=meta,
                    mode_label="حالت خروجی: یادداشت مرور سریع",
                    heading_offset=OFFICE_HEADING_OFFSET,
                )
            await progress.update(
                self.OFFICE_BUILD_PERCENT - 10,
                "ساخت فایل‌های Word و PDF",
                "خروجی‌ها با همان مکان‌یاب‌های سند ساخته می‌شوند…",
            )
            pdf_document = await run_file_io(
                build_office_document,
                markdown,
                title=title,
            )
            pdf_payload = await run_file_io(
                build_office_pdf,
                pdf_document,
                meta=meta,
                mode_label=mode_label,
            )
            variant = "کامل" if mode == "full" else "مرور"
            if fallback_used:
                logger.info(
                    "Office document notes used deterministic source fallback mode=%s",
                    mode,
                )
            base_title = short_title(title, fallback=FALLBACK_OFFICE, max_words=2)
            return [
                (
                    office_filename(title, "word", reference),
                    word_payload,
                    f"📘 {variant} — Word | {title}",
                    f"{base_title} — {variant} Word",
                ),
                (
                    office_filename(title, "pdf", reference),
                    pdf_payload,
                    f"📕 {variant} — PDF | {title}",
                    f"{base_title} — {variant} PDF",
                ),
            ]

        await self._office_run_operation(
            event,
            user_id,
            stage=(
                "ساخت جزوهٔ کاملِ منبع‌محور و نسخهٔ Word/PDF"
                if mode == "full"
                else "استخراج نکته‌های فشرده و ساخت Word/PDF"
            ),
            success_text=(
                f"✅ {mode_label} ساخته شد و هر دو فایل Word و PDF در کتابخانه ذخیره شدند.\n"
                "برچسب‌های صفحه/اسلاید از سند استخراج‌شده حفظ شده‌اند؛ فایل اصلی هم همچنان قابل دریافت است."
            ),
            buttons=buttons,
            operation=build,
        )

    async def _guard_file_intake(self, event: Any, operation: Any) -> None:
        if self._shutting_down:
            await event.respond("ربات در حال راه‌اندازی مجدد است؛ دوباره تلاش کنید.")
            return
        task = asyncio.current_task()
        if task is not None:
            self._intake_tasks.add(task)
        try:
            await operation()
        finally:
            if task is not None:
                self._intake_tasks.discard(task)

    async def _accept_document(
        self, event: Any, user_id: int, source_type: str, filename: str | None
    ) -> None:
        if not await self._require_membership(event, user_id, kind="document"):
            return
        await self._guard_file_intake(
            event,
            lambda: self._accept_document_inner(event, user_id, source_type, filename),
        )

    async def _accept_document_inner(
        self, event: Any, user_id: int, source_type: str, filename: str | None
    ) -> None:
        max_bytes = int(self.settings.doc_max_bytes)
        reported_size = getattr(getattr(event.message, "file", None), "size", None)
        if isinstance(reported_size, int) and reported_size > max_bytes:
            await event.reply(
                "❌ حجم فایل بیشتر از سقف مجاز اسناد است. حد مجاز: "
                f"{to_persian_digits(max_bytes // (1024 * 1024))} مگابایت"
            )
            return
        progress = await WorkProgress.start(
            event,
            headline="📄 استخراج متن سند",
            done_headline="✅ متن استخراج شد",
            stage="دریافت فایل",
            detail="فایل از تلگرام دریافت می‌شود…",
        )
        workdir = Path(
            await run_file_io_resource(
                tempfile.mkdtemp,
                lambda path: shutil.rmtree(path, ignore_errors=True),
                dir=self.settings.temp_dir,
                prefix="doc-",
            )
        )
        try:
            suffix = (
                Path(filename).suffix.lower()
                if filename
                else DEFAULT_INTAKE_SUFFIX.get(source_type, f".{source_type}")
            )
            path = workdir / f"source{suffix}"
            try:
                await self._download_telegram_media(  # type: ignore[attr-defined]
                    event.message, path, max_bytes
                )
                await progress.update(
                    40,
                    "استخراج متن",
                    "متن سند خوانده و به بخش‌های قابل ارجاع تقسیم می‌شود…",
                )
            except UploadTooLargeError:
                await self._edit(  # type: ignore[attr-defined]
                    progress,
                    failed_progress_text(
                        progress.percent,
                        "❌ حجم فایل از سقف مجاز اسناد بیشتر است.",
                        headline=DOC_EXTRACTION_STOPPED,
                    ),
                )
                return
            except asyncio.TimeoutError:
                await self._edit(  # type: ignore[attr-defined]
                    progress,
                    failed_progress_text(
                        progress.percent,
                        "❌ دریافت فایل ناموفق بود؛ دوباره بفرستید.",
                        headline=DOC_EXTRACTION_STOPPED,
                    ),
                )
                return
            chat_id = getattr(event, "chat_id", None) or user_id
            message_id = getattr(getattr(event, "message", None), "id", None)
            try:
                document, created = await self._ingest_document(
                    user_id,
                    path,
                    source_type=source_type,
                    filename=filename,
                    file_msg_ref=f"{chat_id}:{message_id}" if message_id else "",
                )
                await progress.update(90, "آماده‌سازی پیش‌نمایش سند")
            except ScannedPdfError as exc:
                await self._edit(progress, str(exc))  # type: ignore[attr-defined]
                return
            except EmptyPresentationTextError as exc:
                buttons = None
                if isinstance(message_id, int) and message_id > 0:
                    buttons = [
                        [
                            Button.inline(
                                "🎧 پردازش صدای ارائه",
                                f"doc:lecturemsg:{message_id}".encode("ascii"),
                            )
                        ]
                    ]
                await self._edit(  # type: ignore[attr-defined]
                    progress,
                    f"⚠️ {exc}\nاگر ارائه صدای ضبط‌شده دارد، می‌توانید آن را پردازش کنید.",
                    buttons,
                )
                return
            except OcrError as exc:
                logger.warning(
                    "ocr failed user=%s: %s", user_id, extract_text_problem(exc)
                )
                await self._edit(  # type: ignore[attr-defined]
                    progress,
                    failed_progress_text(
                        progress.percent,
                        DOC_OCR_FAILED,
                        headline=DOC_EXTRACTION_STOPPED,
                    ),
                )
                return
            except DocExtractError as exc:
                await self._edit(  # type: ignore[attr-defined]
                    progress,
                    failed_progress_text(
                        progress.percent,
                        f"⚠️ {exc}",
                        headline=DOC_EXTRACTION_STOPPED,
                    ),
                )
                return
        except Exception:
            logger.exception("document ingestion failed user=%s", user_id)
            await self._edit(  # type: ignore[attr-defined]
                progress,
                failed_progress_text(
                    progress.percent,
                    "⚠️ استخراج متن این سند ناموفق بود؛ فایل دیگری بفرستید.",
                    headline=DOC_EXTRACTION_STOPPED,
                ),
            )
            return
        finally:
            await run_file_io(shutil.rmtree, workdir, True)
        header = (
            ""
            if created
            else "ℹ️ این فایل قبلاً استخراج شده بود؛ همان متن ذخیره‌شده استفاده می‌شود.\n\n"
        )
        await self._edit(  # type: ignore[attr-defined]
            progress,
            header + self._document_card_text(document),
            self._document_buttons(int(document["id"]), str(document["source_type"])),
        )

    async def _forward_document(
        self, event: Any, user_id: int, document_id: int
    ) -> None:
        document = await self.db.document_meta(user_id, document_id)
        if document is None or not document.get("file_msg_ref"):
            await self._ack(  # type: ignore[attr-defined]
                event, "این فایل دیگر در دسترس نیست.", alert=True
            )
            return
        back = [
            [Button.inline("↩️ بازگشت", f"doc:show:{document_id}".encode("ascii"))]
        ]
        progress = WorkProgress.for_editor(
            lambda text: self._edit(event, text, back),  # type: ignore[attr-defined]
            headline="📥 دریافت فایل اصلی",
            done_headline="✅ فایل ارسال شد",
        )
        await progress.update(
            35, "ارسال فایل", "فایل اصلی از تلگرام دوباره ارسال می‌شود…"
        )
        try:
            chat_raw, message_raw = str(document["file_msg_ref"]).split(":", 1)
            await self.client.forward_messages(
                event.chat_id, [int(message_raw)], from_peer=int(chat_raw)
            )
        except Exception:
            logger.warning("could not forward document=%s", document_id, exc_info=True)
            await progress.fail(
                "این فایل دیگر در دسترس نیست.",
                headline="❌ دریافت فایل ناموفق بود.",
            )
            await self._ack(  # type: ignore[attr-defined]
                event, "این فایل دیگر در دسترس نیست.", alert=True
            )
            return
        await progress.complete("✅ فایل اصلی دوباره در همین گفتگو ارسال شد.")

    async def _start_document_presentation(
        self, event: Any, user_id: int, document_id: int
    ) -> None:
        document = await self.db.document_meta(user_id, document_id)
        if not document or document["source_type"] != "pptx":
            await event.respond("این ارائه در دسترس نیست.")
            return
        try:
            chat_id, message_id = (
                int(part) for part in str(document["file_msg_ref"]).split(":", 1)
            )
        except (ValueError, TypeError):
            await event.respond(
                "فایل اصلی ارائه در دسترس نیست؛ لطفاً دوباره آن را بفرستید."
            )
            return
        await self._start_presentation_from_message(event, user_id, chat_id, message_id)

    async def _start_presentation_from_message(
        self, event: Any, user_id: int, chat_id: int, message_id: int
    ) -> None:
        try:
            if chat_id != user_id or message_id <= 0:
                raise ValueError("presentation source is not in the user's private chat")
            message = await self.client.get_messages(user_id, ids=message_id)
            if message is None or getattr(message, "out", False):
                raise ValueError("source upload missing")
            sender_id = getattr(message, "sender_id", None)
            if sender_id is not None and sender_id != user_id:
                raise ValueError("source upload belongs to another sender")
            kind, filename = _presentation_metadata(message)
            if kind not in {"native", "legacy"}:
                raise ValueError("source is not a supported presentation")
        except Exception:
            logger.warning(
                "presentation source unavailable user=%s", user_id, exc_info=True
            )
            await event.respond(
                "فایل اصلی ارائه در دسترس نیست؛ لطفاً دوباره آن را بفرستید."
            )
            return
        job_event = SimpleNamespace(
            message=message,
            sender_id=user_id,
            chat_id=event.chat_id,
            reply=event.respond,
            respond=event.respond,
        )
        await self._accept_presentation(job_event, user_id, kind, filename)  # type: ignore[attr-defined]

    async def _show_documents(self, event: Any, *, page: int = 0) -> None:
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 6
        count = await self.db.document_count(user_id)
        page = min(max(0, int(page)), max(0, (count - 1) // page_size))
        documents = await self.db.documents_list(
            user_id, limit=page_size, offset=page * page_size
        )
        if count == 0:
            await self._edit(  # type: ignore[attr-defined]
                event,
                "📄 اسناد\n\n"
                "هنوز سندی ذخیره نکرده‌اید.\n"
                "فایل PDF، Word یا PowerPoint را همین‌جا بفرستید تا متنش استخراج و "
                "از آن فلش‌کارت و سؤال ساخته شود.",
                [[Button.inline("↩️ کتابخانه", b"library")]],
            )
            return
        lines = ["📄 اسناد", "", f"تعداد: {to_persian_digits(count)}"]
        buttons: list[list] = []
        for document in documents:
            label = SOURCE_TYPE_LABELS_FA.get(str(document["source_type"]), "سند")
            buttons.append(
                [
                    Button.inline(
                        button_label(f"📄 {document.get('title') or label}"),
                        f"doc:show:{document['id']}".encode("ascii"),
                    )
                ]
            )
        nav: list = []
        if page > 0:
            nav.append(
                Button.inline("◀️ قبلی", f"lib:docs:{page - 1}".encode("ascii"))
            )
        if (page + 1) * page_size < count:
            nav.append(
                Button.inline("بعدی ▶️", f"lib:docs:{page + 1}".encode("ascii"))
            )
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("↩️ کتابخانه", b"library")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_document_card(
        self, event: Any, user_id: int, document_id: int
    ) -> None:
        document = await self.db.document_meta(user_id, document_id)
        if document is None:
            await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            await self._show_documents(event)
            return
        await self._edit(  # type: ignore[attr-defined]
            event,
            self._document_card_text(document),
            self._document_buttons(document_id, str(document["source_type"])),
        )

    async def _show_document_generate_screen(
        self, event: Any, user_id: int, document_id: int, kind: str
    ) -> None:
        document = await self.db.document_meta(user_id, document_id)
        if document is None:
            await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        deck_label = "مجموعهٔ فلش‌کارت" if kind == "deck" else "آزمون"
        buttons: list[list] = []
        free_kind = "deck" if kind == "deck" else "quiz"
        free = await self._free_try_available(user_id, free_kind)  # type: ignore[attr-defined]
        if kind == "deck":
            base_size = int(self.settings.flashcard_ai_deck_size)
            base_cost = int(self.settings.flashcard_ai_cost_seconds)
            for code, label, cards, _questions in SIZE_PRESETS:
                buttons.append(
                    [
                        Button.inline(
                            button_label(
                                f"{label} — {to_persian_digits(cards)} کارت — "
                                f"{format_duration(self._preset_cost(base_cost, cards, base_size))} اعتبار"  # type: ignore[attr-defined]
                            ),
                            f"doc:gen:fc:{document_id}:{code}".encode("ascii"),
                        )
                    ]
                )
        else:
            per_question = int(self.settings.quiz_cost_seconds_per_question)
            for _code, label, _cards, questions in SIZE_PRESETS:
                buttons.append(
                    [
                        Button.inline(
                            button_label(
                                f"{label} — {to_persian_digits(questions)} سؤال — "
                                f"{format_duration(questions * per_question)} اعتبار"
                            ),
                            f"doc:gen:quiz:{document_id}:{questions}".encode("ascii"),
                        )
                    ]
                )
        header = (
            f"🃏 ساخت فلش‌کارت از «{document.get('title') or 'سند'}»"
            if kind == "deck"
            else f"❓ ساخت سؤال از «{document.get('title') or 'سند'}»"
        )
        lines = [header, "", "اندازهٔ مجموعه را انتخاب کنید."]
        if free:
            lines.append(f"\n{FREE_TRY_BADGE} — اولین {deck_label} شما رایگان است.")
        buttons.append(
            [Button.inline("↩️ سند", f"doc:show:{document_id}".encode("ascii"))]
        )
        await self._edit(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _generate_from_document(
        self,
        event: Any,
        user_id: int,
        document_id: int,
        kind: str,
        choice: str,
    ) -> None:
        if not self.settings.note_configured:
            await event.respond("سرویس تولید محتوا در حال حاضر فعال نیست.")
            return
        source = await self._source_from_document(user_id, document_id)  # type: ignore[attr-defined]
        if source is None:
            await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        if kind == "deck":
            if choice and preset_cards(choice) is None:
                await self._ack(  # type: ignore[attr-defined]
                    event, texts.INVALID_LEARNING_SIZE, alert=True
                )
                return
            if (
                await self.db.deck_count(user_id)
                >= self.settings.flashcard_max_decks_per_user
            ):
                await event.respond(
                    "به حداکثر تعداد مجموعه‌های فلش‌کارت رسیده‌اید؛ یک مجموعهٔ قدیمی را "
                    "حذف کنید."
                )
                return
            wanted, cost = self._deck_size_and_cost(choice or None)  # type: ignore[attr-defined]
            note = f"fc-pending:doc:{document_id}"
        else:
            wanted = _to_int(choice) or 5
            if wanted not in QUIZ_COUNTS:
                await self._ack(event, "تعداد سؤال نامعتبر است.", alert=True)  # type: ignore[attr-defined]
                return
            cost = wanted * int(self.settings.quiz_cost_seconds_per_question)
            note = f"quiz-pending:doc:{document_id}"
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
            async with self._learning_lock(user_id):  # type: ignore[attr-defined]
                if kind != "deck":
                    reason = await self.db.quiz_admission(
                        user_id,
                        max_active_hours=self.settings.quiz_max_active_hours,
                        max_sessions_per_hour=self.settings.quiz_max_sessions_per_hour,
                    )
                    if reason == "active":
                        await event.respond(
                            "یک آزمون فعال دیگر دارید؛ ابتدا آن را تمام کنید."
                        )
                        return
                    if reason == "rate":
                        await event.respond(
                            "تعداد آزمون‌های شما در یک ساعت اخیر به حداکثر مجاز رسیده است."
                        )
                        return
                await self._generate_learning(  # type: ignore[attr-defined]
                    event,
                    user_id=user_id,
                    kind="deck" if kind == "deck" else "quiz",
                    source=source,
                    wanted=wanted,
                    cost=cost,
                    note=note,
                    title=(
                        f"{short_title(source['title'], fallback=FALLBACK_BOOKLET)} — فلش‌کارت"
                        if kind == "deck"
                        else f"{short_title(source['title'], fallback=FALLBACK_BOOKLET)} — آزمون"
                    ),
                )
        finally:
            self._learning_in_flight.discard(user_id)
            if self._learning_tasks.get(user_id) is task:
                self._learning_tasks.pop(user_id, None)

    async def _store_presentation_document(
        self, job: Job, source_path: Path, content: Any
    ) -> None:
        try:
            sections = await run_file_io(sections_from_slides, content.slides)
            if not sections:
                return
            text = await run_file_io(
                lambda: "\n\n".join(section.render() for section in sections)
            )
            if not text.strip():
                return
            chunks = await run_file_io(
                chunk_sections, sections, max_chars=MAX_MODEL_INPUT_CHARS
            )
            digest = await run_file_io(sha256_file, source_path)
            document, _created = await self._save_document(
                int(job.user_id),
                source_type="pptx",
                title=document_title(job.filename, "pptx"),
                file_msg_ref=(
                    f"{getattr(job.message, 'chat_id', job.user_id)}:"
                    f"{getattr(job.message, 'id', 0) or 0}"
                ),
                sha256=digest,
                page_count=len(content.slides),
                text=text,
                chunks=chunks,
            )
            await self._document_library_item(int(job.user_id), document)
        except Exception:
            logger.warning(
                "could not cache presentation text as a document submission=%s",
                job.submission_id,
                exc_info=True,
            )

    async def _route_office_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if not self.settings.office_enabled:
            await self._ack(  # type: ignore[attr-defined]
                event, "آفیس رایگان در تنظیمات سرور غیرفعال است.", alert=True
            )
            return
        if user_id in self._office_in_flight:
            await self._ack(  # type: ignore[attr-defined]
                event, "خروجی آفیس رایگان شما در حال ساخت است.", alert=True
            )
            return
        if action == "start" and len(parts) == 2:
            await self._office_start(event, user_id)
            return
        if action == "continue" and len(parts) == 2:
            if (
                self._office_new_session(user_id, getattr(event, "message", None))
                is None
            ):
                await self._ack(  # type: ignore[attr-defined]
                    event,
                    "آفیس رایگان مشغول است؛ چند دقیقهٔ دیگر دوباره تلاش کنید.",
                    alert=True,
                )
                return
            await self._office_start(event, user_id)
            return
        if action == "formats" and len(parts) == 2:
            await self._office_show_formats(event, user_id)
            return
        if action == "cancel" and len(parts) == 2:
            self._office_sessions.pop(user_id, None)
            await self._edit(  # type: ignore[attr-defined]
                event,
                "پیش‌نویس آفیس رایگان پاک شد. فایل‌های خروجیِ قبلی در کتابخانه می‌مانند.",
                self._user_menu(is_admin=self._is_admin(user_id)),  # type: ignore[attr-defined]
            )
            return
        if action == "fmt" and len(parts) == 3 and parts[2] in OFFICE_FORMATS:
            await self._run_office_session_export(event, user_id, parts[2])
            return
        await self._ack(event, "درخواست آفیس رایگان نامعتبر است.", alert=True)  # type: ignore[attr-defined]

    async def _route_document_callback(
        self, event: Any, user_id: int, data: str
    ) -> None:
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "show" and len(parts) == 3:
            document_id = _to_int(parts[2])
            if document_id is None:
                await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            await self._show_document_card(event, user_id, document_id)
            return
        if action == "export" and len(parts) == 3:
            document_id = _to_int(parts[2])
            if document_id is None:
                await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            await self._show_document_export_screen(event, user_id, document_id)
            return
        if action == "ex" and len(parts) == 4:
            document_id = _to_int(parts[2])
            kind = parts[3]
            if document_id is None or kind not in OFFICE_FORMATS:
                await self._ack(  # type: ignore[attr-defined]
                    event, "قالب خروجی یا سند نامعتبر است.", alert=True
                )
                return
            await self._run_document_export(event, user_id, document_id, kind)
            return
        if action == "note" and len(parts) == 4:
            document_id = _to_int(parts[2])
            mode = parts[3]
            if document_id is None or mode not in {"full", "compact"}:
                await self._ack(  # type: ignore[attr-defined]
                    event, "نوع جزوه یا سند نامعتبر است.", alert=True
                )
                return
            if not await self._require_membership(
                event, user_id, kind="callback", callback_data=data
            ):
                return
            await self._run_document_notes(event, user_id, document_id, mode)
            return
        if action == "f" and len(parts) == 3:
            document_id = _to_int(parts[2])
            if document_id is None:
                await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            await self._forward_document(event, user_id, document_id)
            return
        if action == "lecture" and len(parts) == 3:
            document_id = _to_int(parts[2])
            if document_id is None:
                await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            await self._start_document_presentation(event, user_id, document_id)
            return
        if action == "lecturemsg" and len(parts) == 3:
            message_id = _to_int(parts[2])
            if message_id is None or message_id <= 0:
                await self._ack(event, "این ارائه پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            await self._start_presentation_from_message(
                event, user_id, user_id, message_id
            )
            return
        if action in ("fc", "quiz") and len(parts) == 3:
            document_id = _to_int(parts[2])
            if document_id is None:
                await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            await self._show_document_generate_screen(
                event, user_id, document_id, "deck" if action == "fc" else "quiz"
            )
            return
        if action == "gen" and len(parts) == 5:
            if parts[2] not in {"fc", "quiz"}:
                await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
                return
            kind = "deck" if parts[2] == "fc" else "quiz"
            document_id = _to_int(parts[3])
            if document_id is None:
                await self._ack(event, "این سند پیدا نشد.", alert=True)  # type: ignore[attr-defined]
                return
            await self._generate_from_document(
                event, user_id, document_id, kind, parts[4]
            )
            return
        await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
