"""Lightweight integrity checks for final student-facing PDF/DOCX artifacts."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO


def validate_pdf_artifact(content: bytes) -> None:
    """Require a complete, parseable PDF with at least one page."""
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ValueError("renderer returned an invalid PDF envelope")
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if not reader.pages:
            raise ValueError("renderer returned a PDF with no pages")
    except Exception as exc:
        raise ValueError("renderer returned an invalid PDF document") from exc


def validate_docx_artifact(content: bytes) -> None:
    """Require an intact OOXML ZIP with the main Word document parts."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as package:
            required_parts = {"[Content_Types].xml", "word/document.xml"}
            if not required_parts.issubset(package.namelist()) or package.testzip():
                raise ValueError("renderer returned an invalid DOCX package")
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ValueError("renderer returned an invalid DOCX package") from exc


#: Upper bound for one review sheet. The two embedded faces are about 0.35 MB in
#: base64; the rest is the booklet text, so this is generous but finite.
MAX_REVIEW_HTML_BYTES = 4 * 1024 * 1024
REVIEW_MIN_SECTIONS = 5
REVIEW_MAX_SECTIONS = 12
REVIEW_MIN_SELF_TEST_QUESTIONS = 3
#: The template's guidance; fewer questions is a warning, not a rejection.
REVIEW_RECOMMENDED_SELF_TEST_QUESTIONS = 8
#: Words per minute the template budgets for (spec section 0); outside it is a warning.
REVIEW_WORDS_PER_MINUTE = (65, 125)
_ELEMENT_TAGS = frozenset(
    {
        "html", "head", "meta", "title", "style", "script", "body", "header", "main", "footer",
        "section", "div", "span", "p", "h1", "h2", "h3", "ul", "ol", "li", "small", "b", "e",
        "table", "thead", "tbody", "tr", "th", "td", "a", "button", "details", "summary",
        "svg", "g", "polygon", "circle",
    }
)
# HTMLParser lowercases attribute names, so the allowlist is lowercase too.
_SVG_ATTRS = frozenset({"viewbox", "preserveaspectratio", "fill", "stroke", "stroke-width", "aria-hidden",
                        "points", "cx", "cy", "r"})
_ELEMENT_ATTRS: dict[str, frozenset[str]] = {
    "html": frozenset({"lang", "dir"}),
    "meta": frozenset({"charset", "name", "content", "http-equiv"}),
    "section": frozenset({"data-t", "data-n", "style"}),
    "a": frozenset({"href"}),
    "svg": _SVG_ATTRS,
    "g": _SVG_ATTRS,
    "polygon": _SVG_ATTRS,
    "circle": _SVG_ATTRS,
}
_COMMON_ATTRS = frozenset({"class", "id"})
_META_NAMES = frozenset({"viewport", "referrer", "generator"})
_ANCHOR = re.compile(r"#[A-Za-z][A-Za-z0-9_-]*")
#: The only inline style the template's sections may carry: a palette pair.
_SECTION_STYLE = re.compile(
    r"--c:var\(--(?:pri|sec|blu|carb|tl|rx|pk|ink)\);--cs:var\(--(?:(?:pri|sec|blu|carb|tl|rx|pk)-s|line)\)"
)
_MARKUP_CHARS = re.compile(r"[<>&\"'`]")
_EXTERNAL_URL = re.compile(r"url\(\s*['\"]?(?!data:)", re.IGNORECASE)
_NETWORK_REFERENCE = re.compile(r"https?:|//[A-Za-z0-9-]+\.", re.IGNORECASE)
_CSP_DIRECTIVES = frozenset(
    {"default-src", "style-src", "script-src", "font-src", "img-src", "base-uri", "form-action",
     "object-src", "frame-ancestors"}
)
_SECTION_CHUNK = re.compile(r'<section class="sec[^"]*".*?</section>', re.S)
_HERO_MINUTES = re.compile(r'<div class="clock"><b>([^<]*)</b>')
_PERSIAN_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


@dataclass(frozen=True, slots=True)
class ReviewHtmlReport:
    """Outcome of :func:`inspect_review_html`. Errors block delivery; warnings do not."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    section_count: int = 0
    duration_minutes: int = 0


class _ReviewHtmlScanner(HTMLParser):
    """Collects tags, attributes and inline blocks; interprets nothing itself."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.inline: dict[str, list[str]] = {"script": [], "style": []}
        self._raw: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {name: value or "" for name, value in attrs}))
        if tag in self.inline:
            self._raw = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {name: value or "" for name, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag == self._raw:
            self._raw = None

    def handle_data(self, data: str) -> None:
        if self._raw is not None:
            self.inline[self._raw].append(data)


def _attribute_errors(tag: str, attrs: dict[str, str]) -> list[str]:
    from .review_sheet_html import ALLOWED_CLASSES

    errors: list[str] = []
    allowed = _COMMON_ATTRS | _ELEMENT_ATTRS.get(tag, frozenset())
    for name, value in attrs.items():
        if name not in allowed:
            errors.append(f"attribute {name!r} is not allowed on <{tag}>")
        elif name == "class" and any(token not in ALLOWED_CLASSES for token in value.split()):
            errors.append("unexpected class names in review HTML")
        elif name == "id" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
            errors.append("element id is malformed")
        elif name == "href" and not _ANCHOR.fullmatch(value):
            errors.append("links must point to in-document anchors")
        elif name == "style" and not (tag == "section" and _SECTION_STYLE.fullmatch(value)):
            errors.append("inline style is only allowed as a section colour pair")
        elif name == "data-t" and not re.fullmatch(r"[0-9]+", value):
            errors.append("data-t must be an integer with Latin digits")
        elif name == "data-n" and (_MARKUP_CHARS.search(value) or len(value) > 40):
            # The skeleton's chip text is plain text; markup here would be live HTML.
            errors.append("data-n must be a short label without markup characters")
        elif name == "name" and tag == "meta" and value not in _META_NAMES:
            errors.append("unexpected meta name")
        elif name == "http-equiv" and tag == "meta" and value != "Content-Security-Policy":
            errors.append("only the Content-Security-Policy http-equiv meta is allowed")
    return errors


def _parse_csp(csp: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for chunk in csp.split(";"):
        parts = chunk.split()
        if parts:
            directives.setdefault(parts[0].lower(), parts[1:])
    return directives


def _csp_errors(csp: str, scripts: list[str], styles: list[str], skeleton_script: str) -> list[str]:
    from .review_sheet_html import script_hash

    if not csp:
        return ["missing Content-Security-Policy"]
    directives = _parse_csp(csp)
    errors: list[str] = []
    for name in directives:
        if name not in _CSP_DIRECTIVES:
            errors.append(f"unexpected Content-Security-Policy directive {name!r}")
    expected = {
        "default-src": ["'none'"],
        "font-src": ["data:"],
        "img-src": ["data:"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "style-src": ["'unsafe-inline'"],
    }
    for name, values in expected.items():
        if directives.get(name) != values:
            errors.append(f"Content-Security-Policy {name} must be {' '.join(values)}")
    if len(scripts) != 1 or len(styles) != 1:
        errors.append("review HTML must contain exactly one inline script and one inline style")
        return errors
    if scripts[0] != skeleton_script:
        errors.append("inline script differs from the review sheet template")
    if directives.get("script-src") != [script_hash(scripts[0])]:
        errors.append("inline script does not match its Content-Security-Policy hash")
    if _EXTERNAL_URL.search(styles[0]) or "@import" in styles[0]:
        errors.append("stylesheet references an external resource")
    return errors


def _visible_text(chunk: str) -> str:
    """Text a reader sees, using the same stripping rules as the template linter."""
    text = re.sub(r"<style.*?</style>|<script.*?</script>|<svg.*?</svg>", "", chunk, flags=re.S)
    text = re.sub(r"<e>.*?</e>", "", text, flags=re.S)
    text = re.sub(r'<span class="eq">.*?</span>', "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"https?://\S+", "", text)


def _balance_errors(text: str) -> list[str]:
    errors = []
    for tag in ("section", "div", "table", "details", "ul", "span"):
        opened = len(re.findall(rf"<{tag}[\s>]", text))
        closed = len(re.findall(rf"</{tag}>", text))
        if opened != closed:
            errors.append(f"unbalanced <{tag}>: open={opened} close={closed}")
    return errors


def _section_errors(text: str, sections: list[str]) -> tuple[list[str], list[str], list[int]]:
    errors: list[str] = []
    warnings: list[str] = []
    minutes: list[int] = []
    for number, chunk in enumerate(sections, start=1):
        name = f"s{number}"
        opening_match = re.match(r"<section[^>]*>", chunk)
        opening = opening_match.group(0) if opening_match else ""
        if 'class="sec' not in opening or f'id="{name}"' not in opening:
            errors.append(f"section {name} must be the {number}-th <section class=\"sec\"> with id {name}")
        data_t = re.search(r'data-t="([^"]*)"', chunk)
        value = int(data_t.group(1)) if data_t and re.fullmatch(r"[0-9]+", data_t.group(1)) else 0
        if value < 1:
            errors.append(f"{name}: data-t must be a positive integer with Latin digits")
        minutes.append(value)
        if 'class="done"' not in chunk:
            errors.append(f"{name}: missing the mark-as-reviewed button")
        if f"⏱ {value}" not in chunk.translate(_PERSIAN_TO_LATIN):
            warnings.append(f"{name}: <small> minutes text differs from data-t")
        if "--c:" not in opening or "--cs:" not in opening:
            errors.append(f"{name}: missing --c/--cs colour variables")
        is_quiz = number == len(sections) and 'class="sec quiz"' in opening
        structured = re.search(r'class="(?:tw|g2|card|mono|zone|spec|lad|fig|key|trap|tip)', chunk) or "<details" in chunk
        if not structured:
            warnings.append(f"{name}: prose only, no structured component")
        details = len(re.findall(r"<details[\s>]", chunk))
        if is_quiz:
            if details < REVIEW_MIN_SELF_TEST_QUESTIONS:
                errors.append(f"self-test must contain at least {REVIEW_MIN_SELF_TEST_QUESTIONS} questions")
            elif details < REVIEW_RECOMMENDED_SELF_TEST_QUESTIONS:
                warnings.append(f"self-test has {details} questions (the template recommends 8 to 12)")
        else:
            words = len(_visible_text(chunk).split())
            low, high = REVIEW_WORDS_PER_MINUTE
            if value and not low <= words / value <= high:
                warnings.append(f"{name}: {words} words for {value} minutes is outside the budget")
    return errors, warnings, minutes


def inspect_review_html(content: bytes) -> ReviewHtmlReport:
    """Check a rendered review sheet against the template, offline and safety rules.

    Hard errors (the artifact is never stored or advertised): size and UTF-8; the
    envelope (doctype, ``lang="fa"``, ``dir="rtl"``, charset); a Content-Security-Policy
    that pins the template's script by hash and allows nothing else; the template's
    CSS and script present unchanged; only the allowed tags, attributes, classes and
    section colour pairs; no network reference or external ``url()``; 5-12 sections
    with ids s1..sN; the self-test last with at least three questions; minutes
    that add up to the hero duration; every section has its mark-as-reviewed button
    and a closed structure; in-document anchors that resolve.

    Warnings (logged, the sheet is still delivered): Latin text or digits outside
    ``<e>``, a self-test with fewer than eight questions, a section outside the
    words-per-minute budget, and prose-only sections.
    """
    from .review_sheet_html import load_skeleton

    if not content or len(content) > MAX_REVIEW_HTML_BYTES:
        return ReviewHtmlReport(("review HTML is empty or exceeds the size limit",))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ReviewHtmlReport(("review HTML is not valid UTF-8",))
    scanner = _ReviewHtmlScanner()
    try:
        scanner.feed(text)
        scanner.close()
    except Exception:  # HTMLParser is lenient; anything it raises is malformed input
        return ReviewHtmlReport(("review HTML could not be parsed",))

    skeleton = load_skeleton()
    errors: list[str] = []
    warnings: list[str] = []
    tags = scanner.tags
    if not text.lstrip().lower().startswith("<!doctype html>"):
        errors.append("missing <!DOCTYPE html>")
    if not tags or tags[0][0] != "html" or tags[0][1].get("lang") != "fa" or tags[0][1].get("dir") != "rtl":
        errors.append('root element must be <html lang="fa" dir="rtl">')
    if not any(tag == "meta" and attrs.get("charset", "").lower() == "utf-8" for tag, attrs in tags):
        errors.append('missing <meta charset="utf-8">')
    for tag, attrs in tags:
        if tag not in _ELEMENT_TAGS:
            errors.append(f"tag <{tag}> is not allowed")
        else:
            errors.extend(_attribute_errors(tag, attrs))
    ids = [attrs["id"] for _, attrs in tags if "id" in attrs]
    if len(ids) != len(set(ids)):
        errors.append("element ids must be unique")
    csp = next(
        (attrs.get("content", "") for tag, attrs in tags
         if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"),
        "",
    )
    errors.extend(_csp_errors(csp, scanner.inline["script"], scanner.inline["style"], skeleton.script_body))
    if len(scanner.inline["style"]) == 1 and skeleton.style_body not in scanner.inline["style"][0]:
        errors.append("the review sheet template's stylesheet has been changed")
    if _NETWORK_REFERENCE.search(text):
        errors.append("review HTML references an external network resource")
    if "{{" in text:
        errors.append("unreplaced {{placeholder}} left")
    if not any(tag == "main" and "wrap" in attrs.get("class", "").split() for tag, attrs in tags):
        errors.append("missing the main review container")
    errors.extend(_balance_errors(text))

    sections = _SECTION_CHUNK.findall(text)
    if not REVIEW_MIN_SECTIONS <= len(sections) <= REVIEW_MAX_SECTIONS:
        errors.append(f"review sheet must have {REVIEW_MIN_SECTIONS} to {REVIEW_MAX_SECTIONS} sections")
    if sections and 'class="sec quiz"' not in sections[-1][: sections[-1].find(">") + 1]:
        errors.append("the final section must be the self-test")
    section_errors, section_warnings, minutes = _section_errors(text, sections)
    errors.extend(section_errors)
    warnings.extend(section_warnings)
    hero = _HERO_MINUTES.search(text)
    duration = int(hero.group(1).translate(_PERSIAN_TO_LATIN)) if hero and hero.group(1).translate(
        _PERSIAN_TO_LATIN).isdigit() else 0
    if duration <= 0 or sum(minutes) != duration:
        errors.append("section minutes must add up exactly to the duration in the hero")
    if sections and minutes and minutes[-1] != 1:
        warnings.append("the self-test is expected to take one minute")

    anchors = set(ids) | {"top"}
    if any(tag == "a" and attrs.get("href", "")[1:] not in anchors for tag, attrs in tags):
        errors.append("a navigation link points to a missing section")
    visible = _visible_text(text)
    if re.search(r"[A-Za-z]{2,}", visible):
        warnings.append("Latin text outside <e> (copied booklet text may keep it)")
    if re.search(r"[0-9]", visible):
        warnings.append("Latin digits outside <e> (use Persian digits)")
    return ReviewHtmlReport(
        tuple(dict.fromkeys(errors)),
        tuple(dict.fromkeys(warnings)),
        section_count=len(sections),
        duration_minutes=max(duration, 0),
    )


def validate_review_html_artifact(content: bytes) -> None:
    """Raise ``ValueError`` when a review sheet must not be stored or delivered."""
    report = inspect_review_html(content)
    if report.errors:
        raise ValueError("renderer returned an invalid review HTML: " + "; ".join(report.errors[:5]))


_LEARNING_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_LEARNING_STYLE_RE = re.compile(
    r"<style(?:\s[^>]*)?>(?P<body>.*?)</style>",
    re.IGNORECASE | re.DOTALL,
)
_LEARNING_JSON_TYPE_RE = re.compile(
    r'\btype\s*=\s*["\']application/json["\']',
    re.IGNORECASE,
)
_LEARNING_DATA_ID_RE = re.compile(
    r'\bid\s*=\s*["\']gamas-learning-data["\']',
    re.IGNORECASE,
)


def validate_learning_html_artifact(
    content: bytes,
    *,
    expected_kind: str | None = None,
    max_bytes: int = MAX_REVIEW_HTML_BYTES,
) -> None:
    """Raise ``ValueError`` when an interactive quiz/flashcard HTML is unsafe or invalid."""
    from .review_sheet_html import script_hash

    if not content or len(content) > max_bytes:
        raise ValueError("learning HTML artifact is empty or exceeds size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("learning HTML artifact is not valid UTF-8") from exc
    if not text.lstrip().lower().startswith("<!doctype html>"):
        raise ValueError("learning HTML artifact must start with <!DOCTYPE html>")

    scanner = _ReviewHtmlScanner()
    try:
        scanner.feed(text)
        scanner.close()
    except Exception as exc:
        raise ValueError("learning HTML artifact could not be parsed") from exc

    tags = scanner.tags
    if not tags or tags[0][0] != "html" or tags[0][1].get("lang") != "fa" or tags[0][1].get("dir") != "rtl":
        raise ValueError('learning HTML root element must be <html lang="fa" dir="rtl">')
    if not any(tag == "meta" and attrs.get("charset", "").lower() == "utf-8" for tag, attrs in tags):
        raise ValueError('learning HTML missing <meta charset="utf-8">')

    csp = next(
        (
            attrs.get("content", "")
            for tag, attrs in tags
            if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy"
        ),
        "",
    )
    if not csp:
        raise ValueError("learning HTML missing Content-Security-Policy")
    directives = _parse_csp(csp)
    required_directives = {
        "default-src": ["'none'"],
        "font-src": ["data:"],
        "img-src": ["data:"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "style-src": ["'unsafe-inline'"],
    }
    for name, expected_values in required_directives.items():
        if directives.get(name) != expected_values:
            raise ValueError(f"learning HTML CSP {name} must be {' '.join(expected_values)}")

    styles = [m.group("body") for m in _LEARNING_STYLE_RE.finditer(text)]
    if len(styles) != 1:
        raise ValueError("learning HTML must contain exactly one <style> block")
    if _EXTERNAL_URL.search(styles[0]) or "@import" in styles[0]:
        raise ValueError("learning HTML stylesheet must not load external resources")
    if _NETWORK_REFERENCE.search(text):
        raise ValueError("learning HTML must not reference external network resources")

    exec_scripts: list[str] = []
    json_payload_raw: str | None = None
    for match in _LEARNING_SCRIPT_RE.finditer(text):
        attrs_raw = match.group("attrs") or ""
        body = match.group("body")
        if _LEARNING_JSON_TYPE_RE.search(attrs_raw) and _LEARNING_DATA_ID_RE.search(attrs_raw):
            json_payload_raw = body
        elif _LEARNING_JSON_TYPE_RE.search(attrs_raw):
            raise ValueError("unexpected JSON script block in learning HTML")
        else:
            exec_scripts.append(body)

    if len(exec_scripts) != 1:
        raise ValueError("learning HTML must contain exactly one executable <script> block")
    if directives.get("script-src") != [script_hash(exec_scripts[0])]:
        raise ValueError("learning HTML executable script does not match CSP hash")
    if json_payload_raw is None:
        raise ValueError("learning HTML missing embedded gamas-learning-data JSON payload")

    try:
        payload = json.loads(json_payload_raw)
    except Exception as exc:
        raise ValueError("learning HTML embedded JSON payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("learning HTML JSON payload must be an object")
    kind = payload.get("kind")
    if kind not in {"quiz", "flashcards"}:
        raise ValueError("learning HTML JSON payload has unsupported kind")
    if expected_kind is not None and kind != expected_kind:
        raise ValueError(f"learning HTML kind mismatch: expected {expected_kind}, got {kind}")
    items_key = "questions" if kind == "quiz" else "cards"
    items = payload.get(items_key)
    if not isinstance(items, list) or not items:
        raise ValueError(f"learning HTML JSON payload must contain non-empty {items_key}")

