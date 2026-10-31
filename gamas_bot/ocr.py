"""OCR for image-only documents, through the free i2ocr.com service.

The document pipeline is text-layer only by decision: a scanned PDF or a photo
of a textbook page is refused with an explicit Persian message instead of
producing an empty booklet. This module adds the missing capability without
pretending it is local, and without weakening any of the invariants in §5–§9 of
``AGENTS.md``:

* **Third party, opt-in.** ``OCR_ENABLED`` is ``false`` by default. When it is
  enabled, a text-poor PDF page (or an uploaded photo) leaves the host and is
  read by i2ocr.com, whose published policy deletes inputs and outputs within
  30 minutes. The deployment owner decides that trade-off; the code never
  assumes it.
* **No local engine, no system binary.** There is no Tesseract, no poppler and
  no model download: the recognition happens remotely, over HTTPS, through
  ``curl_cffi`` (the optional extra ``.[ocr]``) because the service fronts its
  API with a browser check that a plain ``aiohttp`` request cannot pass.
* **Bounded.** Page count, upload size, per-request timeout, per-document time
  budget and request rate all have hard ceilings, and one process performs one
  OCR request at a time (the free tier is a single-image service; concurrency
  only produces rate-limit rejections).
* **Blocking, but never on the loop.** Every public method is synchronous and
  thread-safe; callers run it through ``fileio.run_file_io`` so Telethon's event
  loop is untouched.
* **Typed failures.** Callers get :class:`OcrError` subclasses, never a bare
  ``requests`` exception, so a user-facing message can distinguish "service is
  down" from "this page is empty" from "OCR is not installed".

Protocol (reverse-engineered by the upstream ``i2ocr-py`` client, re-implemented
here and verified against an in-process mock in ``tests/test_ocr.py``)::

    GET  /                            establishes the session cookies
    GET  /DevSDK/challenge.php        ALTCHA challenge (SHA-256 proof of work)
    POST /DevSDK/challenge.php        payload=<base64 JSON> → "1"
    POST /DevSDK/upload.php           multipart file + service name → {"status": "1"}
    POST /DevSDK/ajax/pdf2png.php     id=<page> → rendered page file name
    POST /DevSDK/ajax/ocr_backend.php data=<JSON> → HTML holding the OCR text

Defects of the reference client this module fixes (each one has a regression
test in ``tests/test_ocr.py``; ``docs/OCR.md`` explains them at length):

1. **Mojibake on the fallback path.** ``raw.encode("utf-8").decode("unicode_escape")``
   re-reads UTF-8 bytes as Latin-1, so any page whose text contains a lone
   backslash (``C:\\path``, ``\\frac``) comes back as ``Ø³`` garbage for Persian,
   Arabic and Kazakh. :func:`_js_unescape` never mixes codecs.
2. **HTML entities in the textarea variant.** ``&amp;`` and friends were
   returned raw; the textarea fallback now runs through :func:`html.unescape`.
3. **One page per PDF.** The reference client OCRs a single page and discards
   the file name the service returns from ``pdf2png``. One upload now serves
   every requested page.
4. **No retries.** A single 429/503 lost the whole document. Transient statuses
   and transport errors are retried with jittered backoff, honouring
   ``Retry-After``.
5. **Mute CAPTCHA failures.** ``_authenticate_human`` returned ``False`` and the
   caller raised a generic ``RuntimeError``; the challenge now raises
   :class:`OcrChallengeError` and is solved again once before giving up.
6. **Header injection through the file name.** The multipart ``filename`` was
   the raw path name; quotes or CR/LF corrupt the body. Names are sanitized to
   an ASCII-safe form.
7. **Unbounded uploads.** ``Path.read_bytes()`` ran on whatever the caller
   passed. Size, emptiness and existence are checked first.
8. **Opaque parse errors.** ``r.json()`` on an HTML error page raised a
   ``JSONDecodeError``. Every stage now reports which one failed and what the
   service said.
9. **Trusted CAPTCHA range.** ``maxnumber`` came straight from the server and
   was iterated synchronously; the search is capped and has a deadline.
10. **Fabricated ``took``.** The proof-of-work payload reported a random
   500–1200 ms instead of the measured duration.
11. **Eager, unretried session.** Construction performed a warm-up request
    without retries, so one network blip broke the object. The session is lazy,
    retried and closable.
12. **No ordering or throttling.** The reference client had no notion of a rate
    limit, so a loop of pages raced the service; requests are serialized with a
    minimum interval.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .http_util import retry_after_seconds, sanitize_error_text

logger = logging.getLogger(__name__)

#: Base URL of the service. Overridable so tests (and a mirror) can point at a
#: local endpoint without patching module internals.
I2OCR_BASE_URL = "https://www.i2ocr.com"

#: Browser profile ``curl_cffi`` impersonates. The service inspects the TLS
#: fingerprint, so a generic client is refused before it reaches the API.
DEFAULT_IMPERSONATE = "chrome124"

#: Multipart ``service`` names of the two upload endpoints.
SERVICE_IMAGE = "img-ocr_en"
SERVICE_PDF = "pdf-ocr_en"

#: The bot speaks Persian; an alias-free default keeps .env honest.
DEFAULT_LANGUAGE = "persian"

#: Hard ceiling on the ALTCHA search space, independent of what a server asks
#: for. A SHA-256 proof of work over a larger range cannot finish inside the
#: request budget anyway.
MAX_ALTCHA_NUMBER = 2_000_000

#: Longest file name sent in the multipart header (the service only needs the
#: extension to classify the upload).
MAX_FILENAME_CHARS = 96

#: Default upload ceiling; the bot passes its own ``DOC_MAX_BYTES``.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

#: Images i2ocr documents as input (its Features page).
SUPPORTED_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
)

#: Every recognition language i2ocr.com documents (``/features``), as the slug
#: the ``ocr_lang`` parameter expects.
I2OCR_LANGUAGES = frozenset(
    {
        "afrikaans", "akkadian", "albanian", "amharic", "ancient-greek", "arabic",
        "armenian", "assamese", "azerbaijani", "azerbaijani-cyrilic", "basque",
        "belarusian", "bengali", "bosnian", "breton", "bulgarian", "burmese",
        "catalan", "cebuano", "cherokee", "chinese-simplified",
        "chinese-traditional", "corsican", "croatian", "czech", "danish", "dutch",
        "dzongkha", "english", "english-ancient", "esperanto", "estonian",
        "faroese", "filipino-tagalog", "finnish", "frankish", "french",
        "french-middle", "galician", "georgian", "georgian-ancient", "german",
        "german-fraktur", "greek-modern", "gujarati", "haitian", "hebrew", "hindi",
        "hungarian", "icelandic", "indonesian", "inuktitut", "irish", "italian",
        "italian-ancient", "japanese", "javanese", "kannada", "kazakh", "khmer",
        "kirghiz", "korean", "korean-vertical", "kurdish-kurmanji",
        "kurdish-sorani", "lao", "latin", "latvian", "lithuanian",
        "luxembourgish", "macedonian", "malay", "malayalam", "maltese", "maori",
        "marathi", "math-equation", "meitei", "moldavian", "mongolian", "nepali",
        "norwegian", "occitan", "oriya", "panjabi", "persian", "polish",
        "polytonic-greek", "portuguese", "pushto", "quechua", "romanian",
        "russian", "sanskrit", "santali", "scottish-gaelic", "serbian",
        "serbian-latin", "sindhi", "sinhala", "slovakian", "slovenian", "spanish",
        "spanish-ancient", "sundanese", "swahili", "swedish", "syriac", "tagalog",
        "tajik", "tamil", "tatar", "telugu", "thai", "tibetan", "tigrinya",
        "tonga", "turkish", "uighur", "ukrainian", "urdu", "uzbek",
        "uzbek-cyrilic", "vietnamese", "welsh", "western-frisian", "yiddish",
        "yoruba",
    }
)

#: Short codes an operator is likely to put in ``OCR_LANGUAGE`` by mistake.
OCR_LANGUAGE_ALIASES: dict[str, str] = {
    "fa": "persian",
    "fa-ir": "persian",
    "farsi": "persian",
    "parsi": "persian",
    "فارسی": "persian",
    "en": "english",
    "eng": "english",
    "ar": "arabic",
    "ara": "arabic",
    "kk": "kazakh",
    "kaz": "kazakh",
    "ru": "russian",
    "rus": "russian",
    "tr": "turkish",
    "tur": "turkish",
    "de": "german",
    "deu": "german",
    "fr": "french",
    "fra": "french",
}

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"
_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
}

#: Statuses worth another attempt. Everything else is a definitive answer.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})

#: The JS assignment the result page uses to carry the recognised text.
_TEXTBOX_PATTERN = re.compile(
    r"\$\(\s*[\"']#ocrTextBox[\"']\s*\)\.val\(\s*\"(?P<txt>(?:\\.|[^\"\\])*)\"", re.S
)
_TEXTBOX_SINGLE_PATTERN = re.compile(
    r"\$\(\s*[\"']#ocrTextBox[\"']\s*\)\.val\(\s*'(?P<txt>(?:\\.|[^'\\])*)'", re.S
)
_TEXTAREA_PATTERN = re.compile(
    r"<textarea[^>]*id\s*=\s*[\"']ocrTextBox[\"'][^>]*>(?P<txt>.*?)</textarea>",
    re.S | re.I,
)
_PAGE_FILE_PATTERN = re.compile(r"[\w.-]+\.(?:png|jpe?g|gif|bmp|tiff?)", re.I)
_UNSAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class OcrError(RuntimeError):
    """Base class for every OCR failure the bot can explain to a user."""


class OcrUnavailableError(OcrError):
    """The service (or its transport) could not be reached or answered badly.

    Retrying later is the honest advice: nothing about the document is wrong.
    """


class OcrRejectedError(OcrError):
    """The service refused this upload (format, size, or an explicit error)."""


class OcrChallengeError(OcrError):
    """The bot-protection challenge could not be solved or was not accepted."""


class OcrNoTextError(OcrError):
    """The service answered successfully but returned no recognisable text."""


@dataclass(frozen=True, slots=True)
class OcrPage:
    """Recognised text of one document page (``""`` for a blank page)."""

    number: int
    text: str


def normalize_ocr_language(value: str | None) -> str:
    """Return the i2ocr language slug for an operator-supplied value.

    Accepts the documented slug (``persian``), the ISO code an operator is
    likely to use instead (``fa``) and the Persian name. An unknown value is a
    configuration error, not something to silently replace: recognising a
    Persian scan with an English model produces confident garbage.
    """
    token = "-".join(str(value or "").strip().lower().replace("_", " ").split())
    if not token:
        return DEFAULT_LANGUAGE
    token = OCR_LANGUAGE_ALIASES.get(token, token)
    if token not in I2OCR_LANGUAGES:
        raise ValueError(
            f"OCR_LANGUAGE={value!r} is not an i2ocr language; "
            f"use a documented slug such as persian, english, arabic, kazakh"
        )
    return token


def curl_cffi_available() -> bool:
    """Whether the optional HTTP transport the service needs is installed."""
    try:
        import curl_cffi  # noqa: F401  (presence check only)
    except Exception:  # pragma: no cover - depends on the deployment
        return False
    return True


def is_supported_image_name(name: str | None) -> bool:
    """Whether *name* carries an image suffix the service documents."""
    return Path(str(name or "")).suffix.lower() in SUPPORTED_IMAGE_SUFFIXES


def _js_unescape(raw: str) -> str:
    """Decode a JS/JSON string body without ever mixing character encodings.

    ``json.loads`` handles every well-formed literal. When the page carries a
    stray escape (a lone ``\\`` from OCR of a formula or a path is common) the
    manual pass below keeps the character and drops only the backslash — unlike
    ``bytes.decode("unicode_escape")``, which reinterprets the UTF-8 text as
    Latin-1 and turns Persian into mojibake.
    """
    try:
        decoded = json.loads(f'"{raw}"')
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, str):
        return decoded
    out: list[str] = []
    index = 0
    length = len(raw)
    # Deliberately *not* the full JS escape table: in the fallback path the
    # page was already malformed, so only the escapes that cannot plausibly be
    # part of recognised text are interpreted. ``\f`` stays ``\f`` (a Persian
    # OCR page with ``\frac`` must not gain a form feed).
    simple = {"n": "\n", "r": "\r", "t": "\t", "/": "/", "\\": "\\", '"': '"', "'": "'"}
    while index < length:
        char = raw[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        escape = raw[index + 1]
        if escape in simple:
            out.append(simple[escape])
            index += 2
            continue
        if escape == "u" and index + 6 <= length:
            try:
                out.append(chr(int(raw[index + 2 : index + 6], 16)))
            except ValueError:
                out.append(escape)
            index += 6
            continue
        if escape == "x" and index + 4 <= length:
            try:
                out.append(chr(int(raw[index + 2 : index + 4], 16)))
            except ValueError:
                out.append(escape)
            index += 4
            continue
        # Unknown escape (a lone backslash from OCR of a formula or a Windows
        # path): keep both characters. Dropping the backslash would silently
        # rewrite the recognised text.
        out.append("\\")
        out.append(escape)
        index += 2
    return "".join(out)


def extract_ocr_text(page_html: str) -> str:
    """Pull the recognised text out of the ``ocr_backend`` HTML response.

    Raises :class:`OcrChallengeError` when the page still asks for the CAPTCHA
    and :class:`OcrNoTextError` when the text element is missing altogether.
    """
    payload = str(page_html or "")
    for pattern in (_TEXTBOX_PATTERN, _TEXTBOX_SINGLE_PATTERN):
        match = pattern.search(payload)
        if match:
            return _normalize_lines(_js_unescape(match.group("txt")))
    match = _TEXTAREA_PATTERN.search(payload)
    if match:
        return _normalize_lines(html.unescape(match.group("txt")))
    if "altcha" in payload.lower():
        raise OcrChallengeError("The OCR service asked for the CAPTCHA again.")
    raise OcrNoTextError("The OCR service answered without an OCR text element.")


def _normalize_lines(text: str) -> str:
    lines = [line.rstrip() for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _sanitize_filename(name: str) -> str:
    """An ASCII, header-safe file name that keeps the service's extension hint."""
    suffix = Path(str(name or "")).suffix.lower()
    if suffix not in _IMAGE_CONTENT_TYPES:
        suffix = ".bin"
    stem = _UNSAFE_FILENAME_PATTERN.sub("_", Path(str(name or "")).stem).strip("._-")
    stem = stem[: MAX_FILENAME_CHARS - len(suffix)] or "upload"
    return f"{stem}{suffix}"


def _content_type_for(name: str) -> str:
    return _IMAGE_CONTENT_TYPES.get(Path(str(name)).suffix.lower(), "application/octet-stream")


def encode_multipart(
    fields: dict[str, str], files: Sequence[tuple[str, str, bytes, str]]
) -> tuple[bytes, str]:
    """Build a multipart/form-data body with a random boundary.

    Written by hand because the service needs the exact field names and a
    ``curl_cffi`` file tuple would be re-serialised by the HTTP layer.
    """
    boundary = f"----gamas{random.getrandbits(64):016x}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        chunks.append(f"{value}\r\n".encode())
    for field, filename, payload, content_type in files:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _default_session_factory(impersonate: str) -> Any:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:  # pragma: no cover - exercised by the deployment
        raise OcrUnavailableError(
            "OCR needs the optional 'curl-cffi' package: "
            "pip install 'gamas-bot[ocr]' (see docs/OCR.md)."
        ) from exc
    session: Any = curl_requests.Session(impersonate=impersonate)
    session.headers.update(
        {
            "Accept": "*/*",
            "Accept-Language": "fa,en;q=0.8",
            "Origin": I2OCR_BASE_URL,
            "Referer": I2OCR_BASE_URL + "/",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def _json_object(response: Any, stage: str) -> dict[str, Any]:
    body = bytes(getattr(response, "content", b"") or b"")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise OcrUnavailableError(f"The OCR {stage} step returned a non-JSON response.") from exc
    if not isinstance(data, dict):
        raise OcrUnavailableError(f"The OCR {stage} step returned an unexpected payload.")
    return data


def _payload_message(data: dict[str, Any]) -> str:
    for key in ("message", "error", "msg", "detail"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_error_text(value)
    return sanitize_error_text(sorted(data))[:160] or "no reason given"


def _page_filename(body: bytes, page: int) -> str:
    """The page image name the service rendered, with a conventional fallback."""
    text = body.decode("utf-8", "replace").strip()
    candidate = ""
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("file", "filename", "page", "name", "image"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    break
        elif isinstance(parsed, str):
            candidate = parsed.strip()
        if not candidate:
            match = _PAGE_FILE_PATTERN.search(text)
            if match:
                candidate = match.group(0)
    candidate = candidate.strip("\"' ")
    if not candidate or "/" in candidate or "\\" in candidate:
        return f"page-{page:05d}.png"
    return candidate


def solve_altcha(
    challenge: dict[str, Any],
    *,
    deadline_seconds: float = 60.0,
    clock: Callable[[], float] = time.perf_counter,
) -> str:
    """Solve an ALTCHA SHA-256 proof of work and return the base64 payload.

    The search space is bounded by both the server's ``maxnumber`` and
    :data:`MAX_ALTCHA_NUMBER`, and by a wall-clock deadline, so a hostile or
    misconfigured challenge cannot pin a worker thread forever.
    """
    algorithm = str(challenge.get("algorithm") or "")
    target = str(challenge.get("challenge") or "")
    salt = str(challenge.get("salt") or "")
    signature = str(challenge.get("signature") or "")
    if algorithm != "SHA-256" or not target or not salt:
        raise OcrChallengeError(f"Unsupported OCR challenge (algorithm={algorithm!r}).")
    try:
        max_number = int(challenge.get("maxnumber", 100_000))
    except (TypeError, ValueError) as exc:
        raise OcrChallengeError("The OCR challenge has an invalid range.") from exc
    if max_number < 0:
        raise OcrChallengeError("The OCR challenge has an invalid range.")
    max_number = min(max_number, MAX_ALTCHA_NUMBER)

    started = clock()
    deadline = started + max(1.0, float(deadline_seconds))
    salt_bytes = salt.encode("utf-8")
    for number in range(max_number + 1):
        if number % 1024 == 0 and clock() > deadline:
            raise OcrChallengeError("The OCR challenge could not be solved in time.")
        if hashlib.sha256(salt_bytes + str(number).encode("ascii")).hexdigest() == target:
            payload = {
                "algorithm": algorithm,
                "challenge": target,
                "number": number,
                "salt": salt,
                "signature": signature,
                "took": int((clock() - started) * 1000),
            }
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            return base64.b64encode(raw.encode("utf-8")).decode("ascii")
    raise OcrChallengeError("The OCR challenge has no solution in its stated range.")


class I2OCRClient:
    """Thread-safe, bounded client for the i2ocr.com recognition API.

    One instance serves the whole process: the service's free tier accepts one
    image at a time, so the instance serializes its own calls. It is created
    lazily (no network in ``__init__``) and must be closed on shutdown.
    """

    def __init__(
        self,
        *,
        base_url: str = I2OCR_BASE_URL,
        impersonate: str = DEFAULT_IMPERSONATE,
        timeout: float = 120.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        min_interval: float = 1.5,
        max_attempts: int = 3,
        solver_seconds: float = 60.0,
        session_factory: Callable[[str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.impersonate = impersonate
        self.timeout = max(1.0, float(timeout))
        self.max_bytes = max(1, int(max_bytes))
        self.min_interval = max(0.0, float(min_interval))
        self.max_attempts = max(1, int(max_attempts))
        self.solver_seconds = max(1.0, float(solver_seconds))
        self._session_factory = session_factory or _default_session_factory
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._session: Any = None
        self._last_request = 0.0

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        """Release the HTTP session (safe to call from a thread)."""
        with self._lock:
            session: Any = self._session
            self._session = None
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - closing must never fail a job
                logger.debug("closing the OCR session failed", exc_info=True)

    def __enter__(self) -> I2OCRClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- transport
    def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        session = self._session_factory(self.impersonate)
        self._session = session
        try:
            self._call("GET", self.base_url + "/", headers={"Referer": self.base_url + "/"})
        except Exception:
            self._session = None
            raise
        return session

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = self._clock() - self._last_request
        if self._last_request and elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_request = self._clock()

    def _backoff(self, attempt: int) -> float:
        return min(30.0, (2 ** (attempt - 1)) * 0.75) * (0.75 + random.random() / 2)

    def _call(
        self,
        method: str,
        url: str,
        *,
        attempts: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """One HTTP call with bounded retries; returns the 2xx response."""
        self._ensure_session()
        tries = max(1, int(attempts or self.max_attempts))
        last_error: OcrError | None = None
        stage = url.rsplit("/", 1)[-1] or url
        delay = 0.0
        for attempt in range(1, tries + 1):
            self._throttle()
            try:
                response = self._session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except OcrError:
                raise
            except Exception as exc:
                last_error = OcrUnavailableError(
                    f"OCR service unreachable ({type(exc).__name__}): "
                    f"{sanitize_error_text(exc, 120)}"
                )
                delay = self._backoff(attempt)
            else:
                status = int(getattr(response, "status_code", 0) or 0)
                if 200 <= status < 300:
                    return response
                message = sanitize_error_text(
                    bytes(getattr(response, "content", b"") or b"")[:400]
                    .decode("utf-8", "replace")
                )
                if status == 413:
                    raise OcrRejectedError(
                        "The OCR service rejected the upload as too large."
                    )
                if status in _RETRY_STATUSES or status >= 500:
                    last_error = OcrUnavailableError(
                        f"OCR service answered HTTP {status}: {message}"
                    )
                    retry_after = retry_after_seconds(getattr(response, "headers", None))
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                else:
                    raise OcrRejectedError(
                        f"OCR service answered HTTP {status} on {stage}: {message}"
                    )
            if attempt < tries:
                self._sleep(min(30.0, max(0.0, delay)))
        raise last_error or OcrUnavailableError("The OCR service failed without a reason.")

    # ------------------------------------------------------------ service API
    def extract_pages(
        self,
        path: Path | str,
        pages: Sequence[int],
        *,
        language: str = DEFAULT_LANGUAGE,
        budget_seconds: float = 600.0,
    ) -> tuple[OcrPage, ...]:
        """OCR the requested pages of a PDF, or the single image at *path*.

        Pages that could not be read inside the time budget are simply absent
        from the result; the caller keeps whatever the document already had.
        """
        target = Path(path).expanduser()
        requested = sorted({int(page) for page in pages if int(page) >= 1})
        if not requested:
            return ()
        language = normalize_ocr_language(language)
        if target.suffix.lower() == ".pdf":
            return self.ocr_pdf(target, requested, language=language, budget_seconds=budget_seconds)
        if 1 not in requested:
            return ()
        return (OcrPage(1, self.ocr_image(target, language=language)),)

    def ocr_image(self, path: Path | str, *, language: str = DEFAULT_LANGUAGE) -> str:
        """Recognise a single image and return its text (may be empty)."""
        with self._lock:
            upload = self._upload(path, SERVICE_IMAGE)
            page_name = str(upload.get("page") or upload.get("filename") or "image")
            return self._recognize(page_name, normalize_ocr_language(language))

    def ocr_pdf(
        self,
        path: Path | str,
        pages: Sequence[int],
        *,
        language: str = DEFAULT_LANGUAGE,
        budget_seconds: float = 600.0,
    ) -> tuple[OcrPage, ...]:
        """Upload a PDF once and recognise every requested page."""
        with self._lock:
            requested = sorted({int(page) for page in pages if int(page) >= 1})
            if not requested:
                return ()
            self._upload(path, SERVICE_PDF)
            language = normalize_ocr_language(language)
            deadline = self._clock() + max(1.0, float(budget_seconds))
            done: list[OcrPage] = []
            for page in requested:
                if self._clock() > deadline:
                    logger.warning(
                        "ocr budget exhausted after %s page(s); %s page(s) left",
                        len(done), len(requested) - len(done),
                    )
                    break
                page_name = self._pdf_page_image(page)
                done.append(OcrPage(page, self._recognize(page_name, language, page=page)))
            return tuple(done)

    # --------------------------------------------------------------- steps
    def _upload(self, path: Path | str, service: str) -> dict[str, Any]:
        target = Path(path).expanduser()
        if not target.is_file():
            raise OcrRejectedError(f"OCR source file is missing: {target.name}")
        size = target.stat().st_size
        if size <= 0:
            raise OcrRejectedError("OCR source file is empty.")
        if size > self.max_bytes:
            raise OcrRejectedError(
                f"OCR source file exceeds the {self.max_bytes} byte ceiling."
            )
        payload = target.read_bytes()
        fields = {"service": service, "x": "0", "y": "0", "w": "0", "h": "0", "r": "0"}
        body, content_type = encode_multipart(
            fields,
            [("upload_file[]", _sanitize_filename(target.name), payload, _content_type_for(target.name))],
        )
        response = self._call(
            "POST",
            self.base_url + "/DevSDK/upload.php",
            data=body,
            headers={"Content-Type": content_type},
        )
        data = _json_object(response, "upload")
        if str(data.get("status")) != "1":
            raise OcrRejectedError(f"OCR upload was refused: {_payload_message(data)}")
        return data

    def _pdf_page_image(self, page: int) -> str:
        response = self._call(
            "POST",
            self.base_url + "/DevSDK/ajax/pdf2png.php",
            data=urlencode({"id": str(page)}),
            headers={"Content-Type": _FORM_CONTENT_TYPE},
        )
        return _page_filename(bytes(getattr(response, "content", b"") or b""), page)

    def _authenticate(self) -> None:
        """Solve the ALTCHA challenge; the cookie it sets authorises OCR calls."""
        response = self._call(
            "GET",
            self.base_url + "/DevSDK/challenge.php",
            headers={"Referer": self.base_url + "/"},
        )
        challenge = _json_object(response, "challenge")
        payload = solve_altcha(challenge, deadline_seconds=self.solver_seconds)
        accepted = self._call(
            "POST",
            self.base_url + "/DevSDK/challenge.php",
            data=urlencode({"payload": payload}),
            headers={"Content-Type": _FORM_CONTENT_TYPE},
        )
        if bytes(getattr(accepted, "content", b"") or b"").strip() != b"1":
            raise OcrChallengeError("The OCR service did not accept the challenge answer.")

    def _recognize(self, page_filename: str, language: str, *, page: int | None = None) -> str:
        request = {
            "x": "0", "y": "0", "w": "0", "h": "0", "r": "0",
            "en_future_engine": "1",
            "ocr_lang": language,
            "page_layout": "s",
            "page_filename": page_filename,
        }
        for attempt in (1, 2):
            self._authenticate()
            try:
                response = self._call(
                    "POST",
                    self.base_url + "/DevSDK/ajax/ocr_backend.php",
                    data=urlencode({"data": json.dumps(request, separators=(",", ":"))}),
                    headers={"Content-Type": _FORM_CONTENT_TYPE},
                )
                return extract_ocr_text(
                    bytes(getattr(response, "content", b"") or b"").decode("utf-8", "replace")
                )
            except OcrChallengeError:
                if attempt == 2:
                    raise
                logger.info("ocr challenge expired for page %s; solving it again", page)
        raise OcrChallengeError("The OCR service kept asking for a new challenge.")


def extract_text_problem(error: BaseException) -> str:
    """A short, log-safe label for a failed OCR attempt (never user-facing)."""
    return f"{type(error).__name__}: {sanitize_error_text(error, 160)}"
