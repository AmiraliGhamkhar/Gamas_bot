# OCR for scans and photos (opt-in)

The document pipeline is text-layer only by design: a scanned PDF was refused with a Persian
explanation and a photo was answered with «OCR پشتیبانی نمی‌شود». This page documents the **opt-in**
OCR path that fills that gap using the free [i2ocr.com](https://www.i2ocr.com) service, and the
review of the upstream [`i2ocr-py`](https://github.com/AmiraliGhamkhar/i2ocr-py) client it replaces.

```text
PDF with pages < ~10 chars/page ─┬─ OCR_ENABLED=false → refused, same Persian message as before
                                 └─ OCR_ENABLED=true  → text-poor pages → i2ocr → merged sections
Photo / image file (.jpg .png .webp .tif .bmp .gif) ─ OCR_ENABLED=true → one-page document
                                                       OCR_ENABLED=false → refused (unchanged)
```

* **Off by default.** `OCR_ENABLED=false`. With it off, no code path changes: the existing refusals,
  tests and messages stay exactly as they were.
* **Third party.** When it is on, the *rendered pages* of a text-poor document (or the photo itself)
  are uploaded to i2ocr.com. Its policy (stated on `/features`) deletes inputs and outputs within
  30 minutes; the bot stores only the recognised text, in its own SQLite document cache.
* **No local engine.** There is no Tesseract, no poppler, no model download, no system binary — the
  recognition is remote. The only new package is the optional `curl-cffi` extra (`.[ocr]`).
* **Never for text documents.** A PDF that already carries a text layer is never uploaded, and
  `.docx`/`.pptx` never touch the service.

## Does the upstream client work?

Verdict from a line-by-line review plus an offline protocol simulation
(`tests/test_ocr.py` drives the real client code against an in-process mock of every endpoint):

| Question | Answer |
| --- | --- |
| Is the protocol real? | Yes. Five endpoints, one ALTCHA proof-of-work challenge, a multipart upload, a PDF→PNG renderer and an HTML page that carries the text. It matches the public site behaviour. |
| Does the reference code work as written? | For a happy-path single image, yes. For this bot, no: see the defect table below. |
| Does it work in production? | Unverified **here** — this repository's CI cannot reach i2ocr.com, and a test suite must not depend on a third-party site. Verify on the deployment with the smoke test at the end of this page before enabling it for users. |
| Is it safe to run inline with asyncio? | No. Every upstream call is blocking; running it on Telethon's loop would stall the bot for the duration of an upload. |

## Protocol

| Step | Request | Response |
| --- | --- | --- |
| Session | `GET /` | cookies |
| Challenge | `GET /DevSDK/challenge.php` | `{algorithm, challenge, salt, signature, maxnumber}` |
| Solve | `POST /DevSDK/challenge.php` `payload=<base64 JSON>` | `1` (and an authorising cookie) |
| Upload | `POST /DevSDK/upload.php` multipart `service`, `upload_file[]` | `{"status": "1"}` |
| Render page | `POST /DevSDK/ajax/pdf2png.php` `id=<page>` | page image file name |
| Recognise | `POST /DevSDK/ajax/ocr_backend.php` `data=<JSON>` | HTML with `$("#ocrTextBox").val("…")` |

The challenge is ALTCHA: find the integer `n ≤ maxnumber` for which
`sha256(salt + str(n)) == challenge`, then send
`base64({"algorithm","challenge","number","salt","signature","took"})`. It is a proof of work, not a
security boundary; it exists to slow automated clients down.

## Defects found in the reference client (all fixed here)

| # | Defect in `i2ocr-py` | Consequence | Fix in `gamas_bot/ocr.py` |
| --- | --- | --- | --- |
| 1 | `raw.encode("utf-8").decode("unicode_escape")` fallback | Persian/Arabic/Kazakh text becomes `Ø³…` mojibake as soon as a page contains a lone `\` (a formula, a Windows path) — the exact case OCR output is full of | `_js_unescape`: JSON first, then a UTF-8-safe manual unescape that keeps unknown escapes |
| 2 | `<textarea>` fallback returns raw HTML | `&amp;`, `&lt;` reach the user | `html.unescape` |
| 3 | `ocr_pdf` reads exactly one page | A 20-page scan needs 20 uploads, and the page name the service returns is discarded | one upload, every requested page, server-provided page name honoured |
| 4 | no retries | one `429`/`503` loses the whole document | bounded retries with jittered backoff, honouring `Retry-After` |
| 5 | `_authenticate_human()` returns `False` | the caller raises a generic `RuntimeError`; an expired challenge is never retried | `OcrChallengeError` + one automatic re-solve per page |
| 6 | raw file name in the multipart header | a name with `"` or CR/LF corrupts the body (header injection) | ASCII-sanitised, length-bounded file name |
| 7 | `Path.read_bytes()` unguarded | a 500 MB upload is read into RAM before anyone checks | existence, non-empty and `max_bytes` checks first |
| 8 | `r.json()` unguarded | an HTML error page surfaces as `JSONDecodeError` | typed error naming the stage and quoting the service's message |
| 9 | server-supplied `maxnumber` iterated blindly | a hostile challenge pins a thread for minutes | capped at 2,000,000 and a wall-clock deadline |
| 10 | `took = random.randint(500, 1200)` | the payload reports a fabricated duration | measured duration |
| 11 | eager, unretried warm-up in `__init__` | one network blip breaks construction; the session is never closed | lazy session, retried warm-up, `close()` |
| 12 | no throttling or serialisation | parallel pages race the free tier and trip its limits | one request at a time, minimum interval 1.5 s |
| 13 | `lang="kazakh"` default in the client and example | wrong model for a Persian bot (confident garbage) | explicit language, `fa`/`farsi`/`فارسی` aliases, validated against the documented language list |
| 14 | blocking calls, no thread contract | stalls the asyncio loop | synchronous API called through `fileio.run_file_io`, with an internal lock |

## How the bot uses it

1. `GamasBot` builds a blocking hook only when `OCR_ENABLED=true` (`_ocr_extractor`).
2. `doc_extract.extract_document(..., ocr=hook, ocr_max_pages=N)` runs in the existing document
   worker thread. It never imports the network layer itself.
3. A PDF whose whole text layer is below ~10 characters per page is OCR'd **page by page**; pages
   that already have text keep it, and only text-poor pages are uploaded.
4. A photo or image file is OCR'd into a one-page document (`source_type = 'image'`, migration 017).
5. If the service fails, the user sees «خواندن تصویری سند ناموفق بود…» and **nothing is stored**, so
   a retry is clean. If the service succeeds but finds nothing, the Persian message asks for a
   clearer photo instead of inventing an empty document.
6. The original file always stays in Telegram; the OCR text is stored in the same `documents` cache
   every other study document uses, so notes, quizzes and flashcards work unchanged.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCR_ENABLED` | `false` | Master switch. `check()` refuses to start with `true` when `curl-cffi` is missing. |
| `OCR_LANGUAGE` | `persian` | i2ocr slug. `fa`, `farsi`, `فارسی`, `en`, `ar`, `kk`… are aliased; an unknown value logs a warning and falls back to Persian. |
| `OCR_MAX_PAGES` | `20` | Most pages of one document sent to the service (`0` = every page). The first text-poor pages are used. |
| `OCR_TIMEOUT_SECONDS` | `600` | Wall-clock budget for one document. Pages not reached inside it are simply absent; the rest of the document is stored normally. |

Install the transport once per deployment:

```bash
pip install 'gamas-bot[ocr]'      # or: pip install "curl-cffi>=0.6,<0.18"
```

`curl-cffi` is deliberately **not** in `requirements.txt`: cPanel shared hosting installs that file
and may not have a matching wheel. It is a bounded optional extra in `pyproject.toml`.

## Limits, cost and risks

* **Free tier, one image at a time.** No API key exists, so the bot serializes OCR work: a second
  document waits in the queue instead of receiving a rate-limit rejection. A 20-page scan takes
  roughly 2–6 minutes; that is why OCR runs inside the existing document progress bar.
* **Accuracy varies.** Printed Persian/Kazakh/Russian is good; handwriting is explicitly weaker (the
  service says so itself). Multi-column layouts are handled server-side; tables lose structure.
* **Terms of service.** The service is free and requires no registration, but it is a third-party
  website with no SLA. The client solves its CAPTCHA proof of work programmatically. An operator who
  is uncomfortable with either point should leave `OCR_ENABLED=false`; nothing else depends on it.
* **Privacy.** Only text-poor pages leave the host, never whole libraries, and never a document that
  already has a text layer. Uploads are deleted by the service within 30 minutes per its policy.
  Say so in your own user-facing copy if you enable it.
* **No billing impact.** OCR is free and does not touch the credit ledger. It only makes a document
  readable; generating notes/quizzes from it still uses the normal rules.

## User-facing messages

| Situation | Message |
| --- | --- |
| OCR off, scan or photo | «OCR پشتیبانی نمی‌شود…» (unchanged) |
| OCR on, service failed | «خواندن تصویری سند ناموفق بود (سرویس OCR در دسترس نیست یا پاسخ نداد)…» |
| OCR on, nothing recognised | «…خواندن تصویری آن هم متنی پیدا نکرد…» / «متنی در این تصویر شناسایی نشد…» |

## Tests

```bash
python -m pytest tests/test_ocr.py tests/test_ocr_integration.py -q
```

`tests/test_ocr.py` runs the real client against an in-process mock of the service (challenge,
multipart upload, page renderer, HTML result, retries, hostile file names, budget cut-off,
concurrency). `tests/test_ocr_integration.py` covers the extractor hook and the bot flow for a
scanned PDF, a photo, and both failure modes.

## Live smoke test before enabling

The offline suite cannot reach i2ocr.com, so verify the protocol once on the deployment host with a
file of your own:

```bash
pip install 'gamas-bot[ocr]'
python -m scripts.ocr_smoke scan.jpg
python -m scripts.ocr_smoke lesson.pdf --pages 1,2 --language persian
```

It prints the size of the recognised text per page (never the text itself) and exits non-zero on
failure. Any `OcrError` names the stage that changed — `challenge`, `upload`, `pdf2png` or
`ocr_backend` — which is exactly what a future fix needs.

## The upstream bug, reproduced

The reference client's fallback decodes with `raw.encode("utf-8").decode("unicode_escape")`. Feeding
both implementations the same page:

```text
page : $("#ocrTextBox").val("مسیر C:\Users و فرمول \frac{1}{2}");
i2ocr-py : 'Ù\x85Ø³Û\x8cØ± C:…sers Ù\x88 Ù\x81Ø±Ù\x85Ù\x88Ù\x84 \x0crac{1}{2}'
ours     : 'مسیر C:\Users و فرمول \frac{1}{2}'
```

That is why every recognition is decoded by `gamas_bot.ocr._js_unescape` and pinned by
`tests/test_ocr.py::OcrTransportTests::test_lone_backslash_keeps_persian_text_intact`.
