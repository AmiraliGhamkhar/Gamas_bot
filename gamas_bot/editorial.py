"""Global lecture context and the final editorial compilation pass.

The note pipeline writes one strict-JSON answer *per chunk*. That is what makes
long lectures affordable, and it is also why a naive pipeline produces a pile of
independently written mini-documents: duplicated introductions, drifting
terminology, repeated "key points", weak transitions and an outline that is
just the concatenation of chunk headings.

This module supplies the smallest layer that fixes that without embeddings, a
vector store or an agent framework:

* :class:`LectureContext` — a compact, cheap-to-produce orientation for the
  whole lecture (title, ordered topic list, key terminology) derived from the
  *beginnings* of every chunk;
* :func:`build_context_block` — the piece of that context each chunk receives,
  including a short window of the previous and next chunk so a boundary
  sentence can be continued instead of restarted;
* :func:`compact_notes_payload` + :func:`build_compile_document` — the input of
  the one controlled final editorial pass, which merges logically related
  fragments, removes accidental duplication and repairs transitions;
* :func:`compile_is_better` — the deterministic acceptance gate: a compilation
  that loses a measured number, term or content unit is rejected and the
  unmodified merge is delivered instead.

Nothing here talks to a provider; :mod:`gamas_bot.structuring` owns the calls
and injects them, which keeps the provider abstraction in exactly one place.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .structuring import StructuredNotes  # noqa: F401  (typing only)

#: How much of each chunk the orientation pass reads. The beginning of a chunk
#: states its topic; the tail only disambiguates a topic that starts late.
OUTLINE_CHUNK_HEAD_CHARS = 220
OUTLINE_CHUNK_TAIL_CHARS = 80

#: Hard bound on the orientation document, so one huge lecture cannot turn the
#: cheapest call into an expensive one.
OUTLINE_DOCUMENT_MAX_CHARS = 12000

#: Neighbour window handed to a chunk as *context only* (never to be re-noted).
NEIGHBOUR_CONTEXT_CHARS = 260

#: Bounds on what is kept from the orientation answer.
MAX_OUTLINE_TOPICS = 40
MAX_TERMINOLOGY = 40
OUTLINE_ITEM_CHARS = 200

#: A compilation must keep at least this share of the merged note text.
#: Restructuring may tighten prose, but gutting the booklet is never accepted.
COMPILE_MIN_KEEP_RATIO = 0.85


@dataclass(frozen=True, slots=True)
class LectureContext:
    """A compact orientation for one lecture (title, topics, terminology)."""

    title: str = ""
    topics: tuple[str, ...] = ()
    terminology: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.topics or self.terminology)


#: The orientation answer is deliberately tiny and strictly sourced: it may
#: only restate what the chunk openings already say.
OUTLINE_SYSTEM_PROMPT = (
    "شما ویراستار آموزشی «گاماس» هستید. در ورودی، «آغاز چند بخش پشت‌سرهم از یک درس» آمده است. "
    "خروجی شما فقط و فقط یک شیء JSON معتبر است با این ساختار:\n"
    "{\n"
    '  "title": "عنوان کوتاه و گویای کل درس",\n'
    '  "topics": ["موضوع بخش اول", "موضوع بخش دوم", "..."],\n'
    '  "terminology": ["اصطلاح کلیدی ۱", "Term 2", "..."]\n'
    "}\n"
    "قواعد:\n"
    "- فقط از همین متن استخراج کنید؛ هیچ موضوع، عنوان یا اصطلاحی از خودتان نسازید.\n"
    "- ترتیب «topics» دقیقاً همان ترتیب بخش‌های ورودی باشد و برای هر بخش یک موضوع بنویسید.\n"
    "- هر موضوع حداکثر ۱۲ واژه و بدون توضیح اضافه باشد.\n"
    "- «terminology» اصطلاح‌های فنی، پزشکی یا انگلیسی کلیدی متن است؛ آن‌ها را به همان صورت اصلی (انگلیسی) بنویسید.\n"
    "- هیچ متن اضافه، توضیح یا خنده‌کدی بیرون از JSON ننویسید.\n"
)


def build_outline_document(chunks: list[str], *, max_chars: int = OUTLINE_DOCUMENT_MAX_CHARS) -> str:
    """A compact digest of a whole lecture: the opening of every chunk.

    The digest is deterministic and small (bounded by ``max_chars``), so the
    orientation call is cheap even for a multi-hour lecture and never needs the
    full transcript again.
    """
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        text = chunk.strip()
        if not text:
            continue
        head = text[:OUTLINE_CHUNK_HEAD_CHARS]
        tail = text[-OUTLINE_CHUNK_TAIL_CHARS:] if len(text) > OUTLINE_CHUNK_HEAD_CHARS * 2 else ""
        entry = f"### بخش {index}\n{head}"
        if tail:
            entry += f" … {tail}"
        parts.append(entry)
    document = "\n\n".join(parts)
    if len(document) > max_chars:
        document = document[:max_chars]
    return document


def _clean_items(value: object, *, limit: int, item_chars: int) -> tuple[str, ...]:
    """Non-empty, bounded, de-duplicated strings from a model-supplied list."""
    if not isinstance(value, (list, tuple)):
        return ()
    seen: set[str] = set()
    items: list[str] = []
    for raw in value:
        text = re.sub(r"\s+", " ", str(raw)).strip()[:item_chars]
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
        if len(items) >= limit:
            break
    return tuple(items)


def _json_object(text: str) -> dict | None:
    """Best-effort JSON object extraction from a model answer."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL)
    if fence and "{" in fence.group(1):
        candidate = fence.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    fragment = candidate[start : end + 1]
    for attempt in (fragment, re.sub(r",\s*([}\]])", r"\1", fragment)):
        try:
            payload = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def parse_outline(text: str) -> "LectureContext | None":
    """Parse the orientation answer, tolerating a notes-shaped fallback answer.

    A model (or a test double) that answers with ordinary note JSON still
    yields a usable context: the section headings become the topic list. That
    keeps the pipeline degrading gracefully instead of dropping global context.
    """
    payload = _json_object(text)
    if payload is None:
        return None
    title = re.sub(r"\s+", " ", str(payload.get("title") or "")).strip()[:OUTLINE_ITEM_CHARS]
    topics = _clean_items(payload.get("topics"), limit=MAX_OUTLINE_TOPICS, item_chars=OUTLINE_ITEM_CHARS)
    terminology = _clean_items(
        payload.get("terminology"), limit=MAX_TERMINOLOGY, item_chars=OUTLINE_ITEM_CHARS
    )
    if not topics:
        sections = payload.get("sections")
        if isinstance(sections, (list, tuple)):
            topics = _clean_items(
                [
                    section.get("heading")
                    for section in sections
                    if isinstance(section, dict) and section.get("heading")
                ],
                limit=MAX_OUTLINE_TOPICS,
                item_chars=OUTLINE_ITEM_CHARS,
            )
    context = LectureContext(title=title, topics=topics, terminology=terminology)
    return None if context.is_empty else context


def build_context_block(
    context: LectureContext,
    *,
    index: int,
    total: int,
    previous_tail: str = "",
    next_head: str = "",
    previous_headings: tuple[str, ...] = (),
) -> str:
    """The global-context block injected into one chunk's system prompt.

    This is context, not payload: it tells the model where it is in the lecture
    and how the neighbours end and begin, without asking it to note any of that
    text again. The wording says so explicitly, so the same sentence cannot be
    summarized twice.

    ``previous_headings`` are the *already written* section headings of the
    part before this one. They are the cheapest possible defence against a
    topic being printed twice: the model is told the exact wording in use and
    asked to reuse it when its first section continues that topic. The merge
    recognises the same situation deterministically, so this only makes the
    common case produce the right heading in the first place.
    """
    lines: list[str] = [
        "### زمینهٔ کلی درس (فقط برای هماهنگی — این متن را دوباره جزوه نکنید)",
    ]
    if context.title:
        lines.append(f"عنوان درس: {context.title}")
    if context.topics:
        numbered = "، ".join(
            f"({_persian_number(position)}) {topic}"
            for position, topic in enumerate(context.topics, start=1)
        )
        lines.append("موضوع‌های درس به ترتیب: " + numbered)
    if context.terminology:
        lines.append("اصطلاح‌های کلیدی درس: " + "، ".join(context.terminology))
    lines.append(
        f"جایگاه این بخش: بخش {_persian_number(index)} از {_persian_number(total)} درس."
    )
    if previous_headings:
        listed = "، ".join(f"«{heading}»" for heading in previous_headings)
        lines.append(f"عنوان‌های بخش پیشین که همین حالا نوشته شده‌اند: {listed}")
        lines.append(
            "اگر موضوع این بخش ادامهٔ همان موضوع است، عیناً همان عنوان را برای بخش خود "
            "به کار ببرید (کلمهٔ «ادامه» یا عنوان تازه اضافه نکنید) تا در جزوهٔ نهایی یک "
            "بخش یکپارچه شود؛ فقط اگر موضوع واقعاً تازه است عنوان تازه بسازید."
        )
    if previous_tail:
        lines.append("چند جملهٔ پایانی بخش پیشین (فقط برای پیوند): … " + previous_tail.strip())
    if next_head:
        lines.append("چند جملهٔ آغاز بخش بعدی (فقط برای پیوند): " + next_head.strip() + " …")
    return "\n".join(lines)


def context_block_for(
    context: LectureContext,
    documents: list[str],
    index: int,
    *,
    previous_headings: tuple[str, ...] = (),
) -> str:
    """Convenience wrapper: the context block for part ``index`` (1-based).

    The neighbour windows are read from the neighbouring parts themselves, so
    a boundary sentence can be continued rather than restarted.
    """
    previous_tail = ""
    next_head = ""
    if index > 1 and index - 2 < len(documents):
        previous_tail = documents[index - 2].strip()[-NEIGHBOUR_CONTEXT_CHARS:].strip()
    if index < len(documents):
        next_head = documents[index].strip()[:NEIGHBOUR_CONTEXT_CHARS].strip()
    return build_context_block(
        context,
        index=index,
        total=len(documents),
        previous_tail=previous_tail,
        next_head=next_head,
        previous_headings=previous_headings,
    )


_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _persian_number(value: int) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


#: The per-chunk contract when a lecture is split. It is the difference between
#: "write me notes for this text" and "write part 3 of one lecture".
CHUNKED_CONTENT_RULES = (
    "\nقواعد نوشتن یک «بخش» از یک درس بلند:\n"
    "- شما فقط همین بخش را می‌نویسید؛ بخش‌های دیگر را ویرایشگران دیگر می‌نویسند. "
    "فهرست موضوع‌های بالا نقشهٔ راه است، نه متنی که باید بازنویسی شود.\n"
    "- مقدمه، معرفی و جمع‌بندی کل درس را دوباره ننویسید؛ مستقیم سر موضوع همین بخش بروید.\n"
    "- اگر این بخش وسط یک موضوعِ نیمه‌تمام شروع می‌شود (متن با ادامهٔ توضیح قبلی آغاز شده)، "
    "اولین بخش خود را با عیناً همان عنوان بخش پیشین بنویسید — نه عنوان تازه و نه عنوان با "
    "کلمهٔ «ادامه» — چون جزوهٔ نهایی این دو را یک بخش می‌داند.\n"
    "- کلیدهای «summary» و «key_points» و «glossary» سطح کل درس را فقط زمانی پر کنید که درس تک‌بخشی است؛ "
    "در درس چندبخشی، نکته‌های کلیدی را در «key_points» همان بخش بگذارید و بقیه را خالی بگذارید.\n"
    "- اگر همین بخش موضوع تازه‌ای را شروع می‌کند، عنوان بخش را از «موضوع‌های درس» بردارید تا با بقیهٔ جزوه یکی باشد.\n"
    "- انتهای بخش را طوری بنویسید که به موضوع بعدی وصل شود (بدون افزودن ادعای تازه)؛ مثال: "
    "«در ادامه می‌بینیم که …» فقط اگر موضوع بعدی در فهرست آمده باشد.\n"
    "- اصطلاح‌های تخصصی و نام‌های انگلیسی را عیناً مثل بقیهٔ بخش‌ها بنویسید.\n"
)


#: The one controlled editorial pass. It compiles; it does not re-summarize.
COMPILE_SYSTEM_PROMPT = (
    "شما ویراستار نهایی جزوهٔ فارسی «گاماس» هستید. ورودی شما «زمینهٔ کلی درس» و «جزوهٔ فعلی» "
    "به شکل JSON است؛ جزوهٔ فعلی از چند بخش جداگانه ساخته شده و ممکن است ناپیوسته یا تکراری باشد. "
    "خروجی شما یک جزوهٔ واحد و روان است، نه خلاصه‌ای از جزوه.\n\n"
    "کار شما:\n"
    "- بخش‌هایی که به یک موضوع واحد تعلق دارند را در یک بخش منسجم ادغام کنید؛ ترتیب منطقی متن را نگه دارید.\n"
    "- عنوان بخش‌ها را از «موضوع‌های درس» بردارید و برای هر موضوع، دقیقاً یک بخش بسازید: اگر محتوای یک "
    "موضوع زیر دو عنوان متفاوت پخش شده، آن‌ها را زیر عنوان همان موضوع جمع کنید. عنوان تازه‌ای که در "
    "«موضوع‌های درس» نیست، فقط برای موضوعی به کار ببرید که واقعاً در جزوه هست و در فهرست نیامده است.\n"
    "- تکرارهای عینی و تیترهای تکراری را یک‌بار بنویسید، ولی هیچ توضیح، تعریف، مثال، مرحله، مقایسه، هشدار، "
    "استثنا، فرمول، عدد، واحد، تاریخ یا اصطلاح تازه‌ای را به‌خاطر کوتاه‌شدن حذف نکنید. دو جمله که یک "
    "مفهوم را با کلمات متفاوت می‌گویند، تکرار عینی نیستند و هر دو می‌مانند؛ فقط تکرار لفظ‌به‌لفظ را یکی کنید.\n"
    "- گذارهای طبیعی را فقط بر پایهٔ روابطی که در متن هست بازسازی کنید (زیرا، بنابراین، در مقابل، برای نمونه، "
    "نخست/سپس/در پایان). ادعای تازه نسازید.\n"
    "- به‌جای جمله‌های بریده‌بریده و فهرست‌های بی‌جا، پاراگراف‌های کامل و روان بنویسید؛ فهرست را فقط برای "
    "مواردی نگه دارید که واقعاً شمارشی‌اند.\n"
    "- یک صورت یکدست برای هر اصطلاح انتخاب کنید و در همه‌جا همان را بنویسید.\n"
    "- تعریف‌ها را در definitions، مثال‌ها را در examples، مراحل را در steps، فرمول‌ها را در formulas، "
    "جدول‌ها را در table و هشدارها را در callouts نگه دارید.\n"
    "- خلاصهٔ کل درس را در summary و مهم‌ترین نکته‌های کل درس را در key_points بنویسید.\n"
    "- فقط اگر همین متن مدرک روشنی برای «اهداف یادگیری» یا «پرسش‌های مرور» دارد، آن‌ها را کوتاه و مستقیماً "
    "برگرفته از متن اضافه کنید؛ در غیر این صورت این دو کلید را نیاورید. هیچ هدف یا پرسش تازه‌ای نسازید.\n"
    "- اطلاعات تازه اضافه نکنید. اگر چیزی در ورودی نیست، حدس نزنید.\n\n"
    "ساختار خروجی — فقط یک شیء JSON معتبر، بدون هیچ متن اضافه:\n"
    "{\n"
    '  "title": "عنوان جزوه",\n'
    '  "summary": "خلاصهٔ چند جمله‌ای کل درس",\n'
    '  "learning_objectives": ["هدف ۱", "هدف ۲"],\n'
    '  "sections": [\n'
    "    {\n"
    '      "heading": "عنوان بخش",\n'
    '      "paragraphs": ["پاراگراف توضیحی کامل"],\n'
    '      "bullets": ["مورد فهرستی"],\n'
    '      "definitions": [{"term": "اصطلاح", "term_en": "English term", "definition": "تعریف"}],\n'
    '      "examples": ["مثال کامل"],\n'
    '      "steps": ["گام ۱ …", "گام ۲ …"],\n'
    '      "formulas": ["صورت دقیق فرمول"],\n'
    '      "key_points": ["نکتهٔ کلیدی همین بخش"],\n'
    '      "table": {"headers": ["ستون ۱"], "rows": [["مقدار"]]},\n'
    '      "callouts": [{"kind": "نکته", "text": "متن برجسته"}]\n'
    "    }\n"
    "  ],\n"
    '  "key_points": ["نکتهٔ کلیدی کل جزوه"],\n'
    '  "review_questions": ["پرسش مرور برگرفته از متن"],\n'
    '  "glossary": [{"term": "اصطلاح", "definition": "تعریف کوتاه"}]\n'
    "}\n"
    "قواعد JSON: نقل‌قول دوگانه، بدون کامای اضافه، بدون خنده‌کد، و «sections» هرگز خالی نباشد. "
    "در callouts مقدار kind فقط «نکته»، «هشدار» یا «یادآوری» است."
)


def compact_notes_payload(notes: StructuredNotes) -> dict:
    """The notes as a compact payload: empty fields are omitted.

    Sending only what exists keeps the compilation input small and, more
    importantly, makes the compiler's job unambiguous — an empty list is not a
    hint that something should be invented.
    """
    payload: dict = {}
    if notes.title:
        payload["title"] = notes.title
    if notes.summary:
        payload["summary"] = notes.summary
    if notes.learning_objectives:
        payload["learning_objectives"] = list(notes.learning_objectives)
    sections: list[dict] = []
    for section in notes.sections:
        item: dict = {"heading": section.heading}
        if section.paragraphs:
            item["paragraphs"] = list(section.paragraphs)
        if section.bullets:
            item["bullets"] = list(section.bullets)
        if section.definitions:
            item["definitions"] = [
                {"term": d.term, "term_en": d.term_en, "definition": d.definition}
                for d in section.definitions
            ]
        if section.examples:
            item["examples"] = list(section.examples)
        if section.steps:
            item["steps"] = list(section.steps)
        if section.formulas:
            item["formulas"] = list(section.formulas)
        if section.key_points:
            item["key_points"] = list(section.key_points)
        if section.table is not None:
            item["table"] = {"headers": list(section.table.headers), "rows": [list(r) for r in section.table.rows]}
        if section.callouts:
            item["callouts"] = [{"kind": c.kind, "text": c.text} for c in section.callouts]
        sections.append(item)
    payload["sections"] = sections
    if notes.key_points:
        payload["key_points"] = list(notes.key_points)
    if notes.review_questions:
        payload["review_questions"] = list(notes.review_questions)
    if notes.glossary:
        payload["glossary"] = [{"term": e.term, "definition": e.definition} for e in notes.glossary]
    return payload


def compact_notes_json(notes: StructuredNotes) -> str:
    """``compact_notes_payload`` serialized for the compiler prompt."""
    return json.dumps(compact_notes_payload(notes), ensure_ascii=False)


def build_compile_document(context: LectureContext, notes: StructuredNotes) -> str:
    """The final editorial pass input: global outline + the merged notes."""
    parts: list[str] = []
    if not context.is_empty:
        parts.append("### زمینهٔ کلی درس\n" + _outline_lines(context))
    parts.append("### جزوهٔ فعلی (ساختهٔ بخش‌های جداگانه)\n" + compact_notes_json(notes))
    parts.append(
        "### دستور\n"
        "این بخش‌ها را به یک جزوهٔ واحد و روان تبدیل کنید. هیچ مطلبی را حذف نکنید؛ "
        "فقط تکرارهای لفظ‌به‌لفظ را یک‌بار بنویسید، بخش‌های هم‌موضوع را ادغام کنید، گذارها را درست کنید و "
        "یکدستی اصطلاح‌ها را برقرار کنید.\n"
        "- ترتیب موضوع‌های درس را نگه دارید؛ هر موضوع یک بار و در جای خودش بیاید.\n"
        "- اگر جمله‌ای از بخش‌ها بریده یا ناقص است، آن را با همان اطلاعاتِ همین جزوه کامل و روان کنید؛ "
        "هیچ اطلاع تازه‌ای از خودتان اضافه نکنید.\n"
        "- خروجی فقط JSON با همان ساختار است و «sections» هرگز خالی نمی‌شود."
    )
    return "\n\n".join(parts)


def _outline_lines(context: LectureContext) -> str:
    lines: list[str] = []
    if context.title:
        lines.append("عنوان درس: " + context.title)
    if context.topics:
        lines.append(
            "موضوع‌های درس به ترتیب: "
            + "، ".join(
                f"({_persian_number(position)}) {topic}"
                for position, topic in enumerate(context.topics, start=1)
            )
        )
    if context.terminology:
        lines.append("اصطلاح‌های کلیدی: " + "، ".join(context.terminology))
    return "\n".join(lines)


def compile_is_better(
    before,
    after,
    *,
    before_chars: int,
    after_chars: int,
) -> tuple[bool, str]:
    """Should the compiled notes replace the merged notes?

    The compiled version is accepted only when it is *not measurably worse* on
    the deterministic content measures (semantic coverage, signal coverage,
    missing numbers) and keeps at least :data:`COMPILE_MIN_KEEP_RATIO` of the
    merged text. Style and continuity improve the document, but they are not
    allowed to pay for lost content.
    """
    if after.semantic_coverage < before.semantic_coverage:
        return False, "semantic coverage regressed"
    if after.coverage < before.coverage:
        return False, "signal coverage regressed"
    if len(after.missing_numbers) > len(before.missing_numbers):
        return False, "more numbers went missing"
    if before_chars > 0 and after_chars < before_chars * COMPILE_MIN_KEEP_RATIO:
        return False, (
            f"compiled notes kept only {after_chars / before_chars:.0%} of the merged text"
        )
    return True, "compiled with no measured content loss"
