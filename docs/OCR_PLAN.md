# OCR plan — Persian, Arabic and English text from images and scanned PDF

> **Status: proposed (2026-10-19).** Nothing in this document is implemented
> yet. When it lands, the operator-facing description goes in
> [OCR.md](OCR.md) and the change is recorded in [CHANGELOG.md](CHANGELOG.md);
> this page is kept as the design record — it explains *why* the design looks
> the way it does, and which alternatives were rejected.

---

## 1. Executive summary

The bot already has a complete, well-tested pipeline for turning *speech* into
a Persian RTL Word booklet. OCR is the same pipeline with a different first
stage:

```
today:   Telegram → queue → media worker → STT  ──┐
                                                   ├→ transcript engine → raw .txt → notes → RTL docx
proposed: Telegram → queue → image normaliser → OCR ─┘
```

The single most important consequence: **OCR output is a `RawTranscript`**.
`transcript.RawTranscript.from_text()` already exists, `build_document()`
already accepts plain text, and everything downstream — typographic cleanup,
paragraphing, the raw `.txt` delivery, the note model, the evidence gate, the
RTL Word export — works unchanged. OCR is therefore *not* a new product; it is
a new **intake kind** and a new **provider layer**, and roughly 80 % of the
work is configuration and plumbing.

What is genuinely new, and genuinely hard, is everything Persian-specific:
Arabic vs Persian glyphs, digit scripts, ZWNJ, mixed-script lines, and the
fact that a misread digit in the OCR text becomes *the source of truth* that
the evidence gate then faithfully propagates.

**Recommended path:** a vision-language model through the gateway the bot
already uses (no new credentials, no new system dependency, best Persian
accuracy), with a dedicated-OCR API as the second failover and local Tesseract
as a third, optional one.

---

## 2. What already exists — the leverage

Do not rebuild any of this. Reuse it as-is.

| Existing asset | Where | How OCR uses it |
| --- | --- | --- |
| `RawTranscript.from_text(text, engine=…)` | `transcript.py:94` | The OCR result *is* a raw transcript. No new document model. |
| `build_document(raw)` | `transcript.py:385` | Cleans + paragraphs the OCR text with the fidelity gate. |
| `TranscriptDocument.llm_document()` | `transcript.py:133` | The note model's input. |
| `render(document, profile)` | `transcript.py:411` | The raw `.txt` delivered to the user. |
| `structure_transcript(...)` | `notes.py` | Note generation — unchanged. |
| `notes_evidence.gate_notes(...)` | `notes_evidence.py` | The "nothing invented" gate — unchanged. |
| `build_notes_docx(...)` | `docx_export.py` | The RTL booklet — unchanged. |
| Provider failover driver | `stt.py:645-825` | The pattern `ocr.py` copies: `ProviderSpec`, ordered failover, per-provider cooldown, bounded retries, upload ceilings. |
| `normalize_display` / `normalize_persian` / `normalize_digits` | `textnorm.py` | The Persian glyph + digit folding applied to OCR output. |
| `reserve` / `settle_hold` / `release_hold` | `db.py:499,540,580` | Billing — OCR pages are billed as seconds. |
| `JobProgress` | `progress.py` | The "🔍 دارم تصویر را می‌خوانم" progress messages. |
| Media worker | `media_worker.py` | Image decode/normalise runs in the same isolated child process. |

The `stt.py` → `ocr.py` copy is deliberate, not accidental duplication:
`stt.py` and `llm.py` already each own their provider clients, and `ocr.py`
being self-contained means a change to the *notes* path can never break
*OCR*, and vice versa.

---

## 3. The three languages, and what each one actually demands

| | Script | Glyph traps | Notes |
| --- | --- | --- | --- |
| **Persian** (`fa`) | Arabic | `ک`/`ك`, `ی`/`ي`/`ى`, `ه`/`ة`; ZWNJ `‌`; `،` `؛` `؟` | The primary language. `textnorm` already folds the letter traps. |
| **Arabic** (`ar`) | Arabic | `ة` vs `ه`; `ى` vs `ي`; hamza forms `ؤإئ`; no ZWNJ | Same folding is *wrong* for strict Arabic (`ة`→`ه` changes meaning). |
| **English** (`en`) | Latin | — | Must survive byte-for-byte: `HbA1c`, `mg/dL`, `COVID-19`. |

Two consequences that fall straight out of the table:

1. **The Arabic→Persian letter folding in `textnorm.normalize_persian` is a
   Persian-only transform.** Folding `ة`→`ه` is correct for Persian and wrong
   for Arabic. If Arabic documents are in scope, the folding must be
   **language-scoped**: apply `normalize_persian` only to `fa` output, and
   leave `ar` output alone (or fold only the unambiguous subset `ك`→`ک`,
   `ي`→`ی`). This is a small change to `textnorm.py` and it is **required for
   correctness**, not cosmetic.

2. **Persian OCR output is usually *Arabic-keyboard* output.** A scanned page
   typed on an Arabic keyboard produces `ك` and `ي` where the author meant `ک`
   and `ی`. The same applies to OCR engines trained on Arabic pages. This is
   exactly what `textnorm.normalize_persian` already fixes — so the OCR path
   should call it, and the existing STT path is already consistent with it.

---

## 4. Provider options, ranked

### 4.1 `vision_llm` — a vision model through the existing gateway *(recommended first)*

Send the image to a multimodal chat model and ask for a verbatim
transcription.

* **Why first:** the bot *already* has an OpenAI-compatible + Gemini +
  Anthropic + OpenRouter/Nara client layer in `llm.py`, and already-reachable
  gateways (the deployment is evidently using `router.bynara.id` /
  OpenRouter). Zero new credentials, zero new system packages.
* **Accuracy:** vision models beat Tesseract by a wide margin on phone
  photos, handwriting and mixed-script pages — on the 2026 olmOCR-Bench,
  Tesseract scored **34.4 %** overall against **77–83 %** for OCR-trained
  vision models; on Arabic-script benchmarks, Tesseract lands around
  **78–84 % CER** while good vision models sit in the low single digits.
* **Cost:** the same free/shared gateway the notes already use. Image tokens
  are more expensive than text tokens, so `OCR_MAX_PAGES` and a downscale
  step are mandatory, not optional.
* **Risk — the real one:** a vision model asked to "read this page" may
  *summarise*, *normalise* or *confidently mis-transcribe*. It is
  probabilistic where Tesseract is deterministic. Two defences, both
  required:
  1. a transcription prompt that forbids summarisation, normalising and
     "fixing" (see §9);
  2. the **raw OCR text is delivered to the user before the notes**, exactly
     as the STT path does today. The user sees what was actually read; the
     booklet is downstream of it. This is the repo's existing invariant and
     it happens to be the right invariant for OCR too.

### 4.2 `google_vision` / `azure_vision` — dedicated cloud OCR *(recommended second)*

* **Google Cloud Vision `DOCUMENT_TEXT_DETECTION`** documents `fa`, `ar` and
  `en` as supported languages, with `languageHints` for multi-language pages
  and layout-aware output (pages → blocks → paragraphs → words). ~$1.50/1k
  pages. Auth is a service-account JSON, which fits the repo's
  *keys-in-the-environment-only* rule as a base64 blob.
* **Azure AI Vision Read** supports `fa`, `ar`, `en` with a plain
  `AZURE_COMPUTER_VISION_KEY` + endpoint — the most natural fit for this
  repo's key model. ~$1/1k pages.
* **Reality check:** the bot's users are Persian and its gateways are
  Iranian-hosted. Google and Azure may be unreachable or slow from the
  deployment host. Treat these as *optional* providers and verify
  reachability before committing to them — this is the one risk that could
  invalidate the plan on day one.

### 4.3 `tesseract` — local, free, offline *(optional third)*

`fas+ara+eng` traineddata, run as a child process like the media worker.

* **Upside:** zero marginal cost, no data leaves the server (a genuine plus
  for the README's Privacy section), works with no network at all.
* **Cost:** it is a **system binary**, which breaks the project's explicit
  "no system media binaries" rule (`ffmpeg`, `ffprobe`, `soffice` are all
  avoided on purpose). On cPanel/Passenger that is a fight, not a checkbox.
* **The stock Persian model is defective**, and this is worth knowing before
  choosing it. The upstream `fas` traineddata:
  * has **no Latin letters in its unicharset** → English words inside a
    Persian page come out as garbage. Fix: always run `fas+ara+eng` together
    so the merged unicharset has Latin.
  * **cannot emit the Persian comma `،` (U+060C)** — it is in
    `forbidden_characters`. No model change fixes this; it needs a
    post-processing rule or an accepted limitation.
  * mixes `ي`/`ی` and `ك`/`ک` in roughly 32 % / 30 % of characters — but
    `textnorm.normalize_persian` **already neutralises exactly this**, so
    the defect costs nothing downstream.
  * fine-tuned alternatives exist: `givia/tesseract-fas` publishes a
    `fas_print` model with **1.14 % CER** against the stock model's
    **5.38 %** on real Persian book scans.
* **Verdict:** worth adding as a *free offline fallback*, not as the primary
  engine.

### 4.4 Hybrid: Tesseract prior → vision model correction *(the accuracy play)*

Run Tesseract, then give the vision model **both** the image and Tesseract's
noisy text, and ask it to correct the text against the image.

The 2026 cross-dataset study on Arabic OCR found this reduces CER by **8.6
absolute points** over Tesseract alone — but only when the prior is
*visually and textually recoverable*; it **hurts** when the prior is
script-mismatched or systematically misleading. For printed Persian/Arabic
pages the prior is recoverable, so the hybrid should win.

This is cheap to implement *because the architecture already separates
concerns*: providers produce text, a separate stage consumes it. It is a
second stage in `ocr.py`, not a new provider.

### 4.5 Decision

| Rank | Provider | Effort | Accuracy (Persian) | Cost | New auth | New system dep |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `vision_llm` (via existing gateway) | low | high | free/shared | **none** | none |
| 2 | `azure_vision` / `google_vision` | medium | high | per page | yes | none |
| 3 | `tesseract` (+ `fas_print`) | medium | medium | free | none | **yes (binary)** |
| 4 | hybrid (2 → 1) | low | highest | free/shared | none | yes (for 3) |

---

## 5. Module design — `gamas_bot/ocr.py`

A new module mirroring `stt.py`, ~350 lines, with **no** dependency on
`llm.py` or `stt.py`.

```python
# errors — a 1:1 mirror of stt.py's taxonomy
class OCRError(RuntimeError): ...
class OCRConfigurationError(OCRError): ...
class OCRAuthenticationError(OCRError): ...
class OCRTransientError(OCRError): ...        # + retry_after_seconds
class OCRRequestError(OCRError): ...

@dataclass(frozen=True, slots=True)
class OCRResult:
    text: str                 # logical-order plain text, all pages
    pages: int
    engine: str               # "vision_llm:gpt-4o-mini" etc.
    confidence: float | None
    languages: tuple[str, ...] # what was actually detected/found
    warnings: tuple[str, ...]  # e.g. "numbers disagree between engines"

@dataclass(frozen=True, slots=True)
class ProviderSpec:
    slug: str
    label: str
    configured: Callable[[Settings], bool]
    max_upload: int
    max_pages: int
    run: Callable[[aiohttp.ClientSession, Path, Settings], Awaitable[OCRResult]]

PROVIDERS: dict[str, ProviderSpec] = {...}

async def recognize(images: list[Path], settings, *, session=None) -> OCRResult:
    ...   # ordered failover, same shape as stt.transcribe()
```

Everything `stt.py` already gets right should be copied verbatim:
`_set_cooldown` / `_cooldown_remaining` (in-memory per process), 15-minute
auth cooldown, 60-second exhausted-retry cooldown, `MAX_TRANSIENT_RETRIES = 2`,
`Retry-After` honoured but clamped to 60 s, exponential backoff capped at
15 s, upload ceiling checked **before** the request, and — importantly — **a
response containing no letter or digit is not a transcription**: it fails
over, because delivering and billing an empty `.txt` would report a failure as
success.

`ocr.py` should be added to the `files = [...]` list in `pyproject.toml`
`[tool.mypy]` so it joins the incremental typing gate from day one.

---

## 6. Configuration surface

New `Settings` fields in `config.py` + a matching block in `.env.example`.
**Both are mandatory**: `tests/test_config_consistency.py` fails the build if
`.env.example` and `config.py` drift in either direction, and every value
listed there must also be added to `DOCUMENTED_DEFAULTS` in that test.

```dotenv
# ── OCR (تصویر و PDF اسکن‌شده → متن) ─────────────────────────────────────────
# روشن/خاموش کردن کل بخش.
OCR_ENABLED=true
# ترتیب failover؛ فقط سرویس‌های دارای کلید امتحان می‌شوند.
#   vision_llm | azure_vision | google_vision | tesseract
OCR_PROVIDERS=vision_llm,tesseract
# زبان‌هایی که از تصویر انتظار می‌رود؛ «auto» یعنی خود سرویس تشخیص دهد.
OCR_LANGUAGES=fa,ar,en
# بیشترین تعداد صفحه در یک کار.
OCR_MAX_PAGES=20
# بیشترین حجم هر تصویر (بایت)؛ بزرگ‌تر پذیرفته نمی‌شود.
OCR_MAX_FILE_BYTES=10485760
# بیشترین ضلع تصویر پیش از ارسال به سرویس (پیکسل)؛ کوچک‌کردن، هزینه و زمان را کم می‌کند.
OCR_IMAGE_MAX_EDGE=2000
# مهلت هر درخواست OCR (ثانیه).
OCR_TIMEOUT_SECONDS=180
# زبان‌های Tesseract (وقتی فعال است) و حالت تجزیهٔ صفحه.
OCR_TESSERACT_LANGS=fas+ara+eng
OCR_TESSERACT_PSM=6
# مسیر متن OCR: فقط متن خام، یا متن خام + جزوهٔ ساختارمند.
OCR_OUTPUT_MODE=raw,notes
# اعتبار مصرفی هر صفحهٔ OCR (ثانیهٔ اعتبار)؛ دفتر کل بر حسب ثانیه است.
OCR_BILLING_SECONDS_PER_PAGE=60
```

`config.check()` gains an advisory when `OCR_ENABLED=true` and no provider
has credentials — and a *hard* problem when `OCR_OUTPUT_MODE` contains
`notes` but no note provider is configured.

### Dependencies

Only two, both pure-Python wheels, both optional:

| Package | Why | Alternative |
| --- | --- | --- |
| `Pillow` | `ImageOps.exif_transpose` (phone photos carry an orientation flag — a rotated image destroys OCR), downscale, grayscale, autocontrast, PNG encode for the data URL | `av` can decode, but has no EXIF-orientation or autocontrast helpers |
| `pypdfium2` | Rasterises PDF pages to bitmaps — pure pip, bundles PDFium, **no system binary** | `pypdf` is test-only and cannot rasterise |

If the dependencies are added, `pyproject.toml` is the source of truth and
`requirements.txt` **must** be regenerated with
`python -m scripts.sync_requirements` — CI runs `sync_requirements --check`
and fails otherwise.

---

## 7. Intake and pipeline changes, file by file

### `gamas_bot/bot.py`

1. **New classifier** `_image_metadata(message) -> tuple[str | None, str | None, int]`:
   * `message.photo` → `("image", filename, page_count=1)`;
   * `message.document` with `mime.startswith("image/")` → `("image", …)`;
   * `message.document` with `.pdf` → `("pdf", …)`.
   * **Ordering matters:** the receipt path (`_is_receipt_image`, checked at
     `bot.py:952`) must keep winning over OCR — it already does, because it is
     tested *before* `_media_metadata` at `bot.py:972`. Keep that order and
     add a test that pins it.
2. **Route** in `_handle_message`: after the presentation check and *before*
   the media check, `if image_kind is not None: await self._accept_image(...)`.
3. **New `_accept_image(...)`** — mirrors `_accept_media` (`bot.py:1001`):
   reported-size pre-check against `OCR_MAX_FILE_BYTES`, credit pre-check
   against `pages × OCR_BILLING_SECONDS_PER_PAGE`, `create_submission(user_id,
   "image", filename)`, `JobProgress.create`, `queue.submit`.
4. **New `_process_image(job, workdir, progress)`** — the mirror of
   `_process_media` (`bot.py:1193`):
   * download → `source.<ext>`; re-check the byte ceiling after download;
   * normalise the image (Pillow) → one PNG per page, capped at
     `OCR_MAX_PAGES`;
   * `reserve(user_id, submission_id, pages × seconds_per_page)`;
   * `recognize(images, settings, session=self._http_session())`;
   * `document = build_document(RawTranscript.from_text(result.text, engine=result.engine))`;
   * **deliver the raw `.txt` first** (same `raw_text_filename` /
     `_deliver_raw` / `_send_preview` path as STT) — *then* notes, *then* the
     booklet, exactly like today;
   * `settle_hold(submission_id, pages × seconds_per_page)`;
   * `save_transcription(submission_id, engine=result.engine, text=document.plain(), …)`.
5. **`_run_job` dispatch** (`bot.py:1112`): add `elif job.kind == "image":
   await self._process_image(...)`.
6. **`_is_unsupported_attachment`** currently rejects photos. After the
   change, a photo is no longer unsupported — update the user-facing message
   to list the new formats.
7. **Error taxonomy:** add `OCRError` to `USER_VISIBLE_ERRORS` so an OCR
   failure produces `❌ …` rather than the generic failure message, and add a
   dedicated `except OCRError` arm in `_run_job` next to the `except STTError`
   arm, so a failed OCR **releases the hold**.

### `gamas_bot/media_worker.py` + `gamas_bot/media.py`

Image decode and normalisation belong in the **child process**, for the same
reason the media pipeline does: a native crash in an image library must take
down the worker, never the bot, and the parent's event loop must never block.
Add an `images` command to the worker (`normalise` / `rasterise`) and the
matching `build_*_command` + `run_command` wrapper in `media.py` — the
existing `run_command` already enforces the timeout, never uses a shell, and
kills the process group on POSIX.

### `gamas_bot/textnorm.py`

Make the Persian letter folding **language-scoped**:

```python
def normalize_for_language(text: str, language: str) -> str:
    """Fold Arabic-keyboard glyphs to Persian only for Persian output."""
```

`normalize_persian` keeps its current behaviour (it is used by the STT path
and is correct there, because the bot is Persian-first); the new helper
decides *whether* to call it for a given OCR language.

### `gamas_bot/notes.py` / `notes_evidence.py`

No change. The OCR text is the source of truth, exactly like a transcript,
and the evidence gate applies verbatim.

One addition worth making in `notes.py`: the note system prompt should say
the source may be an **image transcription**, so the model does not try to
"repair" what it believes is a bad transcript. A one-line addition to
`build_system_prompt`.

---

## 8. Billing — the part that needs a decision

The ledger is **integer seconds of media duration** (`db.py:499`). OCR has no
duration. Three options:

| Option | Effort | Honesty | Recommendation |
| --- | --- | --- | --- |
| **(a) A page costs a fixed number of seconds** (`OCR_BILLING_SECONDS_PER_PAGE`, default 60) | ~0 — `reserve()`/`settle_hold()` already take a seconds count | approximate | ✅ **recommended for v1** |
| (b) A new billing unit (credits per page) | schema migration, plan catalogue, admin panel, every formatter | exact | v2, if the unit ever needs its own pricing |
| (c) OCR is free | ~0 | — | only if OCR is a loss-leader for the note product |

With (a) and a 60 s/page rate, the free 1-hour plan covers **60 pages**,
and the 5-hour plan covers 300 pages. That is a defensible number for a
scanned lecture, and it needs **zero** schema change, zero plan-catalogue
change and zero admin-UI change.

The reserve/settle/release discipline must be identical to the media path:
reserve at the start, settle with the measured page count, **release in full
on every failure path** (including the OCR provider raising).

`docs/BILLING.md` gains a paragraph: *an OCR page is billed as N seconds of
credit; the rate is `OCR_BILLING_SECONDS_PER_PAGE`.*

---

## 9. The OCR prompt, and the Persian post-processing rules

### 9.1 The transcription prompt

For the vision-LLM provider, the prompt must forbid exactly the failure
modes that matter for a study bot:

> You are an OCR engine. Transcribe the page **verbatim**. Do not summarise,
> do not translate, do not correct spelling, do not reformat, do not add
> headings that are not on the page, do not complete a truncated word, and do
> not drop repeated lines. Preserve the original line breaks as separate
> lines and keep the reading order of the page. If a region is illegible,
> write `[ناخوانا]` and nothing else. Numbers, units and dosages must be
> copied exactly as printed. Output plain text only — no markdown, no code
> fences, no commentary.

### 9.2 Deterministic post-processing (in `ocr.py`, not in the model)

| Rule | Why |
| --- | --- |
| Strip the model's preamble/suffix ("Here is the transcription:") | Models add it even when told not to |
| Strip markdown fences and `**bold**` | Models add them despite the instruction |
| Fold Arabic-keyboard glyphs → Persian, **only for `fa`** | See §3.1 |
| Fold Arabic-Indic digits (`٠-٩`) and Extended Arabic (`۰-۹`) → ASCII | Makes the evidence gate's number comparison consistent across engines; the renderer converts back to Persian for display |
| **Never** touch ZWNJ, Latin tokens, units, or digit *values* | `textnorm` already has this contract; do not weaken it |
| Join lines into paragraphs on blank-line / block boundaries | A vision model returns lines; the transcript engine expects paragraphs. `paragraphs_from_text` already splits a blob, so the safe move is to emit **blank-line-separated blocks** and let `build_document` split them |
| Detect and strip a repeated model echo | Some models repeat the page twice |

Every one of these is a pure function and gets a unit test. The repo's
post-processing layers are all deterministic and model-free on purpose; OCR
post-processing belongs in the same camp.

### 9.3 The ZWNJ caveat, stated honestly

OCR and VLM transcription of Persian frequently lose the ZWNJ (`می‌شود` →
`می شود`) or insert a space where it belongs. Restoring it reliably needs a
Persian morphological model, and a wrong restoration is worse than a missing
one. **Recommendation: do not attempt it.** Record it as a known limitation.
`normalize_for_compare` already strips ZWNJ before comparison, so the
evidence gate is unaffected.

---

## 10. The number-safety problem (the hardest part)

The bot's core promise is *"nothing invented, nothing lost"*. The evidence
gate enforces that promise **against the source**. But for OCR, the source
*is* the OCR text. A misread `5 mg` → `50 mg` is not caught by the gate —
the gate will faithfully protect the wrong number, because that is what the
source says.

This is the one place where the OCR path needs its own defence. In
increasing order of cost:

1. **Prefer accuracy** — vision model first, `fas_print` Tesseract if used.
2. **Never let the note model touch the numbers** — already true; the notes
   engine restructures, it does not author. Keep it that way.
3. **Cross-engine number check** — when two engines are configured, run both
   and compare the extracted number sets (the repo already has the machinery:
   `notes_evidence` extracts number+unit pairs and folds both digit
   scripts). Report the disagreement to the user *in the delivered text* —
   e.g. a line at the top: `⚠️ در عدد ۲۵ اختلاف بین دو موتور دیده شد` — rather
   than silently picking one. This mirrors the repo's philosophy exactly:
   *the system reports what it cannot verify instead of guessing.*
4. **Flag low-confidence pages** — if the provider reports a confidence or the
   text is suspiciously short for the page, tell the user to verify.

Do **not** try to be clever here. A "smart" number-correction pass that
guesses `5 mg` vs `50 mg` will eventually produce a confidently wrong
medical dosage. The right behaviour is to surface the uncertainty.

---

## 11. Phases

Each phase ends green: `ruff check . && python -m mypy && python -m pytest -q`.

### Phase 0 — decisions *(no code)*
Settle the three questions in §16. Confirm the deployment host can reach the
chosen provider. **Exit:** a one-line statement of the provider chain.

### Phase 1 — foundation *(no provider wired up yet)*
1. `gamas_bot/ocr.py`: errors, `OCRResult`, `ProviderSpec`, `PROVIDERS`,
   `recognize()` failover driver, cooldowns, retries, upload/page ceilings.
2. `config.py`: the new `Settings` fields, `from_env`, `check()`.
3. `.env.example`: the new block.
4. `tests/test_ocr.py`: the driver, with a scripted fake session — failover
   order, retry/cooldown, ceilings, empty-response rejection, error
   classification. **No paid calls, no API key.** (Mirrors `tests/test_stt.py`.)
5. `tests/test_config_consistency.py`: the new `DOCUMENTED_DEFAULTS` rows.
**Exit:** `--check` reports the OCR configuration; the failover driver is
fully tested with zero providers configured.

### Phase 2 — first real provider + the whole path end-to-end
6. `vision_llm` provider (OpenAI-compatible `image_url` data URL; Gemini
   `inline_data` and Anthropic images if the deployment uses them).
7. Image normalisation in the media worker: EXIF transpose, downscale to
   `OCR_IMAGE_MAX_EDGE`, grayscale + autocontrast, PNG encode.
8. `Pillow` dependency + `python -m scripts.sync_requirements`.
9. `bot.py`: `_image_metadata`, `_accept_image`, `_process_image`, dispatch,
   `OCRError` in `USER_VISIBLE_ERRORS`, updated unsupported-attachment copy.
10. Billing: reserve → settle → release, at `OCR_BILLING_SECONDS_PER_PAGE`.
11. `textnorm.normalize_for_language`.
12. Deterministic post-processing + its tests.
13. `docs/OCR.md`, `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`,
    `docs/BILLING.md`, `docs/TROUBLESHOOTING.md`, `README.md`,
    `docs/CHANGELOG.md`.
**Exit:** a photo of a Persian page comes back as a raw `.txt` **and** an RTL
Word booklet, billed correctly, and a provider outage releases the credit.

### Phase 3 — production hardening
14. Scanned PDF via `pypdfium2` (page rasterisation → the same OCR path).
15. `OCR_MAX_PAGES` enforcement end-to-end, with the page count known
    *before* the credit reservation.
16. Number cross-check between two engines (§10.3).
17. Admin panel: OCR jobs appear in the overview (`submissions.kind` already
    stores it; the admin screen may need to group by it).
**Exit:** a 20-page scanned PDF is processed, billed, and delivered.

### Phase 4 — optional
18. `tesseract` provider as the offline fallback (system binary; document the
    cPanel caveat), optionally with the `fas_print` model.
19. Hybrid Tesseract → vision-model correction (§4.4).
20. Preprocessing (deskew/denoise) for phone photos.
21. OCR of the images *embedded in a PPTX* — this closes the gap
    `docs/PRESENTATIONS.md` currently states out loud: *"Text that exists
    only as an image is not OCR'd."*
**Exit:** each of these is independently shippable and independently
revertable.

---

## 12. Testing

| File | Covers |
| --- | --- |
| `tests/test_ocr.py` | Provider request shapes, language mapping, failover order, bounded retries, `Retry-After` clamping, cooldowns, upload/page ceilings, empty-response rejection, error classification. Scripted fake session — **no paid call, no key**. |
| `tests/test_ocr_text.py` | The deterministic post-processing: glyph folding, language-scoped folding, digit folding, ZWNJ preservation, preamble/fence stripping, block joining, line-echo removal. Pure functions, no I/O. |
| `tests/test_ocr_billing.py` | Reserve/settle/release with the per-page rate; a provider failure releases the full hold. |
| `tests/test_ocr_intake.py` | Classification: photo vs. image file vs. PDF vs. unsupported; **the receipt-photo path still wins**. |
| `tests/test_config_consistency.py` | The new `DOCUMENTED_DEFAULTS` rows (CI already enforces `.env.example` ↔ `config.py` parity). |

The existing suite must keep passing untouched — that is the proof that OCR
did not destabilise the audio path.

A real-image fixture is *not* required: the provider layer is tested at the
HTTP boundary (like `test_stt.py`), and the post-processing layer is pure. If
a fixture is wanted later, a small base64 PNG of a Persian+English line
belongs in `tests/fixtures/ocr/`.

---

## 13. Documentation to update

| File | Change |
| --- | --- |
| `docs/OCR.md` | **New** — the operator page: providers, the three languages, limits, failover, diagnosis table, deliberate non-features. Modelled on `docs/STT_PROVIDERS.md`. |
| `docs/README.md` | Index row for `OCR.md` (and for this plan page). |
| `docs/ARCHITECTURE.md` | Add `ocr.py` to the module map; add the OCR branch to the pipeline diagram. |
| `docs/CONFIGURATION.md` | Every new environment variable. |
| `docs/BILLING.md` | The per-page seconds rule. |
| `docs/PRESENTATIONS.md` | The line *"Text that exists only as an image is not OCR'd"* — either update it (Phase 4.21) or scope it precisely. |
| `docs/TROUBLESHOOTING.md` | Symptom → cause → where to look rows for OCR. |
| `docs/SECURITY.md` | Where the image goes (a third-party OCR provider) — the privacy contract must name it. |
| `README.md` | Supported-formats table gains "Images / scanned PDF"; the Known-limitations list loses the OCR line. |
| `docs/CHANGELOG.md` | An `Unreleased` entry, in the established style. |

The README's Privacy section is the one that most needs a real edit: today it
says *"Audio goes to the configured STT provider, the transcript to the
configured note provider."* With OCR, **images go to a third provider**, and
that sentence becomes wrong. It must be updated in Phase 2, not later.

---

## 14. Risks

| Risk | Severity | Mitigation |
| --- | --- | --- |
| **Telegram photo compression** — `message.photo` is a re-encoded, downscaled JPEG (largest size ~1280 px). OCR accuracy on a compressed phone photo is materially worse than on the original file. | high | Prefer the *document* path (the user sends the image as a file); use the largest entry of `message.photo.sizes`; tell the user in the UI: «برای دقت بیشتر، تصویر را به‌صورت فایل بفرستید» |
| **The vision model summarises instead of transcribing** | high | Strict prompt (§9.1) + deterministic preamble stripping + deliver the raw OCR text *before* the notes |
| **A misread digit becomes the source of truth** | high | §10 — cross-engine check, surface the uncertainty, never guess |
| **Google/Azure unreachable from the deployment host** | high | Prefer the gateway the bot already uses; make cloud OCR optional and verify reachability in Phase 0 |
| **Cost blowup** — a 20-page PDF is 20 image requests | medium | `OCR_MAX_PAGES`, downscale to `OCR_IMAGE_MAX_EDGE`, per-page credit reserve before the work starts |
| **Stock Tesseract `fas` has no Latin** | medium | Only ever run `fas+ara+eng`; or use the vision model |
| **Stock Tesseract `fas` cannot emit `،`** | low | Accept it as a documented limitation, or add a targeted post-processing rule |
| **A system binary breaks the deployment's promise** | medium | Tesseract is Phase 4, optional, and documented as such |
| **Persian↔Arabic letter folding corrupts Arabic** | medium | Language-scoped folding (§3.1) |
| **`config.py` ↔ `.env.example` drift** | low | CI already fails the build; add the rows in the same commit |
| **The evidence gate protects the wrong number** | high | §10 |

---

## 15. What is explicitly *not* in scope for v1

* Handwriting-grade OCR (a VLM does it well; Tesseract does not).
* Layout reconstruction into real Word tables — the OCR text is a text
  transcript; the note model builds tables from it, and the evidence gate
  checks the cells. That is the existing, proven path.
* OCR of images embedded in a PPTX (Phase 4.21).
* A new billing unit (Phase 2 uses the existing seconds ledger).
* Perfect ZWNJ restoration (§9.3).
* Any change to the STT path, the note path, or the evidence gate.

---

## 16. Decisions needed

1. **Which engine chain?** Recommended: `vision_llm` first. If the deployment
   cannot use a vision model through the existing gateway, the answer changes
   to `azure_vision` (key-based, closest to the repo's model) — and that
   changes Phase 2's provider work.
2. **Does OCR produce a booklet, or only text?** Recommended: both, via
   `OCR_OUTPUT_MODE=raw,notes` — the raw OCR text first, the booklet second,
   identical to the audio path.
3. **Is OCR billed, and at what rate?** Recommended: yes, `60` seconds of
   credit per page, no schema change.
4. **Is scanned-PDF input in v1?** Recommended: yes (Phase 3) — a scan of a
   book chapter is the most likely real request, and `pypdfium2` keeps the
   "no system binaries" promise.
5. **Is Arabic *output* in scope, or only Arabic *input* recognition?** The
   letter-folding change in §3.1 depends on this: if Arabic documents must
   come out as correct Arabic, the folding must be language-scoped.

---

## 17. Sources

* Google Cloud Vision — [OCR language support](https://cloud.google.com/vision/docs/languages)
  (`fa`, `ar`, `en`), [OCR detection](https://cloud.google.com/vision/docs/ocr)
* Azure AI Vision — [language support](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/language-support)
* Tesseract Persian — [`givia/tesseract-fas`](https://github.com/givia/tesseract-fas)
  (stock-model defects and the `fas_print` fine-tune)
* Vision models vs. Tesseract —
  [OCR guide 2026](https://slavadubrov.github.io/blog/2026/03/04/ocr-guide/),
  [dedicated OCR vs. vision LLM](https://joshua8.ai/ocr-models-vs-vision-llms-vs-tesseract/)
* Tesseract→VLM correction for Arabic —
  [When Do VLMs Help Arabic Manuscript OCR?](https://arxiv.org/html/2608.22366)
* `docs/STT_PROVIDERS.md` — the provider-page template this plan mirrors
