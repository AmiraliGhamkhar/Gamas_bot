"""Extract narration audio and slide text from PowerPoint decks.

A .pptx file is an OPC (ZIP) package: narration lives in ``ppt/media/*`` and is
bound to a slide through that slide's ``_rels`` part. The reader below walks the
package with the standard library so it stays in control of every byte it
extracts (path traversal, entry count and unpacked size are all bounded), and
uses python-pptx only for the slide/notes text.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from lxml import etree

from .config import Settings
from .media import MediaToolError, merge_audio_tracks, probe_media, tool_available

logger = logging.getLogger(__name__)

NATIVE_EXTENSIONS = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}
LEGACY_EXTENSIONS = {".ppt", ".pps", ".pot", ".odp", ".otp"}
PRESENTATION_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "native",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": "native",
    "application/vnd.openxmlformats-officedocument.presentationml.template": "native",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": "native",
    "application/vnd.ms-powerpoint.slideshow.macroenabled.12": "native",
    "application/vnd.ms-powerpoint.template.macroenabled.12": "native",
    "application/vnd.oasis.opendocument.presentation-template": "legacy",
    "application/vnd.ms-powerpoint": "legacy",
    "application/mspowerpoint": "legacy",
    "application/powerpoint": "legacy",
    "application/vnd.oasis.opendocument.presentation": "legacy",
}

AUDIO_MEDIA_EXTENSIONS = {
    ".aac", ".aiff", ".aif", ".amr", ".au", ".flac", ".m4a", ".m4b", ".mp3",
    ".oga", ".ogg", ".opus", ".wav", ".wma",
}
VIDEO_MEDIA_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".webm", ".wmv",
}

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MEDIA_REL_TYPES = {
    f"{OFFICE_REL_NS}/audio": "audio",
    f"{OFFICE_REL_NS}/video": "video",
    f"{OFFICE_REL_NS}/media": "media",
}
MAX_ZIP_ENTRIES = 20000


class PresentationError(RuntimeError):
    """Raised for decks the bot cannot or should not process."""


@dataclass(frozen=True, slots=True)
class SlideText:
    number: int
    title: str = ""
    body: tuple[str, ...] = ()
    notes: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.title.strip() or any(self.body) or self.notes.strip())

    def as_markdown(self, include_notes: bool = True) -> str:
        heading = f"### اسلاید {self.number}" + (f" — {self.title.strip()}" if self.title.strip() else "")
        lines = [heading]
        lines.extend(f"- {item}" for item in self.body if item.strip())
        if include_notes and self.notes.strip():
            lines.append("یادداشت گوینده: " + " ".join(self.notes.split()))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class MediaClip:
    order: int
    slide_number: int | None
    part_name: str
    path: Path
    kind: str
    duration: float | None = None

    @property
    def label(self) -> str:
        where = f"اسلاید {self.slide_number}" if self.slide_number else "بدون اسلاید"
        return f"{where} ({posixpath.basename(self.part_name)})"


@dataclass(frozen=True, slots=True)
class PresentationContent:
    slides: tuple[SlideText, ...]
    clips: tuple[MediaClip, ...]
    skipped: tuple[str, ...] = ()
    total_slides: int | None = None

    @property
    def slide_count(self) -> int:
        return self.total_slides if self.total_slides is not None else len(self.slides)


@dataclass(frozen=True, slots=True)
class PreparedAudio:
    path: Path
    clips: tuple[MediaClip, ...]
    total_duration: float | None
    merged: bool
    skipped: tuple[str, ...] = ()


def classify_presentation(filename: str | None, mime_type: str | None) -> str | None:
    """Return 'native', 'legacy' or None for the given Telegram document."""
    suffix = Path(filename).suffix.lower() if filename else ""
    if suffix in NATIVE_EXTENSIONS:
        return "native"
    if suffix in LEGACY_EXTENSIONS:
        return "legacy"
    normalized = (mime_type or "").split(";")[0].strip().lower()
    return PRESENTATION_MIME_TYPES.get(normalized)


def media_kind(part_name: str) -> str | None:
    suffix = posixpath.splitext(part_name)[1].lower()
    if suffix in AUDIO_MEDIA_EXTENSIONS:
        return "audio"
    if suffix in VIDEO_MEDIA_EXTENSIONS:
        return "video"
    return None


def _resolve_target(base_part: str, target: str) -> str:
    base_dir = posixpath.dirname(base_part)
    joined = target if target.startswith("/") else posixpath.join(base_dir, target)
    return posixpath.normpath(joined).lstrip("/")


def _rels_part_for(part_name: str) -> str:
    directory, name = posixpath.split(part_name)
    return posixpath.join(directory, "_rels", f"{name}.rels")


def _parse_xml(payload: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, recover=False
    )
    return etree.fromstring(payload, parser=parser)


def _read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        raise PresentationError("یکی از بخش‌های فایل ارائه بیش از حد بزرگ است.")
    with archive.open(info) as handle:
        return handle.read(limit + 1)[:limit]


def _relationships(archive: zipfile.ZipFile, part_name: str) -> list[tuple[str, str, str]]:
    """Return (id, type, internal_target) triples; external targets are blank."""
    payload = _read_member(archive, _rels_part_for(part_name), 8_000_000)
    if not payload:
        return []
    try:
        root = _parse_xml(payload)
    except etree.XMLSyntaxError:
        logger.warning("Malformed relationship part: %s", part_name)
        return []
    result = []
    for element in root.findall(f"{{{RELS_NS}}}Relationship"):
        rel_id = element.get("Id") or ""
        rel_type = element.get("Type") or ""
        target = element.get("Target") or ""
        mode = element.get("TargetMode") or "Internal"
        if rel_id and target:
            result.append((rel_id, rel_type, target if mode == "Internal" else ""))
    return result


def _slide_parts_in_order(archive: zipfile.ZipFile) -> list[str]:
    """Slide part names following the deck's own slide order."""
    payload = _read_member(archive, "ppt/presentation.xml", 20_000_000)
    # Built once: ``namelist()`` rebuilds a list on every call, which turns the
    # membership test below into a quadratic scan on large decks.
    names = set(archive.namelist())
    rels = {
        rel_id: _resolve_target("ppt/presentation.xml", target)
        for rel_id, _type, target in _relationships(archive, "ppt/presentation.xml")
        if target
    }
    ordered: list[str] = []
    if payload:
        try:
            root = _parse_xml(payload)
        except etree.XMLSyntaxError as exc:
            raise PresentationError("ساختار XML فایل ارائه معتبر نیست.") from exc
        for node in root.iterfind(f".//{{{PML_NS}}}sldIdLst/{{{PML_NS}}}sldId"):
            target = rels.get(node.get(f"{{{OFFICE_REL_NS}}}id") or "")
            if target and target in names and target not in ordered:
                ordered.append(target)
    if ordered:
        return ordered
    # Fall back to numeric filename ordering when the deck omits sldIdLst.
    def slide_index(name: str) -> tuple[int, str]:
        match = re.search(r"slide(\d+)\.xml$", name)
        return (int(match.group(1)) if match else 1 << 30, name)

    return sorted(
        (n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=slide_index,
    )


def natural_key(name: str) -> tuple:
    """Sort media2.m4a before media10.m4a instead of lexicographically."""
    # The empty pieces are kept on purpose: re.split alternates text/number, so
    # every tuple position keeps one type and comparisons stay well defined.
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name)
    )


def _safe_media_filename(order: int, part_name: str) -> str:
    base = posixpath.basename(part_name) or "media"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)[-64:].lstrip(".") or "media"
    return f"{order:04d}-{cleaned}"


def _validate_archive(archive: zipfile.ZipFile, settings: Settings) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise PresentationError("تعداد بخش‌های داخل فایل ارائه بیش از حد مجاز است.")
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or PurePosixPath(name).is_absolute():
            raise PresentationError("فایل ارائه شامل مسیر نامعتبر است و پردازش نشد.")
        total += info.file_size
        if total > settings.presentation_max_unpacked_bytes:
            raise PresentationError("حجم بازشدهٔ فایل ارائه از حد مجاز بیشتر است.")


def _extract_slide_text(path: Path) -> list[SlideText]:
    """Read titles, bullet text, tables and speaker notes with python-pptx."""
    try:
        # Presentation() rejects slideshow/template main content types even
        # though their slide structure is identical. Register the two missing
        # macro-enabled variants and open the package directly (no macros run).
        from pptx.opc.package import PartFactory
        from pptx.package import Package
        from pptx.parts.presentation import PresentationPart
    except ImportError:  # pragma: no cover - dependency is declared in requirements
        logger.warning("python-pptx is not installed; slide text will be skipped")
        return []
    try:
        main_types = {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
            "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
            "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
            "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
            "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
        }
        for content_type in main_types:
            PartFactory.part_type_for.setdefault(content_type, PresentationPart)
        main_part = Package.open(str(path)).main_document_part
        if main_part.content_type not in main_types:
            raise PresentationError("نوع فایل ارائه پشتیبانی نمی‌شود.")
        deck = main_part.presentation
    except Exception:
        logger.exception("python-pptx could not open the deck; continuing without slide text")
        return []

    def shape_texts(shapes) -> list[str]:
        collected: list[str] = []
        for shape in shapes:
            try:
                if getattr(shape, "shape_type", None) is not None and shape.shape_type == 6:
                    collected.extend(shape_texts(shape.shapes))  # grouped shapes
                    continue
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        line = " | ".join(cell for cell in cells if cell)
                        if line:
                            collected.append(line)
                    continue
                if getattr(shape, "has_text_frame", False):
                    for paragraph in shape.text_frame.paragraphs:
                        line = "".join(run.text for run in paragraph.runs).strip()
                        if line:
                            collected.append(line)
            except Exception:  # a single broken shape must not lose the deck
                logger.debug("Unreadable shape skipped", exc_info=True)
        return collected

    slides: list[SlideText] = []
    for number, slide in enumerate(deck.slides, start=1):
        title = ""
        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = slide.shapes.title.text.strip()
        except Exception:
            logger.debug("Slide %s has no readable title", number, exc_info=True)
        body = [line for line in shape_texts(slide.shapes) if line and line != title]
        notes = ""
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text.strip()
        except Exception:
            logger.debug("Slide %s has no readable notes", number, exc_info=True)
        slides.append(SlideText(number, title, tuple(dict.fromkeys(body)), notes))
    return slides


def read_presentation(
    path: Path, media_dir: Path, settings: Settings
) -> PresentationContent:
    """Extract media clips and slide text from a .pptx package (blocking)."""
    if not zipfile.is_zipfile(path):
        raise PresentationError(
            "این فایل یک ارائهٔ معتبر PowerPoint نیست یا آسیب دیده است."
        )
    media_dir.mkdir(parents=True, exist_ok=True)
    clips: list[MediaClip] = []
    skipped: list[str] = []
    handled: set[str] = set()

    with zipfile.ZipFile(path) as archive:
        _validate_archive(archive, settings)
        names = set(archive.namelist())
        slide_parts = _slide_parts_in_order(archive)
        ordered_media: list[tuple[int | None, str]] = []
        for slide_number, slide_part in enumerate(slide_parts, start=1):
            for _rel_id, rel_type, target in _relationships(archive, slide_part):
                if not target or rel_type not in MEDIA_REL_TYPES:
                    continue
                resolved = _resolve_target(slide_part, target)
                if resolved in names:
                    ordered_media.append((slide_number, resolved))
        # Media that no slide references (e.g. audio attached to a layout).
        for name in sorted(names, key=natural_key):
            if name.startswith("ppt/media/") and media_kind(name):
                ordered_media.append((None, name))

        for slide_number, part_name in ordered_media:
            if part_name in handled:
                continue
            handled.add(part_name)
            kind = media_kind(part_name)
            if kind is None:
                skipped.append(f"{posixpath.basename(part_name)}: قالب پشتیبانی‌نشده")
                continue
            if kind == "video" and not settings.presentation_include_video_audio:
                skipped.append(f"{posixpath.basename(part_name)}: ویدیو نادیده گرفته شد")
                continue
            if len(clips) >= settings.presentation_max_clips:
                skipped.append("تعداد فایل‌های رسانه‌ای بیش از حد مجاز بود")
                break
            order = len(clips) + 1
            destination = media_dir / _safe_media_filename(order, part_name)
            info = archive.getinfo(part_name)
            if info.file_size > settings.max_file_size:
                skipped.append(f"{posixpath.basename(part_name)}: حجم بیش از حد مجاز")
                continue
            with archive.open(info) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            clips.append(MediaClip(order, slide_number, part_name, destination, kind))

    slides = _extract_slide_text(path) if settings.presentation_include_slide_text else []
    return PresentationContent(tuple(slides), tuple(clips), tuple(skipped), len(slide_parts))


async def load_presentation(
    path: Path, media_dir: Path, settings: Settings
) -> PresentationContent:
    """Async wrapper so ZIP/XML work never blocks the Telethon event loop."""
    # Cancelling to_thread does not stop its worker. Join it before the caller
    # removes the job directory, otherwise extraction can recreate private files.
    worker = asyncio.create_task(asyncio.to_thread(read_presentation, path, media_dir, settings))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except Exception:
            logger.warning("Presentation extraction failed during shutdown", exc_info=True)
        raise


async def prepare_audio(
    content: PresentationContent,
    workdir: Path,
    settings: Settings,
    *,
    skipped_out: list[str] | None = None,
) -> PreparedAudio | None:
    """Probe the extracted clips and merge them into one track for the STT stage.

    ``skipped_out`` receives the skip reasons even when the deck ends up with no
    usable audio at all, so the caller can still explain what was dropped.
    """
    skipped = list(content.skipped)
    if skipped_out is not None:
        skipped_out.clear()
        skipped_out.extend(skipped)
    if not content.clips:
        return None
    usable: list[MediaClip] = []
    can_probe = tool_available(settings.ffprobe_bin)
    if not can_probe:
        logger.warning("ffprobe is unavailable; clip filtering falls back to extensions")
    for clip in content.clips:
        if not can_probe:
            if clip.kind == "video":
                skipped.append(f"{clip.label}: بدون ffprobe نمی‌توان صدای ویدیو را بررسی کرد")
                continue
            usable.append(clip)
            continue
        try:
            info = await probe_media(clip.path, settings)
        except MediaToolError as exc:
            logger.warning("Probing %s failed: %s", clip.part_name, exc)
            skipped.append(f"{clip.label}: بررسی فایل ناموفق بود")
            continue
        if not info.has_audio:
            skipped.append(f"{clip.label}: بدون شاخهٔ صوتی")
            continue
        if (
            info.duration is not None
            and info.duration < settings.presentation_min_clip_seconds
        ):
            skipped.append(f"{clip.label}: کوتاه‌تر از حد تعیین‌شده")
            continue
        usable.append(replace(clip, duration=info.duration))

    if skipped_out is not None:
        skipped_out.clear()
        skipped_out.extend(skipped)
    if not usable:
        logger.info("No usable narration in the deck skipped=%s", len(skipped))
        return None

    durations = [clip.duration for clip in usable if clip.duration is not None]
    total_duration = sum(durations) if len(durations) == len(usable) else None
    if sum(durations) > settings.presentation_max_total_duration:
        raise PresentationError(
            "مجموع مدت صداهای این ارائه از سقف تعیین‌شدهٔ ربات بیشتر است."
        )

    if len(usable) == 1 and usable[0].kind == "audio" and not tool_available(settings.ffmpeg_bin):
        # A single narration track needs no ffmpeg; send it to the STT engine as is.
        clip = usable[0]
        return PreparedAudio(clip.path, (clip,), clip.duration, False, tuple(skipped))
    if not tool_available(settings.ffmpeg_bin):
        raise PresentationError(
            "برای ادغام صداهای این ارائه، ffmpeg باید روی سرور نصب باشد."
        )

    merged = await merge_audio_tracks(
        [clip.path for clip in usable],
        workdir,
        settings,
        total_duration=(
            total_duration + settings.presentation_silence_seconds * (len(usable) - 1)
            if total_duration is not None else None
        ),
        stem="presentation-audio",
    )
    return PreparedAudio(merged, tuple(usable), total_duration, True, tuple(skipped))


def slides_outline(slides: tuple[SlideText, ...] | list[SlideText], limit: int | None = None) -> str:
    """Render all slide material; an explicit limit is only for previews."""
    blocks = [slide.as_markdown() for slide in slides if not slide.is_empty]
    outline = "\n\n".join(blocks)
    if limit is not None and len(outline) > limit:
        outline = outline[:limit].rsplit("\n", 1)[0] + "\n\n(ادامهٔ متن اسلایدها کوتاه شد)"
    return outline
