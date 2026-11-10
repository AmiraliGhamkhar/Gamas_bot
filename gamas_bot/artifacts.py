"""Deliverables are fetched, not pushed: the results screen and its buttons.

The pipeline used to end by pushing the raw transcript file, a chat preview of it
and then the booklet. That made the chat a log instead of a workspace, and it
made a transcript that arrived *after* the booklet read like a second,
contradictory answer.

Now a finished job ends with exactly one message: what exists, who spoke, and a
button per artifact. Nothing is sent unasked.

Where the bytes come from is the only subtle part, and it differs per artifact
because the costs differ:

* **Transcript text** — regenerated from the stored transcription row. It is a
  pure function of bytes already in the database (paragraphing plus the chosen
  profile), so the tenth fetch is identical to the first, needs no model call,
  and costs nothing. Regenerating is also what makes the button work months
  later, when a temporary work directory is long gone.
* **Key points** — read from the stored ``notes_json``. Same argument: the notes
  are already persisted, so the list is free and stable.
* **Booklet PDF / DOCX** — rendered once from the validated stored-note model
  at completion and persisted as owner-scoped binary artifacts. Fetch callbacks
  only send those exact bytes: they never invoke a renderer or a model.
* **Mini App editor** — links to Gamas Studio; the editor exports the latest
  saved version.

Everything here is deterministic and side-effect free, which is why it is a
module instead of another method on the pipeline.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from . import texts

logger = logging.getLogger(__name__)

#: The artifacts a finished submission can offer. ``txt`` is the plain-text
#: *file* download; ``transcript`` is the same text rendered in chat. They are
#: independent buttons because a student may want to read it now or save it.
KIND_BOOKLET = "booklet"   # PDF (primary student deliverable)
KIND_DOCX = "docx"         # editable Word version
KIND_TRANSCRIPT = "transcript"
KIND_TXT = "txt"
KIND_KEYPOINTS = "keypoints"
KIND_QUIZ = "quiz"
KIND_FLASHCARDS = "flashcards"
KINDS = (
    KIND_BOOKLET,
    KIND_DOCX,
    KIND_TRANSCRIPT,
    KIND_TXT,
    KIND_KEYPOINTS,
    KIND_QUIZ,
    KIND_FLASHCARDS,
)

#: Prefix of a fetch callback (``art:GMS-12:transcript``).
CALLBACK_PREFIX = "art:"


def reference_for(submission_id: int) -> str:
    """The conversation reference used in copy, captions and callbacks."""
    return f"GMS-{int(submission_id)}"


def callback_for(reference: str, kind: str) -> bytes:
    """The callback payload for one artifact button."""
    if kind not in KINDS:
        raise ValueError(f"unknown artifact kind {kind!r}")
    return f"{CALLBACK_PREFIX}{reference}:{kind}".encode("ascii")


def parse_callback(data: str) -> tuple[str, str] | None:
    """``art:GMS-12:transcript`` → ``("GMS-12", "transcript")``; else ``None``."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    parts = data[len(CALLBACK_PREFIX) :].split(":")
    if len(parts) != 2:
        return None
    reference, kind = parts[0].strip(), parts[1].strip()
    digits = reference[4:] if reference.startswith("GMS-") else reference
    if not digits.isdigit() or kind not in KINDS:
        return None
    return reference, kind


def submission_from_callback(data: str) -> int | None:
    parsed = parse_callback(data)
    if parsed is None:
        return None
    reference = parsed[0]
    digits = reference[4:] if reference.startswith("GMS-") else reference
    return int(digits)


def key_points_of(notes_json: str | None) -> list[str]:
    """Read the key points out of a stored notes document.

    The stored blob is whatever the notes model produced; anything unreadable is
    reported as "no key points" rather than raised, because a corrupt notes
    document must not break a delivery button that is otherwise fine.
    """
    if not notes_json:
        return []
    try:
        payload = json.loads(notes_json)
    except (TypeError, ValueError):
        logger.warning("stored notes are not readable JSON; key points unavailable")
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("key_points")
    if raw is None:
        raw = payload.get("keyPoints")
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, dict)):
        return []
    points = [str(item).strip() for item in raw]
    return [point for point in points if point]


async def transcript_artifact(db: Any, submission_id: int, profile: str) -> str | None:
    """Rebuild the raw transcript text for one submission, or ``None``.

    Rebuilding — instead of keeping a copy — is deliberate: the transcript is a
    deterministic transformation of a row that is already persisted, so the
    copy would be a second source of truth that can drift. It also means the
    button keeps working after the job's scratch directory is deleted.
    """
    from .transcript import Paragraph, TranscriptDocument, render

    row = await db.transcription(int(submission_id))
    if not row:
        return None
    text = str(row.get("text") or "").strip()
    if not text:
        return None
    # The stored text is already cleaned and paragraphed; re-paragraphing it
    # directly (rather than re-running the paragrapher over words, which are not
    # stored) reproduces the delivered body deterministically.
    paragraphs = [
        Paragraph(text=part.strip())
        for part in text.split("\n\n")
        if part.strip()
    ]
    document = TranscriptDocument(tuple(paragraphs))
    return render(document, profile)


async def keypoints_artifact(db: Any, submission_id: int) -> str | None:
    row = await db.transcription(int(submission_id))
    if not row:
        return None
    points = key_points_of(row.get("notes_json"))
    if not points:
        return None
    return texts.keypoints_text(
        reference=reference_for(submission_id), points=points
    )


def delivery_markup(
    *,
    reference: str,
    speakers: int,
    has_booklet: bool,
    kinds: set[str],
    item_id: int | None = None,
    mini_app_url: str = "",
):
    """One completion screen, one button per independently available artifact.

    Button contract (per engineering addendum):

    * 📘 دریافت جزوه PDF  — primary booklet (PDF)
    * 📝 دریافت متن خام
    * ⭐ دریافت نکات کلیدی
    * ✏️ ویرایش جزوه در Gamas Studio  (when a Mini App URL is configured)
    * 📄 دریافت نسخه Word  (when the DOCX artifact is available)

    Nothing appears here unless it really exists: ``kinds`` comes from the
    central submission artifact registry. Every button retrieves a persisted
    artifact and is free/idempotent — no button triggers transcription or
    booklet regeneration.
    """
    from telethon import Button

    rows: list[list[Any]] = []
    # PDF is the primary student deliverable; it gets its own row.
    if has_booklet and KIND_BOOKLET in kinds:
        rows.append(
            [Button.inline(texts.BTN_GET_BOOKLET, callback_for(reference, KIND_BOOKLET))]
        )
    # Second row: transcript, txt, key points
    row: list[Any] = []
    if KIND_TRANSCRIPT in kinds:
        row.append(
            Button.inline(
                texts.BTN_GET_TRANSCRIPT, callback_for(reference, KIND_TRANSCRIPT)
            )
        )
    if KIND_KEYPOINTS in kinds:
        row.append(
            Button.inline(
                texts.BTN_GET_KEYPOINTS, callback_for(reference, KIND_KEYPOINTS)
            )
        )
    if KIND_TXT in kinds:
        row.append(
            Button.inline(texts.BTN_DOWNLOAD_TXT, callback_for(reference, KIND_TXT))
        )
    if row:
        rows.append(row)
    # DOCX is independently persisted and is offered whenever it exists.
    if KIND_DOCX in kinds:
        rows.append(
            [Button.inline(texts.BTN_GET_DOCX, callback_for(reference, KIND_DOCX))]
        )
    # Studio button (Web App)
    if mini_app_url and has_booklet:
        rows.append([texts.mini_app_button(texts.BTN_EDIT_IN_APP, mini_app_url)])
    # Learning buttons (quiz / flashcards)
    learning: list[Any] = []
    if item_id and KIND_QUIZ in kinds:
        learning.append(
            Button.inline(
                texts.BTN_QUIZ_FROM_BOOKLET,
                f"quiz:new:{int(item_id)}".encode("ascii"),
            )
        )
    if item_id and KIND_FLASHCARDS in kinds:
        learning.append(
            Button.inline(
                texts.BTN_CARDS_FROM_BOOKLET,
                f"fc:ai:{int(item_id)}".encode("ascii"),
            )
        )
    if learning:
        rows.append(learning)
    rows.append([Button.inline(texts.BTN_BACK_MAIN, b"menu:main")])
    return rows
