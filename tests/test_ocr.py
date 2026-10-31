"""The i2ocr adapter, exercised against an in-process mock of the service.

The real service cannot be reached from CI (and a test suite must never depend
on a third-party website), so every protocol detail this module relies on is
reproduced here: the ALTCHA challenge, the multipart upload, the PDF page
renderer and the HTML that carries the recognised text.

The regression tests pin the defects listed in ``gamas_bot/ocr.py``'s module
docstring — in particular the mojibake fallback of the reference client, which
turned Persian text into ``Ø³`` as soon as a page contained a lone backslash.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

from gamas_bot.ocr import (
    DEFAULT_IMPERSONATE,
    I2OCRClient,
    OcrChallengeError,
    OcrNoTextError,
    OcrRejectedError,
    OcrUnavailableError,
    encode_multipart,
    extract_ocr_text,
    normalize_ocr_language,
    solve_altcha,
)

PERSIAN_TEXT = "سلول‌های بدن برای تولید انرژی به اکسیژن نیاز دارند."


class OcrMockServer:
    """A faithful-enough i2ocr.com stand-in for the documented protocol."""

    def __init__(self) -> None:
        self.pages: dict[int, str] = {1: PERSIAN_TEXT}
        self.html_style = "json"
        self.upload_status = "1"
        self.upload_message = ""
        self.challenge_answer = "1"
        self.page_name_style = "text"
        self.delay = 0.0
        self.failures: list[int] = []
        self.fail_counts: dict[str, int] = {}
        self.uploads: list[dict] = []
        self.calls: list[str] = []
        self.solved_challenges: list[dict] = []
        self.sessions: set[str] = set()
        self._number = 7
        self._salt = "?expires=9999999999&id=mock"
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    # ------------------------------------------------------------- internals
    @property
    def challenge_digest(self) -> str:
        return hashlib.sha256(f"{self._salt}{self._number}".encode()).hexdigest()

    def challenge_payload(self) -> dict:
        return {
            "algorithm": "SHA-256",
            "challenge": self.challenge_digest,
            "salt": self._salt,
            "signature": "signature",
            "maxnumber": 50,
        }

    def render(self, text: str) -> str:
        if self.html_style == "json":
            return f'<script>$("#ocrTextBox").val({json.dumps(text, ensure_ascii=False)});</script>'
        if self.html_style == "raw":
            return f'<script>$("#ocrTextBox").val("{text}");</script>'
        if self.html_style == "single":
            return f"<script>$('#ocrTextBox').val('{text}');</script>"
        if self.html_style == "textarea":
            return f'<textarea id="ocrTextBox" rows="10">{html.escape(text)}</textarea>'
        if self.html_style == "challenge":
            return "<html><body><altcha-widget></altcha-widget></body></html>"
        return "<html><body>no text here</body></html>"


def _make_handler(state: OcrMockServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OcrMock/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:  # keep the test output clean
            return

        # ------------------------------------------------------------- helpers
        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(length) if length else b""

        def _send(self, payload: bytes, status: int = 200, headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def _authorised(self) -> bool:
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "sid" and value in state.sessions:
                    return True
            return False

        def _maybe_fail(self) -> bool:
            if state.failures:
                self._send(b"temporarily unavailable", status=state.failures.pop(0))
                return True
            for suffix, remaining in state.fail_counts.items():
                if self.path.endswith(suffix) and remaining > 0:
                    state.fail_counts[suffix] = remaining - 1
                    self._send(b"temporarily unavailable", status=503)
                    return True
            return False

        # -------------------------------------------------------------- routes
        def do_GET(self) -> None:
            state.calls.append("GET " + self.path)
            if self._maybe_fail():
                return
            if self.path in {"/", ""}:
                self._send(b"<html>i2ocr mock</html>")
                return
            if self.path == "/DevSDK/challenge.php":
                self._send(json.dumps(state.challenge_payload()).encode())
                return
            self._send(b"not found", status=404)

        def do_POST(self) -> None:
            state.calls.append("POST " + self.path)
            # Read the body before any failure path: leaving it unread corrupts
            # the next keep-alive request even though the server answered.
            body = self._body()
            if self._maybe_fail():
                return
            if self.path == "/DevSDK/challenge.php":
                self._challenge(body)
                return
            if self.path == "/DevSDK/upload.php":
                self._upload(body)
                return
            if self.path in {"/DevSDK/ajax/pdf2png.php", "/DevSDK/ajax/ocr_backend.php"}:
                if not self._authorised():
                    self._send(state.render("").encode() if "ocr" in self.path else b"0")
                    return
                if self.path.endswith("pdf2png.php"):
                    page = int(parse_qs(body.decode()).get("id", ["1"])[0])
                    if state.page_name_style == "json":
                        payload = json.dumps({"file": f"page-{page:05d}.png"})
                    else:
                        payload = f"page-{page:05d}.png"
                    self._send(payload.encode())
                    return
                self._ocr(body)
                return
            self._send(b"not found", status=404)

        # ------------------------------------------------------------- actions
        def _challenge(self, body: bytes) -> None:
            form = parse_qs(body.decode())
            if "payload" not in form:
                self._send(b"0")
                return
            try:
                decoded = json.loads(base64.b64decode(unquote(form["payload"][0])).decode())
            except Exception:
                self._send(b"0")
                return
            state.solved_challenges.append(decoded)
            ok = (
                decoded.get("algorithm") == "SHA-256"
                and decoded.get("challenge") == state.challenge_digest
                and decoded.get("salt") == state._salt
                and hashlib.sha256(
                    f"{decoded.get('salt')}{decoded.get('number')}".encode()
                ).hexdigest()
                == decoded.get("challenge")
            )
            if not ok or state.challenge_answer != "1":
                self._send(b"0")
                return
            sid = secrets.token_hex(8)
            state.sessions.add(sid)
            self._send(b"1", headers={"Set-Cookie": f"sid={sid}; Path=/"})

        def _upload(self, body: bytes) -> None:
            if state.delay:
                time.sleep(state.delay)
            filename = re.search(rb'filename="([^"]*)"', body)
            service = re.search(rb'name="service"\r\n\r\n([^\r]*)\r\n', body)
            state.uploads.append(
                {
                    "filename": filename.group(1).decode() if filename else "",
                    "service": service.group(1).decode() if service else "",
                    "size": len(body),
                }
            )
            payload = {"status": state.upload_status}
            if state.upload_message:
                payload["message"] = state.upload_message
            self._send(json.dumps(payload).encode())

        def _ocr(self, body: bytes) -> None:
            if state.delay:
                time.sleep(state.delay)
            form = parse_qs(body.decode())
            request = json.loads(form["data"][0])
            match = re.search(r"(\d+)", request.get("page_filename", "1"))
            page = int(match.group(1)) if match else 1
            text = state.pages.get(page, "")
            state.calls.append(f"ocr lang={request.get('ocr_lang')} page={page}")
            self._send(state.render(text).encode())

    return Handler


class OcrTransportTests(unittest.TestCase):
    """Multipart encoding and text decoding, without the network."""

    def test_multipart_body_is_well_formed(self):
        body, content_type = encode_multipart(
            {"service": "img-ocr_en"},
            [("upload_file[]", "page.png", b"\x89PNG", "image/png")],
        )
        boundary = content_test = content_type.split("boundary=", 1)[1]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertIn(b'Content-Disposition: form-data; name="service"', body)
        self.assertIn(b'filename="page.png"', body)
        self.assertIn(b"\x89PNG", body)
        self.assertTrue(content_test)

    def test_json_literal_is_decoded_without_reencoding(self):
        page = f'<script>$("#ocrTextBox").val({json.dumps(PERSIAN_TEXT + chr(10) + "خط دوم", ensure_ascii=False)});</script>'
        self.assertEqual(extract_ocr_text(page), PERSIAN_TEXT + "\nخط دوم")

    def test_lone_backslash_keeps_persian_text_intact(self):
        """The reference client's ``unicode_escape`` fallback produced Ø³ garbage."""
        raw = r"مسیر C:\Users و فرمول \frac{1}{2}"
        page = f'<script>$("#ocrTextBox").val("{raw}");</script>'
        self.assertEqual(extract_ocr_text(page), raw)
        self.assertNotIn("Ø", extract_ocr_text(page))

    def test_single_quoted_and_textarea_variants(self):
        quoted = "<script>$('#ocrTextBox').val('متن کوتاه');</script>"
        self.assertEqual(extract_ocr_text(quoted), "متن کوتاه")
        textarea = '<textarea id="ocrTextBox" rows="4">A &amp; B &lt;3</textarea>'
        self.assertEqual(extract_ocr_text(textarea), "A & B <3")

    def test_missing_element_raises_and_challenge_is_distinguished(self):
        with self.assertRaises(OcrNoTextError):
            extract_ocr_text("<html>nothing</html>")
        with self.assertRaises(OcrChallengeError):
            extract_ocr_text("<html><altcha-widget></altcha-widget></html>")

    def test_language_aliases_normalise_and_unknown_values_are_refused(self):
        self.assertEqual(normalize_ocr_language("fa"), "persian")
        self.assertEqual(normalize_ocr_language("Farsi"), "persian")
        self.assertEqual(normalize_ocr_language(""), "persian")
        self.assertEqual(normalize_ocr_language("Chinese Simplified"), "chinese-simplified")
        with self.assertRaises(ValueError):
            normalize_ocr_language("klingon")

    def test_altcha_solver_answers_with_a_valid_payload(self):
        salt, number = "?id=abc", 1234
        payload = solve_altcha(
            {
                "algorithm": "SHA-256",
                "challenge": hashlib.sha256(f"{salt}{number}".encode()).hexdigest(),
                "salt": salt,
                "signature": "sig",
                "maxnumber": 5000,
            }
        )
        decoded = json.loads(base64.b64decode(payload))
        self.assertEqual(decoded["number"], number)
        self.assertGreaterEqual(decoded["took"], 0)
        self.assertEqual(decoded["signature"], "sig")

    def test_altcha_solver_gives_up_on_an_unsolvable_challenge(self):
        with self.assertRaises(OcrChallengeError):
            solve_altcha(
                {
                    "algorithm": "SHA-256",
                    "challenge": "0" * 64,
                    "salt": "?id=abc",
                    "signature": "sig",
                    "maxnumber": 2_000_000,
                },
                deadline_seconds=0.05,
            )


class OcrClientTests(unittest.TestCase):
    """The full protocol against the mock service."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.server = OcrMockServer()
        self.clients: list[I2OCRClient] = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.close()
        self._tmp.cleanup()

    def make_client(self, **overrides) -> I2OCRClient:
        options = {
            "base_url": self.server.base_url,
            "impersonate": DEFAULT_IMPERSONATE,
            "timeout": 15.0,
            "max_bytes": 1024 * 1024,
            "min_interval": 0.0,
            "max_attempts": 3,
            "sleep": lambda _seconds: None,
        }
        options.update(overrides)
        client = I2OCRClient(**options)
        self.clients.append(client)
        return client

    def write(self, name: str, payload: bytes = b"\x89PNG\r\n\x1a\n") -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    # ---------------------------------------------------------------- images
    def test_image_ocr_solves_the_challenge_and_returns_persian_text(self):
        client = self.make_client()
        text = client.ocr_image(self.write("scan.png"), language="fa")
        self.assertEqual(text, PERSIAN_TEXT)
        self.assertEqual(self.server.uploads[0]["service"], "img-ocr_en")
        self.assertEqual(self.server.uploads[0]["filename"], "scan.png")
        self.assertTrue(any("ocr lang=persian" in call for call in self.server.calls))
        self.assertEqual(len(self.server.solved_challenges), 1)

    def test_page_style_variants_decode_identically(self):
        text = "خط اول\nخط دوم با \"نقل قول\""
        # A raw unescaped quote cannot exist inside a valid JS literal, so the
        # "raw" (malformed page) variant is covered separately below.
        for style in ("json", "single", "textarea"):
            with self.subTest(style=style):
                self.server.pages = {1: text}
                self.server.html_style = style
                client = self.make_client()
                self.assertEqual(client.ocr_image(self.write(f"page-{style}.png")), text)
                client.close()

    def test_malformed_raw_page_still_keeps_unicode_and_backslashes(self):
        raw_text = "فرمول \\frac{1}{2} در C:\\Users\nخط دوم"
        self.server.pages = {1: raw_text}
        self.server.html_style = "raw"
        client = self.make_client()
        self.assertEqual(client.ocr_image(self.write("raw.png")), raw_text)

    # ------------------------------------------------------------------ PDFs
    def test_every_requested_pdf_page_is_recognised_from_one_upload(self):
        self.server.pages = {1: "صفحهٔ یک", 2: "صفحهٔ دو", 3: "صفحهٔ سه"}
        client = self.make_client()
        pages = client.ocr_pdf(self.write("lesson.pdf", b"%PDF-1.4 mock"), [1, 2, 3])
        self.assertEqual([page.number for page in pages], [1, 2, 3])
        self.assertEqual([page.text for page in pages], ["صفحهٔ یک", "صفحهٔ دو", "صفحهٔ سه"])
        self.assertEqual(len(self.server.uploads), 1)
        self.assertEqual(self.server.uploads[0]["service"], "pdf-ocr_en")

    def test_page_name_from_the_service_is_used_when_it_answers_json(self):
        self.server.page_name_style = "json"
        self.server.pages = {2: "دوم"}
        client = self.make_client()
        pages = client.ocr_pdf(self.write("doc.pdf", b"%PDF-1.4"), [2])
        self.assertEqual(pages[0].text, "دوم")

    def test_extract_pages_dispatches_on_the_suffix(self):
        client = self.make_client()
        self.assertEqual(
            [page.text for page in client.extract_pages(self.write("a.png"), [1])],
            [PERSIAN_TEXT],
        )
        self.assertEqual(client.extract_pages(self.write("a.png"), [2]), ())

    # -------------------------------------------------------------- failures
    def test_transient_status_is_retried(self):
        self.server.fail_counts = {"upload.php": 2}
        client = self.make_client()
        self.assertEqual(client.ocr_image(self.write("retry.png")), PERSIAN_TEXT)
        self.assertEqual(sum(1 for call in self.server.calls if call.endswith("upload.php")), 3)

    def test_retries_are_bounded(self):
        self.server.fail_counts = {"upload.php": 10}
        client = self.make_client(max_attempts=2)
        with self.assertRaises(OcrUnavailableError):
            client.ocr_image(self.write("always-down.png"))
        self.assertEqual(sum(1 for call in self.server.calls if call.endswith("upload.php")), 2)

    def test_upload_rejection_carries_the_service_message(self):
        self.server.upload_status = "0"
        self.server.upload_message = "unsupported file type"
        client = self.make_client()
        with self.assertRaises(OcrRejectedError) as caught:
            client.ocr_image(self.write("weird.png"))
        self.assertIn("unsupported file type", str(caught.exception))

    def test_missing_and_oversized_files_never_reach_the_service(self):
        client = self.make_client(max_bytes=10)
        with self.assertRaises(OcrRejectedError):
            client.ocr_image(self.root / "absent.png")
        with self.assertRaises(OcrRejectedError):
            client.ocr_image(self.write("big.png", b"x" * 64))
        self.assertEqual(self.server.calls, [])

    def test_hostile_file_name_is_sanitised_in_the_multipart_header(self):
        client = self.make_client()
        client.ocr_image(self.write('evil"name\r\nX-Injected: 1.png'))
        stored = self.server.uploads[0]["filename"]
        self.assertNotIn('"', stored)
        self.assertNotIn("\r", stored)
        self.assertNotIn("\n", stored)
        self.assertTrue(stored.endswith(".png"))

    def test_refused_challenge_raises_a_typed_error(self):
        self.server.challenge_answer = "0"
        client = self.make_client()
        with self.assertRaises(OcrChallengeError):
            client.ocr_image(self.write("denied.png"))

    def test_expired_challenge_is_solved_again_and_the_page_succeeds(self):
        client = self.make_client()
        # The first OCR call runs without any session cookie: the service
        # answers with the challenge page, and the client must re-authenticate.
        self.server.sessions.clear()
        original = self.server.render
        calls = {"count": 0}

        def flaky(text: str) -> str:
            calls["count"] += 1
            if calls["count"] == 1:
                return "<html><altcha-widget></altcha-widget></html>"
            return original(text)

        self.server.render = flaky  # type: ignore[method-assign]
        try:
            self.assertEqual(client.ocr_image(self.write("again.png")), PERSIAN_TEXT)
        finally:
            self.server.render = original  # type: ignore[method-assign]
        self.assertGreaterEqual(len(self.server.solved_challenges), 2)

    def test_unreachable_service_is_reported_as_unavailable(self):
        client = self.make_client(base_url="http://127.0.0.1:1", max_attempts=1)
        with self.assertRaises(OcrUnavailableError):
            client.ocr_image(self.write("offline.png"))

    def test_time_budget_stops_the_page_loop(self):
        self.server.pages = {1: "یک", 2: "دو", 3: "سه"}
        self.server.delay = 0.4
        client = self.make_client()
        started = time.monotonic()
        pages = client.ocr_pdf(self.write("slow.pdf", b"%PDF-1.4"), [1, 2, 3], budget_seconds=1.0)
        self.assertLess(len(pages), 3)
        self.assertLess(time.monotonic() - started, 6.0)

    def test_one_client_serialises_concurrent_callers(self):
        client = self.make_client()
        results: list[str] = []
        errors: list[BaseException] = []

        def work(index: int) -> None:
            try:
                results.append(client.ocr_image(self.write(f"thread-{index}.png")))
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result == PERSIAN_TEXT for result in results))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
