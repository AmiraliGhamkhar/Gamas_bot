# Audit history (superseded)

These reports were written while the billing, provider-security and RTL-DOCX
work landed. They are kept for traceability — the reasoning behind decisions
that are still visible in the code — but they are **historical**: individual
claims, scores and file inventories in them may no longer match the current
implementation. The current architecture, configuration and behaviour are
described in `ARCHITECTURE.md`, `CONFIGURATION.md` and the other canonical
pages indexed in `docs/README.md`, and are enforced by the test suite rather
than by prose.

Where a historical report contradicts the code, the code and its tests win.


---

## Deep audit (2026-09) (`AUDIT.md`, removed)

## Scope and result

Deep audit of the note-generation path — prompt construction, transcript
chunking, schema/merge, and the Word renderer — against the question a student
actually asks: *did anything the lecturer said disappear, and does the PDF/Word
file render Persian and English correctly side by side?*

Research (OOXML §17.3.2.30 `w:rtl`, `w:bidi`, complex-script run properties,
python-docx `font.rtl` serialization, `w:altName` in `word/fontTable.xml`)
confirmed five root causes; all five are fixed. **314 tests pass** (265 existing
+ 49 new across `test_note_quality.py` and `test_note_evaluation.py`). No live
Telegram login and no paid provider call was made.

## Current follow-up — 2026-10-07

This document contains dated audit snapshots; the historical results and test
counts below are not the validation status of the current branch. The current
implementation adds canonical prepaid entitlements, transaction-safe integer-
second reservations/refunds and ledger audit; private manual receipt approval;
Fernet-encrypted provider credentials; and a static DOCX TOC whose page numbers
must come from a real LibreOffice/PDF render. It fails closed when exact mapping
is unavailable. The current environment does not have LibreOffice, so actual
rendered-page accuracy, internal PDF link destinations and visual DOCX output
have **not** been validated here. See the current README and deployment guides
for configuration; report only the test runs performed for this revision.

### Revision log — 2026-10-07 (audit findings and fixes)

A second audit pass over the whole tree (bot handlers, database, STT, LLM,
DOCX/BiDi, providers, migrations, tests, docs) found and fixed the following.
Everything is minimum-change: no module was rewritten and no existing test was
removed or weakened.

| # | Finding (audit) | Fix |
|---|---|---|
| 1 | The **free plan code did not match the canonical contract**: the catalogue and migration seeded `free_lifetime_1h` while the requirement names `free_1h`. | `free_1h` is now the canonical code in `billing.py`; `migration 004` renames the legacy row in place (keeping its foreign keys) and the database resolves the free plan under either spelling, so a pre-004 database neither loses nor duplicates its one free hour. Regression test: `test_legacy_free_plan_code_is_renamed_and_never_granted_twice`. |
| 2 | The tariff was hard-coded and had **no canonical configuration keys** (`FREE_PLAN_HOURS`, `PLAN_25_*`, `PLAN_50_*`). | Added `CANONICAL_*` values plus the seven `PLAN_ENV_FIELDS` keys in `config.py`, validated at startup, exposed as `Settings.plan_values`, and consumed by `billing.plan_catalog(values)` → SQLite seed → UI. One source of truth, no numbers in handlers. |
| 3 | `payment_requests` had **no `admin_note`/`receipt_file_id`**: the requirement asks for both, and losing the local receipt under retention left no reference. | Migration 004 adds both columns; `submit_payment_receipt`, `approve_payment` and `reject_payment` write them; `payment_detail` and rejection sanitization use the same printable-text cleaner. |
| 4 | Rejection sanitization stripped the Persian **zero-width non-joiner**, corrupting reasons like «هم‌خوانی». | `database.clean_human_text()` keeps ZWNJ/ZWJ while removing control characters; used by rejection, manual credit and admin notes. Test: `test_rejection_reason_is_sanitized_before_storage`. |
| 5 | Admin menus/UX were missing the required entries: **🩺 وضعیت سرویس‌ها**, **⏱ اعتبار کاربران**, **⏱ اعتبار من**, **💳 خرید اشتراک**. | Menus renamed/extended to the approved Persian labels; the admin credit screen (`admin:credits*`) and the manual adjustment flow (`credit_add:<id>`) were added. |
| 6 | The credential panel explicitly stated it performed **no live health check**, and there was no `provider_health` module at all. | New `gamas_bot/provider_health.py`: manual, cached (300 s), free read-only probes (Speechmatics job list, Deepgram project list, Gemini/Anthropic/OpenAI-compatible model list) with the full required status vocabulary, safe fallback for unprobeable deployments, sanitized errors, and cooldown/quarantine state shared with live traffic. `admin:health`/`admin:health:refresh`/`admin:health:test:<id>`. |
| 7 | There was **no credential reorder and no per-key manual test**, both required by the admin API-key flow. | `ProviderCredentialManager.credential_for_test()` + `reorder()` with dense deterministic priorities and an audit entry per move; **▲/▼** and **🧪 تست** buttons. |
| 8 | A second image sent while a receipt was **pending** fell through to the media pipeline. | Payment state (level-triggered, read from SQLite) now answers explicitly with the pending request id, and only images can be receipts (`_is_receipt_image`). A lecture audio/video/PowerPoint upload keeps going to the normal pipeline. Tests: `test_payment_flow.py`. |
| 9 | The offline page renderer read only direct `w:r` children, so **every hyperlinked TOC row rendered blank** — a tool that would have hidden a TOC regression. | `_paragraph_runs` iterates `paragraph.iter(w:r)`. Verified visually: page 2 now shows titles + page numbers. |
| 10 | Migration/index coverage was thinner than required (no payment-review, cooldown/enabled, entitlement-status or audit-target indexes). | Migration 004 adds those indexes alongside the column additions. |
| 11 | `add_admin_credit` attributed a manual grant to the free plan row, and the code depended on the legacy plan code. | Manual credit resolves the canonical free plan row via `_free_plan_row()` (either spelling). |

**Verification performed for this revision (nothing beyond it is claimed):**

* full suite `python -m pytest tests/ -q` → **665 passed, 1 skipped, 1609 subtests passed** (44.7 s);
* `python -m compileall gamas_bot scripts tests` clean; `scripts/cpanel_preflight.py` runs (the only FAIL is the absent `.env` in this sandbox, as expected);
* `python -m scripts.validate_docx` → 0 structural failures for both sample documents (RTL defaults/styles/paragraph marks/LTR runs, RTL tables, repeating headers, running header, real `PAGE` footer field, element ordering, clean reopen);
* five real sample documents (Persian-only, Persian+English medical, 22 topics, numeric/units, links/emails) rendered with the repository's offline Pillow renderer: page 1 = cover, page 2 = topic list with titles and page numbers, page 3+ = notes, no blank/overflow/orphan warnings;
* **LibreOffice is not installed here**, so real Word pagination, the PDF-destination page map, hyperlink navigation in Word, and visual output in Word/LibreOffice remain unverified in this environment. Page numbers in this environment came from the injected page-map fixture and the offline renderer, never claimed as real pagination;
* no Telegram login, no paid provider request and no live provider health check was executed (all provider tests use injected responses).

## Root causes found

1. **The prompt asked for compression, not preservation.** It requested a
   "2–4 sentence summary" per section with no rule about keeping numbers,
   units, dosages or English terms, so the model was *rewarded* for dropping
   them. Information loss was designed in, not a model failure.
2. **Chunking was character-based.** A fixed 22 000-character cut could land
   mid-sentence and mid-number, so a chunk boundary could destroy `7.2` or
   split a dosage — and gave the model no idea where it was in the lecture.
3. **The schema had nowhere to put detail.** Only paragraphs / bullets /
   key_points / tables / callouts existed, so definitions, worked examples,
   ordered steps and formulas had to be flattened into prose (or dropped).
4. **No QA layer.** Nothing compared the notes back to the source, so a prompt
   regression, a provider swap or a weaker model would silently degrade output
   with no signal anywhere but a user's complaint.
5. **BiDi rendering was wrong in a specific, reproducible way.** `_style_run`
   forced `run.font.rtl = True` on *every* run, so a pure-Latin token such as
   `500 mg`, `120/80` or a URL was stored as a complex-script run: Word renders
   it with the Persian face, in the wrong order, at the wrong size weight.
   A single hardcoded font was used everywhere, with no fallback advertised.

## Changes

### `gamas_bot/structuring.py`

* Prompts rebuilt as composable rule blocks: `_JSON_RULES`, `_CONTENT_RULES`
  and per-mode `MODE_RULES`, exposed through `build_system_prompt(mode)` and
  `build_presentation_system_prompt(mode)`. `full` (the default) states a
  *preservation contract* — every number, unit, dosage, English term and
  definition from the source must appear — and treats the model as a
  lecture-to-notes compiler; only `summary` is allowed to compress.
* Schema extended with `definitions` (term / term_en / definition), `examples`,
  `steps` and `formulas` on every section; `NoteSection` and `to_payload` /
  `to_markdown` / the DOCX renderer round-trip them without loss.
* `split_transcript` rewritten: paragraph → sentence → word packing with a
  terminator regex `(?:[!?؟…]+|؛|(?<!\d)\.(?!\d))[»\)\]”"']*` so decimals and
  ratios are never mistaken for a sentence end. Each chunk is prefixed with
  positional context (`_chunk_prefix(index, total)`) and oversize sentences are
  hard-cut losslessly rather than dropped.
* `merge_structured_notes` now deduplicates **only exact** repeats
  (`_dedupe_exact`, `_dedupe_glossary` keeping the longest definition, and
  `_dedupe_section_bullets` across chunks) so boundary sentences that repeat do
  not stack while paraphrases and distinct facts all survive.
* `structure_transcript(..., mode=...)` and `structure_presentation(..., mode=...)`
  resolve the mode, use the per-mode prompt and log the QA report.

### `gamas_bot/qa.py` (new)

Deterministic, log-only content-preservation QA — no embeddings, no vector DB,
no second LLM pass, no new dependency. `run_note_qa()` extracts numbers-with-
units (`mg`, `mL`, `kg/m²`, `mmHg`, `درصد`, …), percentages and BP pairs from
each source chunk and compares them to every user-visible string of the notes,
plus English technical terms and per-chunk section coverage. Notes are **never
mutated**; the worst outcome is a bounded WARNING line.

Comparison is script- and spelling-independent on purpose: Persian/Arabic digits
are normalised to Latin, and unit spellings are canonicalised through
`_UNIT_ALIASES` (`میلی‌گرم` ≡ `mg`, `میلی‌متر جیوه` ≡ `mmhg`, `درصد` ≡ `%`).
Lowercase English words only count inside a Persian passage, and URLs are
stripped first, so ordinary prose and address fragments cannot manufacture
false "missing term" warnings.

### `gamas_bot/bidi.py` (new)

`split_direction_runs()` segments a mixed string into logical-order direction
runs using the Unicode BiDi rule *plus* the domain rule that technical Latin
tokens (`500 mg`, `120/80`, `HbA1c`, URLs) are atomic. Neutrals glue to the
preceding run and adjacent same-direction runs merge, so the concatenation of
the runs is always byte-identical to the input. `is_rtl_dominant()` drives the
paragraph `w:bidi` decision.

### `gamas_bot/docx_export.py`

Rewritten around the research findings, public API preserved:

* one run **per direction segment**, not one run per paragraph;
* RTL runs get `w:rFonts/@w:cs`, `w:szCs`, `w:bCs`; LTR runs get
  `<w:rtl w:val="0"/>` and an explicit `w:ascii` face — so `500 mg` is no
  longer typeset as complex script;
* paragraphs carry `w:bidi` and tables carry `w:bidiVisual` in the
  schema-correct position (`_apply_rtl_table_direction` fixes the glossary table
  column order);
* glossary renders as a real RTL table (اصطلاح / توضیح) with a repeating header
  row, and the document gains a running page header
  (`_add_document_header`) and a generation-mode label;
* `DocumentFonts` + `resolve_fonts()` give per-role faces (body / heading /
  latin / fallback); `_inject_font_fallbacks()` rewrites `word/fontTable.xml`
  post-save to add `w:altName`, and any failure there is swallowed so a
  font-table quirk can never lose a document.

### `gamas_bot/config.py`, `gamas_bot/bot.py`

`NOTE_MODE` (`full` / `standard` / `summary`, resolved by `resolve_note_mode`,
defaulting to `full`) and `DOCX_FONT_BODY` / `_HEADING` / `_LATIN` / `_FALLBACK`
were added and wired through `Settings.docx_fonts` into both DOCX builders.

## Evaluation corpus (new)

`tests/fixtures/notes/` holds six realistic source transcripts — medical
(Persian+English, dosages, `HbA1c`, `120/80`, `eGFR`), HCI/university (Persian-
dominant with English terminology), computer science (big-O, merge sort,
hashing), fully Persian humanities (must report *zero* signals), heavy
code-switching, and a PowerPoint `slides_outline()` dump — each paired with a
hand-written reference document and the coverage floor declared in
`corpus.json`. `tests/test_note_evaluation.py` runs the whole corpus through
chunking, QA, merge and DOCX rendering and asserts those floors. It is a fully
reproducible baseline a prompt change cannot silently regress, and it needs no
network or model call.

## Tests added / reworked

| File | Tests | Covers |
|---|---|---|
| `tests/test_note_quality.py` (new) | 37 | chunking invariants, QA semantics, extended schema, merge dedup, prompt modes, BiDi segmentation, DOCX direction runs / fonts / header / glossary |
| `tests/test_note_evaluation.py` (new) | 12 | the six-fixture corpus, coverage floors, negative control, merge + render pipeline |
| `tests/test_docx_export.py` (reworked) | 16 | glossary is now a table (`w:tblHeader` count 1 → 2, `HbA1c | هموگلوبین گلیکوزیله`) |
| `tests/test_provider_robustness.py` (reworked) | 31 | chunk prefix in the chronological-order assertion |

**Validation:** `314 passed, 0 failed` — every file run individually with
`cd tests && PYTHONPATH=.:<repo> python3 -m unittest discover -s . -p "test_X.py"`
(full discovery exceeds the 180 s command cap in this environment).
`python -m compileall` and `pip check` clean. Fonts are **not** embedded in the
document and no code claims otherwise; substitution is advertised via
`w:altName` only.

## Configuration changes

New, all optional with safe defaults — an existing deployment needs no change:
`NOTE_MODE` (default `full`), `DOCX_FONT_BODY`, `DOCX_FONT_HEADING`,
`DOCX_FONT_LATIN`, `DOCX_FONT_FALLBACK` (default `Tahoma`). `.env.example`
could not be edited by the tooling in this environment, so they are documented
in `README.md` instead.

## Migration and deploy impact

None. No database schema change, no new dependency, no change to the provider
abstraction (`gemini` / `openai_compatible` / `anthropic`), no change to audio
chunking, and the raw-transcript fallback path is intact. The public Python
signatures of `build_notes_docx` / `build_plain_docx` are backwards compatible
(`fonts=` added, legacy `font=` still accepted). Restarting the bot picks up the
new behaviour; in-flight jobs are unaffected.

## Remaining limitations

- QA is a *proxy*: it proves numbers and technical terms survived, not that the
  prose is correct or well-organised. It never rewrites notes by design.
- Prompt quality still depends on the provider/model; the corpus measures the
  pipeline, so real end-to-end quality must be re-checked with the operator's
  own model and provider.
- `w:altName` is a substitution hint. Readers without the Persian face still
  see Word's own substitution; only full font *embedding* would guarantee the
  glyphs, and that is not implemented.
- Chunk prefixes add ~200 characters per chunk to the prompt budget; very short
  lectures are unaffected.
- Presentation decks are still chunked without narration-to-slide timestamps.
- Live Telegram delivery, provider billing and Windows execution remain
  unverified on the target host, as in the earlier reviews.

---

# Repository review — 2026-09-29 (Telegram rendering + Word booklet polish)

## Scope and result

A focused pass over the two user-visible deliverables — the Telegram message
renderer and the generated `.docx` booklet — after the cPanel audit below. Every
tracked module, both migrations, the tests and the docs were re-read for
consistency. **265 tests pass** (249 existing + 16 new). No live Telegram login
or paid provider call was made.

## Bugs found and fixed

| # | Area | Problem (reproduced) | Fix |
|---|---|---|---|
| 1 | Frontend (Telegram) | A heading or table header that was **itself bold** (`# **عنوان**`) was wrapped in a second `<b>`, emitting `<b><b>…</b></b>`. Telegram rejects a same-nested tag by failing the **entire message** ("can't parse entities"), so the whole booklet was lost — not just the heading. Reproduced on 5 distinct inputs. | New `strong()` / `strong_label()` helpers add a bold wrapper only when the already-rendered inline HTML contains no `<b>`, so a self-bold heading stays `<b>عنوان</b>`. Plain headers keep the previous `<b>a:</b>` output byte-for-byte. |
| 2 | Frontend (Telegram) | `progress_bar()` rounded 96–99% up to a **full** fill, then appended the emoji past the fixed width: the bar became 13 visible cells instead of 12 and visibly jumped in width mid-animation. | Clamp the fill to `width - 1` when a head emoji is used. The bar is now exactly `width` cells for every percentage, width and head combination (regression-tested across all of them). |
| 3 | Word document | `w:bidiVisual` was **appended** to `w:tblPr`, landing after `w:tblLook`. `CT_TblPr` is order-sensitive, so the generated file was schema-invalid and Word could report it as needing repair. | Insert `w:bidiVisual` before the first following element (`w:tblW`/`w:tblLook`/…), matching the schema. A test asserts the resulting order. |
| 4 | Word document | Truncating a long note title at 50 characters could leave a **trailing space or dot**, producing filenames like `جزوه - عنوان  - GMS-000001.docx` with a doubled separator and a near-empty extension. | `sanitize_filename_part()` re-strips ` ` and `.` after truncation. |

## Word booklet improvements

Beyond the fixes above, the booklet now reads properly across page breaks:

* **Repeating table headers** — `w:tblHeader` on the first row plus
  `w:cantSplit`, so a long comparison table keeps its column headings on every
  page instead of losing them after the first break.
* **Keep-with-next on headings** — every section heading, the summary heading
  and each "key points" box label carry `w:keepNext` + `w:keepLines`, so a
  heading can no longer be stranded alone at the foot of a page.

A new `WordLayoutTests` suite parses the produced `word/*.xml` parts and asserts
that `w:pPr`, `w:rPr`, `w:tblPr` and `w:trPr` children all follow the
`CT_PPrBase` / `CT_TblPrBase` / `CT_TrPrBase` sequences — the ordering class of
bug above cannot regress unnoticed.

## Not changed (deliberate)

* No table of contents or page-number field rework beyond what already exists;
  the footer PAGE field is correct and adding a TOC would require Word to
  refresh fields on open.
* Document structure, shading palette, fonts and the raw-text companion file
  are unchanged, so existing output stays comparable.

## Validation

`python -m unittest discover -s tests`: **265 passed** (0 failures, 0 skips).
`ruff check gamas_bot scripts tests passenger_wsgi.py --select E9,F`,
`compileall` and `pip check` pass. The generated document reopens cleanly in
python-docx and every `word/*.xml` part was verified against the OOXML element
sequences.

---

# Repository review — 2026-09-29 (full-stack audit + cPanel deployability)

## Scope and headline

Every tracked file was reviewed: all `gamas_bot/` modules, both SQL migrations,
`scripts/`, the tests, CI, the systemd unit, `.env.example`, README and the
Persian deployment guide. Baseline on entry: 220 tests green, `ruff` (E9,F) and
`pip check` clean.

* **There is no website and no frontend.** The project is a Telethon (MTProto)
  Telegram bot: one long-lived outbound connection, no HTTP server, no HTML/JS.
  The "frontend" is the Telegram chat UI (inline-button menus, progress
  messages, the generated `.docx`/`.txt` files). Those were audited as such.
* **cPanel is a poor natural fit** (it launches web requests, not permanent
  workers) and the previous docs declared shared hosting unsupported. It can
  work on plans that allow long-running processes, so this review added the
  missing pieces (below) and an honest checklist rather than pretending it is a
  standard web deploy.
* The **database layer is sound**: idempotent, transactional migrations with
  bookkeeping, `foreign_keys=ON`, WAL, serialised writes, crash-orphaned
  `pending/processing` rows are marked `failed` at start-up. No schema defect
  was found.

## Bugs found and fixed

| # | Area | Problem (reproduced) | Fix |
|---|---|---|---|
| 1 | Deploy / backend | `.env`, `data/bot.sqlite3`, the Telegram session and `data/tmp` were resolved from the **current working directory**. Cron, Passenger and `su -c` start in `$HOME` or `/`, silently creating a second empty database and a new Telegram login. | Relative paths and the default `.env` are anchored to the project directory (`config.PROJECT_ROOT`); `~` is expanded. |
| 2 | Deploy / backend | **No single-instance protection**, although the docs warn that two copies corrupt the session and the start-up cleanup deletes the other copy's live job folders. Any watchdog makes duplicates likely. | `instance_lock.py`: kernel `flock` on `<session>.lock`, released by the OS on any exit (tested with `kill -9`). A second process exits with code 3. |
| 3 | Backend | `SIGTERM` (what cPanel, `kill` and `pkill` send) killed the process **without** cleanup: no job status update, no Telegram disconnect. Only `SIGINT` was graceful. | `SIGTERM` now cancels the main task → same graceful shutdown as Ctrl+C. |
| 4 | Backend / output | Any XML-illegal character (NUL, `\x01`…`\x08`, VT `\x0b` from PowerPoint soft line breaks, lone surrogates) in a note, slide or transcript made python-docx raise `ValueError`, so the user **lost the Word deliverable** and got only a chat fallback. Filenames could contain control characters too. | `xml_safe()` applied at every text/run/property write; filename sanitiser strips all control characters. |
| 5 | Frontend (Telegram) | Crossed emphasis such as `**a *b** c*` rendered `<b><i></b></i>`; Telegram rejects the **entire** message ("can't parse entities"). Reproduced in 191 of 30 000 fuzzed inputs. | Improperly nested emphasis falls back to literal text; fuzz now 0/30 000. |
| 6 | Frontend (Telegram) | Admin `/ban ²` (superscript digit) passed `str.isdigit()` but crashed `int()`; absurdly long numbers overflowed SQLite. | `_parse_user_id()` (decimal only, ≤15 digits, Persian digits accepted). |
| 7 | Deploy | Hosts that block Telegram's MTProto ports had no workaround. | Optional `TELEGRAM_PROXY` (`socks5://`, `socks4://`, `http://`); `python-socks[asyncio]` added to requirements. |

## Added for cPanel

* `docs/DEPLOY_CPANEL.md` — host checklist, Python-App/venv setup, cron,
  operations and troubleshooting.
* `scripts/cpanel_preflight.py` — checks Python version, glibc (PyAV/lxml wheels
  need ≥ 2.28), packages, project-inside-`public_html`, `.env` permissions,
  writable dirs and free disk, `flock` support, outbound reachability of
  Telegram and each configured provider, and the media worker.
* `gamas_bot/launcher.py` + `scripts/ensure_running.py` — cron watchdog: cheap
  lock check, spawn throttling (45 s), detached start, default rotating log file.
* `passenger_wsgi.py` — optional "Setup Python App" entry point: JSON status
  only (`running` / `starting` / `error`), serves only `/` and `/health`, and
  doubles as a second watchdog when polled by an uptime monitor.

## Not changed / remaining risks (need the maintainer's decision or a real host)

* **Not verified on a live cPanel account.** Whether the host kills long-running
  processes, exposes Python 3.11+, or allows outbound 443 to Telegram can only be
  proven with the preflight on the target plan.
* STT and note-API calls (`aiohttp`) do **not** use `TELEGRAM_PROXY` or
  `HTTP(S)_PROXY`; those hosts must be directly reachable. `trust_env` was left
  off deliberately (it would also route a local Ollama/vLLM gateway via a proxy).
* No per-user rate limit or queue bound; pending jobs live in memory and are lost
  on restart (users must resend). Public bots need admission control.
* SQLite keeps transcripts and notes indefinitely (no retention policy).
* `ruff --select ASYNC240` flags a few blocking `Path.stat()/open()` calls inside
  async STT functions; they are short local-disk calls and were left alone.
* Prior-review limitations below (Windows unverified, remote Speechmatics jobs not
  cancelled on local timeout, no license file) still apply.

## Validation

`python -m unittest discover -s tests`: **249 passed** (220 existing + 29 new in
`tests/test_cpanel_deploy.py`). `ruff check gamas_bot scripts tests passenger_wsgi.py
--select E9,F` and `pip check` pass. Live checks: launcher started a detached
bot from a foreign working directory and created all state under the project
directory; a second bot exited with code 3; the preflight ran green on Linux
(glibc 2.36). No live Telegram login or paid provider call was made.

---

# Repository review — 2026-09-28 (accuracy-first pipeline hardening)

## Follow-up verification — 2026-09-28

A post-review pass over the current checkout found and fixed one issue in the
Speechmatics result path: after downloading and logging the complete `json-v2`
transcript, `_speechmatics()` made a second identical request and parsed that
response instead. It now parses the already-downloaded payload once, uses that
same payload for confidence metrics, and rejects malformed top-level response
shapes with a sanitized provider error. The regression test asserts exactly one
transcript download per completed job.

Validation on this checkout: `python -m unittest discover -s tests -v` — **220
passed**; `ruff check gamas_bot scripts tests --select E9,F`,
`python -m pip check`, `compileall`, and `git diff --check` pass. These counts
are current for this follow-up; the historical result figures in the sections
below describe their original review snapshots.

## Scope and result

Focused re-audit of the **transcription → note-generation** path (`stt.py`,
`structuring.py`, `config.py`, `bot.py` orchestration, `benchmark_stt.py`) with
accuracy as the primary constraint and no behaviour changes to the working
media/presentation/database pipeline. **Result: 190 tests pass** (up from
164); no paid provider calls were made.

## Root-cause findings and fixes

| Problem | Root cause | Fix | Files |
|---|---|---|---|
| Gemini `HTTP 400` was undiagnosable in production. | Google answers bad/restricted keys and bad request fields with `400 INVALID_ARGUMENT` (e.g. `details[].reason: API_KEY_INVALID`), never 401, while `_structure_chunk` read and **discarded** the error body and logged only `status=400 request_id=unknown` (Google does not send `x-request-id`). | Parse the error body defensively and log only the provider's **structured metadata** (`status/type/code`, bounded `message`, `details[].reason`) with the configured key redacted and control characters stripped; attach the same sanitized detail to the raised `StructuringError`. Raw bodies are still never logged. | `structuring.py` |
| `NOTE_API_MODEL=models/gemini-…` produced `…/models/models%2F…:generateContent` → guaranteed `400`. | Model name was URL-quoted verbatim into the path. | Strip a leading `models/` prefix before quoting. | `structuring.py` |
| Gemini safety-blocked responses surfaced as "empty/invalid response". | `candidates[0]` indexing raised `IndexError` before `promptFeedback.blockReason` was read — a real possibility for medical lecture content. | Missing candidates now raise with the sanitized `blockReason`; the raw-material fallback still applies. | `structuring.py` |
| Speechmatics ran the **throughput** tier (`"model": "standard"`) for an accuracy-first Persian bot. | Hardcoded tier; Speechmatics documents `enhanced` as its highest-accuracy model and supports a native custom dictionary. | `SPEECHMATICS_OPERATING_POINT` (default `enhanced`, legacy alias `SPEECHMATICS_MODEL`) and `SPEECHMATICS_ADDITIONAL_VOCAB` (native `additional_vocab`; no LLM layer). A contract without the enhanced tier rejects submission and the existing fallback keeps jobs recoverable. | `stt.py`, `config.py` |
| The serialized tier field drifted from the provider contract. | `operating_point` is now the **deprecated** spelling in the Speechmatics Batch API (`model` is the documented field; possible values `standard`, `enhanced`, `melia-1`), and the custom dictionary is recommended at 1000 entries per job (hard rejection above 20000) rather than "20k is fine". | The serializer sends `model` (with `SPEECHMATICS_MODEL_FIELD=operating_point\|both` as an opt-in for older self-hosted containers), `melia-1`/`oak-1` are accepted with their `multi` language requirement and no custom dictionary, and `additional_vocab` is capped at `SPEECHMATICS_VOCAB_MAX_ITEMS` (default 1000) in the configured priority order. | `stt.py`, `config.py` |
| STT catalogue not extensible (two hardcoded names, ad-hoc 1 GB special case). | Provider list, key checks, size routing inline in `transcribe()`/`_provider_key()`/validation/benchmark. | Small frozen `STTProvider` registry `(availability, attempt, max_upload)`; new optional `openai_compatible` STT engine (OpenAI/Groq `whisper`, local vLLM/Ollama gateways via `POST /audio/transcriptions`); per-engine size caps generalize the 1 GB rule. Default two-provider behaviour, fallback chain and guard rails are unchanged. | `stt.py`, `config.py`, `scripts/benchmark_stt.py` |

## Deliberately not changed (accuracy constraints)

- **No audio chunking for STT.** Files are attempted whole per engine and, above
  a provider's direct-upload cap, routed to another configured engine.
  Chunking would lose word context at every boundary; Speechmatics/Deepgram
  batch APIs accept multi-hour files natively. This is now enforced in one
  place (`STTProvider.max_upload`) instead of an inline 1 GB branch.
- Note-generation chunking stays **sequential** at sentence boundaries with
  chronological `بخش N` joining; long decks keep the complete outline
  behaviours from the previous review.
- Raw-transcript safety net in `bot.py` (structuring failure ⇒ full raw
  transcript/outline delivered and stored) is untouched and re-verified by a
  new bot-level regression test covering a Gemini `HTTP 400`.
- No embeddings/RAG/normalization layer; prompts forbid inventing facts.

## Tests added (`tests/test_provider_robustness.py`, 26 tests)

Gemini/`400` handling (sanitized `API_KEY_INVALID` visible, key redaction,
HTML-body suppression, non-retry), OpenAI-compatible note errors + `429`
retry, `models/` normalization, safety `blockReason`, STT provider
switching/fallback chain incl. `openai_compatible`, sparse-file size routing
(1 GB skip, >all-caps fail-fast), OpenAI STT request shape/privacy, Speechmatics
enhanced + vocab config, config validation, chunk ordering/completeness on
mixed Persian-English medical text (numbers, `Metformin`, `HbA1c`),
benchmark three-engine coverage, and raw-transcript delivery/storage after a
note-generation `HTTP 400`.

## Validation

`python -m unittest discover -s tests`: **190 passed** (0 failures, 0 skips).
`ruff check gamas_bot scripts tests --select E9,F`: passed.
`python -m pip check`, `compileall`: passed.

---

# Repository review — 2026-09-28: system media/office dependencies removed

## Scope and result

Full re-audit of every tracked file (application modules, SQL migrations,
tests, benchmark script, `.env.example`, systemd unit, GitHub workflow, README
and Persian deployment guide), followed by replacement of the external
**FFmpeg / FFprobe / LibreOffice** runtime dependencies with Python packages.

Goal: the bot must run on hosts that have no `ffmpeg`, `ffprobe` or `soffice`
binary installed (cPanel, locked-down containers, minimal VPS images), while
preserving formats, security limits, timeouts, cleanup and error handling.

**Result: 164 tests pass** (up from 141) with no system media binaries present
on the machine, `pip check` and `ruff check gamas_bot scripts tests --select E9,F`
are clean. No live Telegram or paid provider requests were made.

## What replaced the binaries

| Old dependency | Replacement | Why this option |
|---|---|---|
| `ffprobe` stream probing | **PyAV** (`av` wheel) inside `gamas_bot/media_worker.py` | Verified API: `av.open(..., options={"protocol_whitelist": "file,pipe"})`, `container.duration`/`stream.duration` reporting, `Codec.canonical_name` (equals ffprobe's `codec_name`, e.g. `mp3` not `mp3float`). |
| `ffmpeg` audio extraction/transcode | **PyAV** decode → `AudioResampler` (s16/mono, 16 kHz or 48 kHz) → `pcm_s16le` WAV or `libopus` 32 kbit/s Opus in Ogg | Verified encoders ship in the wheel (`pcm_s16le`, `libopus`, `libmp3lame`, `libx264`, …); `codec_context.options = {"application": "voip"}` reaches `avcodec_open2` exactly like the old `-application voip` flag. |
| `ffmpeg` multi-clip merging | **PyAV** per-input resamplers + zero-sample padding between clips | Mirrors the old `aresample`/`apad=pad_dur`/`concat` graph: mono, target rate, 0.5 s (`PPTX_SILENCE_SECONDS`) between clips, none after the last. |
| `soffice` legacy `.ppt → .pptx` | **ppt2pptx** (pure Python, MS-PPT/CFB parser) | Mandated equivalent; converts `.ppt`/`.pps`/`.pot` (extension-agnostic), never executes macros, respects input-size limits, reports lossy features as structured warnings. |
| ODP/OTP via LibreOffice | **Explicit rejection** with a user-facing message asking for `.pptx` | No verified pure-Python ODP→PPTX converter with acceptable fidelity exists; a clean unsupported-format error is better than a fake conversion. |

`pymediainfo` was evaluated and rejected: although recent wheels bundle the
native library, PyAV alone covers probing *and* transcoding with one
dependency, and its FFmpeg libraries are the same code family the previous
commands used, which keeps format coverage closest to the old behaviour.
`pydub`/`moviepy` were not used because they still shell out to FFmpeg.

## Architecture: a supervised Python worker

All media operations run in a dedicated child process,
`python -m gamas_bot.media_worker`, started through the existing
`media.run_command` wrapper:

- **No media-runtime path invokes `ffmpeg`, `ffprobe` or `soffice`** — every
  media command vector starts with `sys.executable -m gamas_bot.media_worker`
  (regression-tested in `test_media_runtime.py`, `test_audit.py`). The current
  DOCX renderer separately uses `soffice` for exact static-TOC pagination when
  an eligible long document has the TOC enabled.
- Timeouts (`MEDIA_TIMEOUT_SECONDS`, `PPT_CONVERT_TIMEOUT_SECONDS`),
  cancellation, POSIX process-group kill, pipe draining, non-shell execution
  and bounded stderr reporting are unchanged.
- A native crash or hang inside the media libraries takes down only the
  worker, not the bot — the isolation property the old subprocess tools gave.
- Each input is opened with the `file,pipe` protocol whitelist, so a disguised
  playlist still cannot fetch network URLs
  (`test_network_playlist_is_rejected_without_fetching_url` asserts the
  refusal message).
- FFmpeg-level error records are routed to the worker's stderr head so the
  first 300 characters the parent reports contain the real cause (e.g.
  `Protocol 'http' not on whitelist 'file,pipe'!`).

## Configuration changes

- Removed: `FFMPEG_BIN`, `FFPROBE_BIN`, `SOFFICE_BIN` (legacy `*_BIN` lines
  left in an existing `.env` are silently ignored).
- Added: `MEDIA_TIMEOUT_SECONDS` (default 3600) and
  `PPT_CONVERT_TIMEOUT_SECONDS` (default 600).
- Backward compatible: the old `FFMPEG_TIMEOUT_SECONDS` and
  `SOFFICE_TIMEOUT_SECONDS` are still read as *fallbacks* for the two new
  variables, so an existing `.env` keeps working unchanged (verified by tests
  in this review). They are deprecated, not removed.
- Startup now runs `media_worker check` (reports installed `av`/`ppt2pptx`
  versions) instead of probing binaries on `PATH`.

## Format support and fidelity notes

- Audio/video coverage follows the FFmpeg libraries bundled in the `av` wheel:
  MP3, M4A/AAC, WAV, OGG/Opus/Vorbis, FLAC, WMA, AMR, MP4/MKV/MOV/AVI/WebM,
  etc. `PASSTHROUGH_CODECS` still matches ffprobe-era canonical names, so
  normal uploads are still not re-encoded.
- Output behaviour is unchanged: mono 16 kHz PCM WAV, or 48 kHz Opus
  (32 kbit/s, voip) when the WAV-size estimate would exceed
  `PPTX_WAV_LIMIT_BYTES`/`MAX_FILE_SIZE_BYTES`.
- Legacy PPT conversion keeps slide text, speaker notes, slide order and
  macros-not-executed behaviour, with ZIP/packaging safety enforced by
  `ppt2pptx` limits plus the bot's own `MAX_FILE_SIZE_BYTES` check.
  ppt2pptx documents lossy legacy features (embedded media playback,
  animations, some OLE objects) as structured warnings; they are logged, not
  silently dropped. Embedded narration media inside old `.ppt` files is no
  longer carried into the converted `.pptx` (LibreOffice used to copy it);
  this is an accepted, documented limitation of the mandated `ppt2pptx`
  approach, covered by `test_embedded_media_deck_converts_with_diagnostics`.
- ODP/OTP are classified as `unsupported` and rejected at submission time
  with an explanatory message; `PPTX_LEGACY_ENABLED=false` still disables
  PPT/PPS/POT conversion entirely.

## Tests added or reworked

- Real-media smoke tests no longer skip: probing (WAV/MP3/MP4 with and
  without audio), extraction to WAV/Opus, merging with resample + silence
  math, corrupt-input errors, bounded error messages, worker timeout kill,
  protocol-whitelist refusal, and the worker dependency self-check all run
  on every CI job without installing anything.
- `PPT → PPTX` coverage uses committed real PowerPoint 97–2003 fixtures
  (`tests/fixtures/*.ppt`, MIT, from the ppt2pptx corpus) for `.ppt`, `.pps`
  and `.pot`, plus garbage-input failure and explicit ODP rejection.
- Native PPTX variants, ZIP safety limits, clip limits, duration caps,
  cancellation/cleanup and security restrictions keep their previous tests,
  now backed by real generated media instead of stub binaries.
- A regression test runs a full video job with `PATH` pointing at a
  non-existent directory, proving no system binaries are needed.
- CI no longer installs `ffmpeg` via apt.

## Validation performed

Environment: Linux, Python **3.11.2**, no `ffmpeg`/`ffprobe`/`soffice`
binaries on the host, `av` **18.1.0**, `ppt2pptx` **0.4.2**.

- `python -m unittest discover -s tests`: **164 passed**, 0 skipped.
- `python -m pip check`: no broken requirements.
- `ruff check gamas_bot scripts tests --select E9,F`: passed.
- `python -m compileall -q gamas_bot scripts tests`: passed.
- Worker CLI exercised directly (`check`, `probe`, `extract`, `merge`,
  `convert`) including the HTTP-playlist whitelist refusal.

## Remaining limitations

- The media worker is still native code parsing untrusted bytes; it is
  process-isolated and protocol-restricted but not a sandbox. Keep `av`
  updated and apply host resource limits.
- ppt2pptx fidelity on exotic legacy decks is bounded by its documented
  feature set (see its README); warnings are logged for omitted content.
- Windows/macOS application execution is documented but validated only on
  Linux CI, as before.

---

# Repository review — 2026-09-27

## Scope and result

Reviewed the tracked application modules, SQL migrations, operational script,
tests, environment example, deployment unit, GitHub workflow, README and Persian
deployment guide. Changes focus on reproducible correctness, privacy and
operational issues; this is not a certification that the project is bug-free.

The original **104 tests passed**. New regression tests reproduced failures that
were not covered by that suite. After fixes, **141 tests pass**, including three
real-FFmpeg smoke tests. No live Telegram or paid provider requests were made.

## Fixes

| Area | Problem | Change |
|---|---|---|
| First startup | Telethon opened its SQLite session before the parent directory existed. | Create the session directory before constructing the client. Apply the restrictive POSIX umask before file logging as well. |
| Telegram delivery | Oversized HTML lines were split after escaping, breaking entities and changing visible text. Literal internal code placeholders could crash rendering. | Split decoded text, then escape each page; strip NUL placeholders from input. Validate nonpositive split limits. |
| Broadcasts | A flood wait on a later page resent already-delivered pages. | Retry only the failed page. |
| Admin interaction | A pending private broadcast could consume an administrator’s unrelated group message or unknown command. | Limit admin commands/callbacks to private chats; group messages do not answer prompts. Menu navigation and cancellation clear pending actions. |
| SQLite writes | An invalid clip row could leave earlier updates pending, then an unrelated write committed them. | Serialize writes in explicit transactions and roll back on errors/cancellation. |
| Migrations | DDL could remain partially applied; failed database startup leaked its connection. Comment removal could join SQL tokens. | Transactional migrations, close-on-failure, idempotent open, whitespace-preserving comment removal and trigger-aware statement boundaries. Existing duplicate-column recovery remains supported. |
| Long presentations | Outline truncation silently lost later slides, including in raw fallback. | Preserve the full extracted outline and chunk it for note generation. Short outlines are still repeated with narration chunks. |
| Presentation variants | python-pptx’s convenience loader rejects slideshow/template content types despite advertised support. | Read their presentation parts directly, with mappings for macro-enabled slideshow/template variants. Native parsing does not execute macros. |
| Presentation metadata | Disabling slide text made the recorded slide count zero. Unnamed legacy files were saved with a native extension. | Count actual slide parts independently of text extraction; choose the appropriate legacy suffix. Add missing template MIME types. |
| Extraction cleanup | Cancelling a thread-backed extraction let it keep writing after its work directory was removed. | Wait for extraction to finish before propagating cancellation and cleaning files. |
| Media commands | Timeout cleanup could leave launcher children running or hang on output pipes. Concatenation did not reset stream timestamps. | POSIX process-group termination, pipe draining, and per-input timestamp reset. Windows still only explicitly kills the direct process. |
| Media safety | A disguised playlist could ask FFmpeg/FFprobe to fetch network resources. | Whitelist local `file` and `pipe` protocols for every media input. This is not a full filesystem sandbox. |
| Media estimates | Inserted silence was absent from WAV-size estimates; one unknown clip duration disabled checks on known durations. | Include silence in size estimates and enforce the cap against the sum of known durations. Report narration duration separately. |
| Configuration/probing | Nonfinite time values and malformed probe JSON caused invalid processing or unexpected exceptions. | Reject nonfinite environment times; validate probe structure and ignore nonfinite durations. Ignore invalid confidence ranges. |
| STT timeout | Socket timeouts/poll checks did not bound the complete upload/request lifecycle. | Bound every provider attempt with the configured job timeout; fallback gets its own attempt budget. |
| Provider privacy | STT response bodies and client exceptions could leak lecture text/URLs to logs; Gemini keys were in query parameters. | Keep sanitized STT status/type errors, omit provider error bodies, suppress sensitive client exception chains and authenticate Gemini with a header. |
| Incomplete notes | Explicit output-token truncation was accepted as a finished booklet. | Detect token-limit finish reasons for supported providers and use the existing raw-material fallback. |
| Benchmark | Large files routed to Deepgram could be labeled as Speechmatics measurements. Nested output directories failed. | Reject unsupported Speechmatics samples in that row, verify the actual engine and create output parent directories. |
| Deployment | Account creation populated the intended checkout directory before `git clone`. The unit’s writable-directory comment overstated `ProtectSystem=full`. | Document `--no-create-home`; use `ProtectSystem=strict` with `data/` as the writable exception. |
| Repository hygiene | SQLite WAL/journal sidecars outside `data/` were not ignored. | Ignore database/session sidecars and the linter cache. |
| GitHub Actions | Unrelated RDP workflow contained a fixed administrator password, disabled NLA and had no bot tests. | Remove it and add read-only-permission Linux test CI for Python 3.11–3.13. |

No new SQL migration or runtime dependency was needed. Existing database schemas
remain compatible. Requirements were installed and audited rather than changed
without evidence of a dependency problem.

## Validation performed

Environment: Linux, Python **3.11.2** (historical run, before the 2026-09-28
dependency removal above).

- `python -m unittest discover -s tests -v`: **141 passed** at that time, no
  skips when the optional smoke-test FFmpeg binary was provided.
- Real FFmpeg checks (now superseded by the always-on PyAV worker tests):
  concatenate/resample clips and inserted silence; WAV/Opus conversion;
  reject a network playlist without fetching its URL.
- Real subprocess checks: timeout with output pipes, cancellation, and POSIX
  launcher-child cleanup (retained, still passing).
- Synthetic package/SQLite tests: presentation variants, complete long outlines,
  rollback after invalid data, migration rollback/replay and parser boundaries.
- Mock provider/Telegram tests: timeout fallback, private admin isolation,
  broadcast retries, truncated responses and error sanitization.
- `ruff check gamas_bot scripts tests --select E9,F`: passed.
- `python -m compileall -q gamas_bot scripts tests`: passed.
- `python -m pip check`: no broken requirements.
- `pip-audit -r requirements.txt`: no known vulnerabilities in resolved
  requirements at review time. This is not a guarantee about unknown issues.
- `git diff --check`: passed.

At that time FFmpeg was supplied locally by an optional `imageio-ffmpeg`
development install because OS package installation was unavailable in this
sandbox; it was not an application dependency. Since the 2026-09-28 review
above, **no FFmpeg/FFprobe binary is used or expected anywhere** — neither for
the application nor for the tests — and CI installs none.

## Deployment actions

1. Back up the database, session and environment with the service stopped.
2. Install updates and run the tests in your virtual environment.
3. If using the supplied systemd unit, reinstall it and run
   `systemctl daemon-reload`. Keep database, session, temp and file-log paths
   under `data/`, or add explicit writable paths to your customized unit.
4. Restart one worker and test a short audio, a video, a slides-only presentation
   and a real legacy deck against your configured accounts.
5. Verify model availability, Persian support, API quotas and upload limits in
   your provider account. Set low concurrency initially.
6. If the removed RDP workflow was ever used, stop remaining runner sessions and
   review/revoke their Tailscale access. Rotate the published password anywhere
   it was reused. Removing the workflow does not erase Git history or rotate
   external credentials.

## Remaining limitations and unverified behavior (2026-09-27 state)

- Live Telegram authentication/upload/delivery, provider API compatibility and
  billing, Windows execution, and actual systemd startup require validation on
  the target host. FFprobe was then exercised through stubs/parser tests; the
  2026-09-28 review replaced it entirely with real PyAV-backed probe tests.
- `MAX_CONCURRENT_JOBS` limits active work, not queue length or per-user usage.
  Pending tasks live in memory. Public deployments need admission/rate limits
  and resource controls; interrupted jobs require resubmission.
- Large presentation parsing still loads substantial package data. Unknown media
  durations cannot be completely bounded in advance, and output sizes are
  checked after conversion. Set host disk, memory and CPU limits.
- Protocol restrictions and the service unit are not a full parser sandbox.
  Untrusted documents may still access local files readable by the service
  account; isolate workers and keep native libraries patched. The unit needs
  network access for Telegram/STT, so it does not isolate the media worker
  from the network (the worker itself only whitelists local protocols).
- Long decks are chunked without precise narration-to-slide timestamps. Retaining
  all input does not guarantee an LLM preserves every fact or cross-chunk context.
- A local STT timeout does not cancel/delete a remote Speechmatics job. Review
  provider retention policies and possible costs for abandoned requests.
- SQLite retains user information, transcripts and notes. There is no automatic
  retention/deletion policy. Backups and logs also need access controls.
- No project license has been supplied; the maintainer must choose one.

## Repository coverage

- `gamas_bot/`: configuration, entry point, handlers, progress, logging,
  persistence, media/presentation pipeline, speech-to-text and note generation.
- `migrations/`: both existing schemas reviewed and replay/rollback tested;
  migration files did not need modification.
- `scripts/`: benchmark routing/reporting fixed; text normalization reviewed.
- `tests/`: existing suite retained; `test_audit.py` and
  `test_media_runtime.py` add regression and native-tool checks.
- `deploy/`, `.github/`, `.env.example`, `.gitignore`, `requirements.txt`:
  operational/security review and changes described above.
- `README.md`, `docs/DEPLOY_FA.md`: updated setup, timeout semantics, private
  administration, chunking/fallback behavior, testing and security limitations.

---

# Follow-up — 2026-10-03: global coherence and the booklet document

Second audit round, same repository and the same question ("did anything the
lecturer said disappear, and is the delivered file a *booklet*?"), but with the
lens on the two weaknesses the first round left open:

* every chunk was still written as an independent mini-lecture, so a long
  lecture produced duplicated introductions, drifting terminology and repeated
  "key points" with no global outline and no editorial pass;
* and the Word file, while correct and RTL, was a text dump: no cover, no
  heading hierarchy, a fake "page border" drawn as a bordered empty paragraph,
  no automatic table of contents, and no way to *look* at the result offline.

## What changed

* `gamas_bot/editorial.py` (new, provider-free): `LectureContext`, outline
  document/parsing, the per-chunk context block (position + neighbours + rules
  that keep a chunk from behaving like a whole lecture), the compact note
  payload, the compile document and the deterministic acceptance gate.
* `gamas_bot/structuring.py`: `structure_transcript` / `structure_presentation`
  now run orientation (multi-part only, failure non-fatal) → per-chunk prompt
  with global context → additive merge → optional source-grounded repair → one
  controlled editorial compilation, accepted only when QA does not regress.
  `merge_structured_notes` was rewritten: adjacent same-topic sections join,
  verbatim paragraphs/bullets collapse document-wide, a section that only
  restates an earlier one verbatim is dropped, a second differing table becomes
  labelled bullets, summaries are unioned (never replaced).
* `gamas_bot/qa.py`: `NoteStructureReport` + `analyze_structure()` — repeated
  headings, duplicate paragraphs/bullets/key points, empty/very short sections,
  bullet-only sections, summary sentences repeated from the body. Observational
  only: it never deletes content and never triggers a repair.
* `gamas_bot/docx_export.py`: cover page (God line, Gamas mark with typographic
  fallback, verbatim quotation) in its own section; real section-level
  `w:pgBorders`; live `PAGE` field footer; running header; real Heading 1/2/3 +
  Title/Subtitle/Quote/Definition/Example/Note/Warning/Table-text styles;
  automatic TOC for long documents; white-on-accent table headers; cover-less
  documents open with a title block and no longer start with a blank page.
* `gamas_bot/config.py` + `.env.example`: `NOTE_GLOBAL_CONTEXT_ENABLED` and the
  `DOCX_*` design keys (cover, TOC, border style/colour/width/space, footer
  brand, optional local logo).
* `scripts/render_docx_pages.py` (new): offline page renderer (Pillow) used for
  visual validation, with blank-page / overflow / orphan-heading detection.
  `scripts/validate_docx.py --render` now falls back to it instead of skipping
  visual validation, and its order check no longer misreads nested `w:sectPr`.
* `scripts/benchmark_notes.py`: reports document facts (cover, heading styles,
  page borders, TOC, PAGE field), a merge-duplication probe and a
  realistic long-document probe.

## Measured before/after (deterministic, no provider call)

| measure | before | after |
| --- | --- | --- |
| tests | 395 pass | **441 pass** |
| full-mode system prompt | 3 414 chars, no style/transition rules | 5 040 chars (+ rules, still no compression ask) |
| fixture signal coverage / semantic coverage | 1.0 / 1.0 | 1.0 / 1.0 (no regression) |
| `merge(reference ×2)` — sections / duplicate blocks | 8 / 7 duplicates (50 %) | 4 / **0 duplicates** |
| 6-fixture DOCX: cover / page border / PAGE field | 0 / 0 / 6 | **6 / 6 / 6** |
| long booklet (30 sections): heading styles, TOC | none, none | Heading 1–3 (68 headings), automatic TOC |
| duplicate rate over the whole reference corpus | not measured (no diagnostics) | 0.0000 (128 blocks) |

## Known limits

The measures above are deterministic and offline. The two *model* passes
(orientation and compilation) can only be scored with `--live` and a configured
provider, so their gain is asserted through the acceptance gates, not through a
token-level comparison; a compilation that loses measured content is rejected,
which bounds the downside but cannot prove a readability improvement without a
human reviewer (`--human-out`).

---

## Audit — 2026-10-03 (repo-wide consistency review)

Scope: every file in the repository read end to end (21 `gamas_bot/` modules,
21 test modules, 6 scripts, 4 docs, migrations, deploy unit, workflows), then
each discrepancy cross-checked against a *measured* run rather than a reading
of the source. Baseline before any change: 488 tests pass, `ruff check
--select E9,F` clean, `pip check` clean, `scripts/validate_docx.py` PASSED.

### Bugs fixed

| # | file | defect | fix |
| --- | --- | --- | --- |
| B1 | `gamas_bot/qa.py` (`run_note_qa`) | `uncovered_chunks` / `chunk_coverage` contradicted their own contract. The rule was "every number *and* every term missing", but a chunk with **no numbers** satisfies "every number missing" vacuously, so a term-only chunk whose terms were all dropped reported `chunk_coverage == 1.0` while `missing_terms` listed the loss. | A signal the chunk does not carry is vacuously "all absent"; a chunk is uncovered when each signal it *does* carry is missing. |
| B2 | `gamas_bot/units.py` (`_CUES_WEAK`) | The weak comparison cue was `"اما "` with a trailing space. `_contains` enforces a word boundary *after* the cue, and the character after the space is a letter, so the cue could never match — the branch was dead (measured: `False` for every sample sentence). | Cue is now `"اما"` (plus `"ولی"`), with a comment recording why the padding must not come back. |
| B3 | `gamas_bot/docx_export.py` (`resolve_fonts`) | The docstring promised the prefixed keys (`body_font`, `latin_font`, …) but only the plain ones were read: `resolve_fonts({"body_font": "X Serif"})` measured `body='Tahoma'`. | `_font_role_value` accepts either spelling; a blank alias no longer shadows a real value. |
| B4 | `gamas_bot/docx_export.py` (`_enable_bidi`) | `bidi.is_rtl_dominant` exists, is unit-tested, and both module docstrings say it decides the paragraph direction — but every paragraph was forced to `w:bidi`, including formula and URL lines. The helper was dead in production. | The decision now lives in `_add_directional_text` (the single funnel every run passes through): a paragraph with any strong RTL character becomes RTL, a Latin-only line keeps Word's LTR default. A paragraph created empty and filled later (the cover title) still turns RTL. |
| B5 | `gamas_bot/docx_export.py` (`ALLOWED_BORDER_STYLES`) | `thick` — a valid `ST_Border` line style — was missing from the allow-list, so a configured `thick` frame silently became `single`. | `thick` added; the constant now documents that the whole ECMA-376 line-style set is accepted. |
| B6 | `gamas_bot/presentations.py` (`prepare_audio`) | The total-duration bound was checked against `sum(durations)` (only clips that were measured) instead of `total_duration` (the complete sum when it is known). | Checked against the best estimate available. |
| B7 | `gamas_bot/structuring.py` | `TOPIC_BREAK_MIN_FILL` was defined *after* `split_transcript`, which uses it. | Moved above its first use. |
| B8 | `gamas_bot/structuring.py` | Prompt typo `درصدمقدار` ("percentamount") in two preservation rules. | `درصد،` |
| B9 | `gamas_bot/bot.py` | `removed += not leftover.exists()` added booleans to an `int`. | Explicit `if not leftover.exists(): removed += 1`. |
| B10 | `requirements.txt` | `Pillow` is imported directly by `scripts/render_docx_pages.py` but was only an incidental dependency of `python-pptx`. | Declared explicitly. |

### Documentation corrected (no behaviour change)

* **`DOCX_TOC_LEVELS`**: README advertised `1-1 … 1-9`; the validator accepts
  only `1`, `1-1`, `1-2`, `1-3`. README now lists the real set.
* **TOC rule**: README said "three or more sections". The real rule is two
  independent signals — `DOCX_TOC_MIN_SECTIONS` (default 4) **or** 2400+
  characters of body text (`docx_export.TOC_MIN_BODY_CHARS`).
* **TOC field**: README quoted `TOC \o "1-2"`; the default is `1-1`.
* **`DOCX_PAGE_BORDER_STYLE`**: `thinSingle` was listed but is not a valid
  `ST_Border` value; replaced with `thick` and the real constraint.
* **Font profiles**: `persian_modern` uses **Aptos** for Latin (not Vazirmatn),
  and the undocumented `persian_modern_alt` (Vazirmatn + Calibri) is now
  listed. The example no longer sets `DOCX_FONT_LATIN=Vazirmatn`.
* **`GMS-XXXXXX`**: the README never said it *is* the submission ID
  zero-padded to six digits, and the logging section implied they were two
  different identifiers. Both now state the equivalence (matching
  `docs/DEPLOY_FA.md`).
* **Project structure**: 5 modules and 3 scripts were missing from the tree
  (`bidi`, `textnorm`, `qa`, `units`, `editorial`, `benchmark_notes`,
  `render_docx_pages`, `validate_docx`), as was `docs/QUALITY_REPORT.md`.
* **Lint command**: the local `ruff` line omitted `passenger_wsgi.py`, which CI
  does check.
* **Video formats**: `WEBM` was missing from the supported-formats table.
* **Undocumented settings** now in the README: `TELEGRAM_PROXY`,
  `STT_POLL_INTERVAL_SECONDS`, and the `GEMINI_MODEL` / `GEMINI_API_KEY`
  legacy fallback.
* **`config.py`**: the `DOCX_TOC_LEVELS` comment claimed an invalid value falls
  back; the code raises. Comment corrected (and the README's "none of them can
  make generation fail" claim qualified).
* **`docs/AUDIT.md` (2026-09-2x entry)**: listed `SOFFICE_TIMEOUT_SECONDS`
  under "Removed" and, three lines later, under "Backward compatible". The
  code honours it as a *fallback* for `PPT_CONVERT_TIMEOUT_SECONDS`; entry
  corrected.

### Candidates examined and rejected

* `STT_OPENAI_MAX_UPLOAD_BYTES` / `NOTE_API_TIMEOUT_SECONDS` naming — every
  reference (`.env.example`, README, `config.py`, callers) agrees.
* `_process_submission`'s `notice` — bound on both the success and the failure
  path; never unbound.
* `build_plain_docx`'s heading mapping `#`/`##` → Heading 1 looks like an
  off-by-one, but `##` *is* the top level the pipeline emits, so promoting it
  to Heading 1 is what keeps the fallback booklet's TOC non-empty. Only the
  misleading comment was corrected.
* `jalali_date` — spot-checked against leap years (1403, 1404) and the
  1378/1404 decade boundaries; correct.
* The QA "coverage vs. terms" gap and the `اما ` probe were both confirmed as
  *independent* gates before B1/B2, and re-confirmed after.

### After

| check | result |
| --- | --- |
| `python -m unittest discover -s tests` | **493 pass** (488 + 5 new regression tests) |
| `ruff check gamas_bot scripts tests passenger_wsgi.py --select E9,F` | clean |
| `pip check` | clean |
| `gamas_bot.media_worker check` | `{"av": "18.1.0", "ppt2pptx": "0.4.2"}` |
| `scripts/validate_docx.py --out … --render` | PASSED, 3 pages rendered, no layout warnings |
| `scripts/benchmark_notes.py` | signal 1.0, semantic 1.0, ratio 1.007 — unchanged |

New regression tests: chunk coverage judged on the signals a chunk carries
(`test_fidelity_upgrade.py`), reachable weak cues (`test_semantic_coverage.py`),
prefixed font keys (`test_note_quality.py`), and paragraph base direction for
Latin-only vs. Persian paragraphs and for a paragraph filled after creation
(`test_mixed_script_typography.py`).

---

## Quality report (2026-09) (`QUALITY_REPORT.md`, removed)

Second audit round on `AmiraliGhamkhar/Gamas_bot`. Baseline commit `39cfdc5`,
branch `arena/01a10267-gamas-bot`. Everything below was measured on the local
repository (deterministic, provider-free) plus visual inspection of a rendered
booklet; no model provider credentials were available in this environment, so
the two *model* passes are bounded by their deterministic acceptance gates
rather than scored live.

> **Historical report.** The findings and measurements below describe the earlier
> note-quality/DOCX revision on branch `arena/01a10267-gamas-bot`; they are not
> a statement of the current production state. Since that audit, the dynamic Word
> TOC was replaced by a static ordinary-text TOC with PDF-destination mapping,
> unique bookmarks and fail-closed pagination; billing, private receipt review,
> provider-key encryption and their migrations/tests were also added. The current
> operational requirements are in [`README.md`](../README.md) and
> [`DEPLOY_CPANEL.md`](DEPLOY_CPANEL.md). LibreOffice is not installed in the
> present validation environment, so real Word/LibreOffice page mapping and
> visual DOCX validation remain unverified; mocked page-map tests do not prove
> real pagination.

---

## A. Root causes found in the audit

1. **A part did not know what it was about.** `build_context_block` told a chunk
   its position and the lecture's topic list, but never which topic *that part*
   owns. Every chunk therefore introduced itself as a summary of the whole
   lecture, and the merge had to repair that afterwards.
2. **A part did not know how it sat in the lecture.** Nothing distinguished a
   part that opens mid-topic (so it must link, not introduce) from one that
   starts a topic; nothing distinguished a part that ends mid-explanation from
   one that finishes a subject. The prompt's advice was therefore generic.
3. **One oversized final pass was skipped.** The single global editorial pass —
   the only step that produces a coherent document out of per-part drafts — was
   *skipped* whenever the merged notes exceeded `COMPILE_MAX_CHARS`. The longest
   lecture, exactly the one that needs it, was delivered as the raw merge.
4. **The repair pass could silently rewrite good parts.** Acceptance was decided
   on the merged document's averages, so a repair that fixed one part and
   damaged another was accepted; and `_repair_is_better` compared the *values*
   of the missing-number tuple element-wise, rejecting correct repairs whose new
   missing values sorted higher.
5. **The orientation digest silently dropped the tail of a long lecture.** A
   fixed per-part slice plus a whole-document truncation meant the last parts —
   whose topics the digest exists to provide — were cut off, and the positional
   topic map then mismatched the parts.
6. **Quality diagnostics were blind to whole-explanation loss and to structure.**
   Number/term coverage caught dosage corruption but not a deleted explanation;
   there was no measure of untitled sections, abrupt section openings, split
   topics or fragment-only prose.
7. **The DOCX's navigation layer was either missing or unhelpful.** A TOC was
   written for any document from three sections up — including short notes where
   it costs a page — and with `\o "1-2"` it was dominated by block labels
   («تعریف‌ها»، «مثال‌ها») that repeat in every section. Long documents that had
   no headings at all could still earn an empty TOC page. The cover's metadata
   was a single long `•`-joined line that wrapped and stranded the tracking
   reference.

## B. Files changed

| file | change |
| --- | --- |
| `gamas_bot/editorial.py` | per-part topic ownership, continuity flags, budget-aware outline digest, segmented compile document, section-boundary slicing |
| `gamas_bot/structuring.py` | multi-slice final compilation, per-part repair acceptance, missing-number comparison fix, context block without an outline |
| `gamas_bot/qa.py` | untitled-section and abrupt-opening diagnostics (content-free words, relation markers) |
| `gamas_bot/docx_export.py` | TOC policy (levels + two-signal threshold), cover metadata lines, adaptive cover spacing, body-section helper, logo passthrough |
| `gamas_bot/config.py` | `DOCX_TOC_LEVELS`, `DOCX_TOC_MIN_SECTIONS` |
| `.env.example`, `README.md` | document the two new settings |
| `scripts/benchmark_notes.py` | new structure fields, part-context probe, richer console summary |
| `tests/test_global_compilation.py` | part-context, outline-digest, segmented-compilation and targeted-repair tests |
| `tests/test_note_quality.py` | coherency-diagnostic tests |
| `tests/test_docx_polish.py` | TOC levels/threshold, cover metadata/spacing, raw-text TOC tests |
| `tests/test_fidelity_upgrade.py` | every documented `DOCX_*` switch reaches the design object |

## C. Architectural changes (smallest change that fixes the root cause)

**Per-part topic ownership.** `LectureContext.topic_for(index, total)` maps the
outline's positional topic list onto a part — and returns `""` when the list
length does not match the part count, because a global topic list used
positionally would label a part with someone else's subject. `build_context_block`
now states «موضوع همین بخش» and `CHUNKED_CONTENT_RULES` says that line is this
part's responsibility; parts no longer repeat their neighbours' subjects.

**Continuity flags without an extra model call.** `chunk_continuity()` derives two
facts from the part's own text: it opens mid-topic (first line is a continuation
fragment: «و …»، «بنابراین …») and it ends mid-topic (no sentence terminator).
Both flags are suppressed for punctuation-free material (raw STT), where they
would fire on every part and become noise. The context block turns each flag
into one linking instruction. No overlap was introduced: the merge and the
context block proved sufficient.

**Budget-aware outline digest.** `build_outline_document` now lowers the per-part
slice as the part count grows, keeps the total inside the budget whenever the
floor allows, and drops only the optional tails — never a part — if the digest
would still overflow. The orientation answer therefore always covers every part
positionally.

**Segmented final compilation.** `split_for_compilation()` cuts the merged notes
at section boundaries into consecutive slices that each fit one request
(`COMPILE_MAX_CHARS` minus the measured system prompt and a reserved overhead).
`build_compile_document(..., part=, parts=)` tells each slice it is a slice, so a
slice writes the summary, key points and glossary for *its own* sections instead
of a whole-lecture summary from a fragment. Each slice is accepted on its own
deterministic QA gate; accepted slices are merged with the existing conservative
merge, which unions summaries, key points, glossary, objectives and questions and
keeps the lecture's order. The slice count is bounded (`COMPILE_MAX_SLICES = 6`);
beyond it the merged notes are delivered unchanged, exactly as before.

**Targeted repair.** `_choose_repaired_draft()` judges every repaired part against
its own draft, with the same ordering as the document gate and "no measurable
improvement → keep what exists" ties. A repair that fixes one part can no longer
overwrite a part that was already right.

**Diagnostics that detect but never rewrite.** `qa.py` now counts untitled
sections (placeholder headings like «بخش ۲») and abrupt section openings: a
non-first section that neither opens with a relation marker
(چون/بنابراین/اما/برای مثال/…) nor shares a content word with the previous
section's body. Word matching strips ZWNJ and uses a Unicode letter tokenizer, so
Persian is measured correctly.

## D. DOCX improvements

* **TOC policy with two independent signals.** A TOC is written when the document
  has enough peer sections *or* enough body text
  (`TOC_MIN_SECTIONS = 4`, `TOC_MIN_BODY_CHARS = 2400`), and never when the notes
  path has no sections — and never for a raw-text document with fewer than two
  Markdown headings, which would have rendered an empty TOC page. Default
  `\o "1-1"`: block labels are real Heading 2s that repeat in every section, so a
  `1-2` TOC buried the lecture's own topics. `DOCX_TOC_LEVELS` (validated against
  `1`, `1-1`, `1-2`, `1-3`) and `DOCX_TOC_MIN_SECTIONS` expose both.
* **Cover metadata as labelled lines.** `cover_meta_lines()` splits the date,
  source, engine and tracking reference into two or three short self-contained
  lines, so every label stays next to its value; the raw-text companion file
  keeps the single-line form.
* **Adaptive cover balance.** The flexible gap above the quotation now shrinks by
  one line for each extra title line (`_wrap_estimate`) and for a longer metadata
  block, bounded at five lines, so the quotation keeps its place on the page for
  any job metadata.
* **One body-section helper.** `_add_body_section()` creates the A4 body section
  and applies the section-level `w:pgBorders`; `build_notes_docx`,
  `build_plain_docx` and the cover-less title block all use it, and a configured
  local logo now also appears in the cover-less document.

Unchanged by design: real Word `PAGE` field footer, subtle running header, real
Heading 1–3 styles, the directional-run (RTL/LTR) strategy, table header
repetition, keep-with-next headings, no remote assets, no dynamic font download,
no failure on a missing logo.

## E. Prompt and quality improvements

* The chunk prompt now names the part's own topic and marks it as that part's
  responsibility, and — through the context block — says whether the part starts
  mid-topic or ends unfinished, with the linking instruction for each case.
* `CHUNKED_CONTENT_RULES` states that the outline topics are a map, not text to
  rewrite, and that a part must not repeat its neighbours' subjects.
* The compilation instruction now covers slices coherently (summary/key points
  scoped to the slice, glossary optional) and keeps the "no content deletion,
  only verbatim duplicate repetition" contract intact.
* Full-mode prompt measured at 6 256 characters with preservation, no-invention,
  number protection and transition-keeping all present, and no compression ask.

## F. Tests executed and results

```
BEFORE  python -m unittest discover -s tests     ->  Ran 471 tests in 24.275s   OK
AFTER   python -m unittest discover -s tests -v  ->  Ran 488 tests in 25.613s   OK
```

New coverage: part context states the part's own topic and ignores a mis-sized
topic list; the outline digest labels every part under a tight budget; an
oversized booklet is compiled in labelled slices and too many slices fall back to
the merge; a damaged repair slice never replaces its own draft; untitled and
abrupt sections are reported (and a linked section is not); TOC levels and
threshold are configurable; the cover's metadata is split into labelled lines and
its quotation keeps its place with a long title; a raw-text document gets a TOC
only when it has headings; every documented `DOCX_*` switch reaches the design.

Visual validation (mandatory, and repeated after the last edit):

```
PYTHONPATH=. python out/mkbooklet.py            # 31 fixture sections -> 50 901 B DOCX
python -m scripts.render_docx_pages out/booklet_after.docx \
       --out out/pages_final --json out/facts_final.json
-> pages=13 sections=2 warnings=0, ok=true, page 826x1169 px
```

All 13 pages were inspected (cover, TOC, 11 body pages): «به نام خدا»، GAMAS /
Gamas Bot, title, mode, date/source/engine/tracking lines, the verbatim quotation
panel; running header «Gamas Bot — جزوه درسی | …», footer «Gamas Bot — صفحه n»
(cover unnumbered, body restarts at ۱), section-level page frame on every page,
sections ۱…۳۱ with shaded key-point panels, ◆ definition lists, warning/tip
callouts, formulas, numbered steps and tables whose shaded header row repeats on
the continuation pages; Latin terms inside Persian sentences stay LTR-correct
(`HbA1c`, `eGFR`, `SELECT`, `O(n log n)`); no blank page, no clipping, no orphan
heading, no bottom-margin overflow.

## G. Benchmark — BEFORE vs AFTER (deterministic, provider-free)

| measure | BEFORE (`39cfdc5`) | AFTER |
| --- | --- | --- |
| unit tests | 471 pass | **488 pass** |
| full-mode prompt chars | 6 256 | 6 256 (preservation/no-invention true) |
| fixture compression / signal / semantic | 1.007 / 1.0 / 1.0 | 1.007 / 1.0 / 1.0 |
| fixtures needing repair | 0 | 0 |
| merge probe (60 → 30 sections, 128 blocks) | 0.0 duplicate rate | 0.0 duplicate rate |
| continuation probe joins | 3/3 correct | 3/3 correct, 0 wrong, 0 prose losses |
| long lecture (61 660 chars) | 3 chunks, 83 → 81 sections, 0 split topics | 3 chunks, 83 → 81 sections, 0 split topics, 0 duplicate blocks |
| long-document DOCX facts | cover, TOC, PAGE field, page borders, Heading 1–3 | same, TOC now `1-1` |
| document structure diagnostics | not measured | 30 sections, 0 split topics, 0 untitled, 0 duplicate paragraphs, 1 repeated heading, 2 abrupt openings (real: two fixtures share «جمع‌بندی») |
| part context (new) | not measured | own topic known, context 526 chars, continuation/unfinished hints emitted, silent on punctuation-free STT |
| renderer | 11 pages, 0 warnings | 13 pages, 0 warnings, every page inspected |

A shorter document is not treated as an improvement anywhere: compression is read
together with semantic and signal coverage, and the booklet is longer than the
earlier one because the segmented compilation now always runs.

## H. Remaining limitations

* The two *model* passes (orientation, compilation) cannot be scored live without
  provider credentials; their downside is bounded by the deterministic
  acceptance gates, but a readability gain from real output still needs a human
  reviewer (`--human-out`).
* Continuity flags depend on the punctuation the STT engine produced; on
  punctuation-free transcripts they stay silent by design rather than guessing a
  boundary.
* The compile slice count is bounded (6). A pathological booklet above that bound
  is delivered as the deterministic merge, which is the previous behaviour, not a
  regression.
* The offline renderer does not evaluate Word fields, so the TOC page renders as
  the heading plus the update hint; the real `TOC \o` field is asserted on the
  document XML instead.
* `Sections that open without a link to the previous one` can fire on a genuinely
  new subject; it is a diagnostic only, never a repair trigger.

## I. Changes intentionally NOT made

* **No automatic chunk overlap.** The global context (topic, neighbours, previous
  headings, continuity flags) plus the merge and the compilation were sufficient;
  overlap would duplicate boundary material into the merge.
* **No vector store, embeddings, RAG or agent framework**, no new provider, no new
  dependency.
* **No destructive QA filters.** The new diagnostics detect; only strong evidence
  of information loss (coverage) or a measurable structural defect (acceptance
  gates) can trigger a targeted repair.
* **No full regeneration on repair**, and no second rewrite pass: the repair is
  still one optional pass, now judged part by part.
* **No schema expansion** beyond what the renderer consumes: `learning_objectives`
  and `review_questions` already existed and remain the only optional fields.
* **No change to Telegram, STT, media, database or deployment code** — the audit
  confirmed the delivery path builds `DocumentMeta` and falls back to a plain
  document on any renderer failure, which is the required behaviour.

---

## Production report (2026-09) (`PRODUCTION_REPORT.md`, removed)

Branch `arena/59eaae07-gamas-bot`, based on `main` (`cea2e61`). Every statement
below is either **verified** with a command run in this environment, or marked
**not verified** with the reason. Nothing here claims a rendered page number, a
live provider health result or a bank verification that was not actually
produced. The full-suite count quoted is the last run of this revision:

```
$ .venv/bin/python -m pytest tests/ -q
673 passed, 1 skipped, 1620 subtests passed in 44.96s
$ .venv/bin/ruff check gamas_bot scripts tests passenger_wsgi.py --select E9,F
All checks passed!
```

---

## 1. Exact plans, exactly as specified — **verified**

One canonical catalogue in `gamas_bot/billing.py`, seeded into SQLite, read by
every screen:

| Plan code | Included time | Price | Validity |
|---|---:|---:|---:|
| `free_1h` | 3,600 s (1 hour) | free | lifetime, once per user, `/start` never renews |
| `paid_25h_30d` | 90,000 s | 150,000 Toman | 30 days from approval |
| `paid_50h_30d` | 180,000 s | 250,000 Toman | 30 days from approval |

Paid entitlements are cumulative; consumption takes the entitlement that expires
soonest first. Evidence: `tests/test_billing.py` (17 tests), including the
legacy-code migration, restart idempotence, the configured-tariff seed, and the
check that no handler hard-codes a price or the card number.

## 2. Canonical card-to-card configuration in one place — **verified**

`CANONICAL_PAYMENT_CARD = "5022291332906625"`, holder «امیرعلی غمخوار», bank
«بانک پاسارگاد» live only in `gamas_bot/config.py`, are overridable from the
environment, and are grouped by the single `format_payment_card()` helper
(`5022 2913 3290 6625`). `tests/test_billing.py` asserts both the values and
that `bot.py` contains none of them.

## 3. True Persian RTL DOCX — **verified structurally, visually inspected offline**

`docDefaults`/styles/paragraph marks/runs carry BiDi, Persian runs carry the
complex-script slots (`w:cs` + `w:szCs` + `w:bCs`), Latin runs carry an explicit
`w:rtl w:val="0"`, and mixed lines are ordered logically. `python -m
scripts.validate_docx` reports 0 structural failures for both sample documents,
including `rtl_paragraphs_present`, `ltr_runs_marked`,
`complex_script_faces_set` and `element_ordering_valid`. Five real documents
(Persian-only, Persian+English medical, 22 topics, numeric/unit-heavy,
URLs/e-mail) were rendered with the repository's offline renderer and inspected
by eye.

## 4. Static table of contents with no F9 — **verified as structure; page numbers NOT validated as rendered**

Page 1 is the cover, page 2 is the topic list, page 3+ are the notes. Verified by
`tests/test_docx_layout_requirements.py` (9 tests): the heading paragraph, the
borderless RTL two-column table, unique internal bookmarks shared with the body
headings, one `w:hyperlink w:anchor` per row, Persian-digit page numbers, no
`w:instrText`, no `TOC \o` field and no `updateFields` anywhere; the footer keeps
a real `PAGE` field. `DOCX_TOC_LEVELS` defaults to `1-1`. Content-based heading
matching, so long and duplicate titles are handled.

**Not verified in this environment:** the numbers the offline renderer drew came
from an injected page map (`_rendered_toc_page_numbers` was mocked), and the
offline renderer is not Word or LibreOffice. Real rendered pagination, Word
hyperlink navigation and PDF-destination mapping require LibreOffice + `pypdf`
on the delivery host. When exact mapping is unavailable the DOCX is **not**
delivered with guessed numbers — `DocxPaginationError` fails closed, and
`tests/test_bot_presentation_flow.py` covers that path.

## 5. Integer-second entitlement/usage-ledger architecture — **verified**

`plans`, `entitlements` and `usage_ledger` tables with
reserve → consume/release, all inside SQLite transactions; grants are
idempotent, the balance can never go negative, and concurrent jobs cannot spend
the same seconds. Evidence: `tests/test_billing.py`,
`tests/test_regressions.py`, and the reservation-release tests in the
presentation flow.

## 6. Manual receipt + admin approve/reject — **verified; manual by design**

`tests/test_payment_flow.py` (17 tests + 11 subtests) covers: a payment stays
pending until a human decision; approval notifies the user once and is
idempotent (a second approval grants nothing); rejection stores a sanitized
reason and grants nothing; the receipt path is recorded with its `file_id`; the
user can submit a new request afterwards; a second image while pending is
refused with the request id instead of being queued as media.

**Approval is a manual administrative decision by design.** The bot performs no
bank, card or transfer verification and does not claim to. It displays the
canonical card, stores the private receipt for a human to inspect, and records
who decided and when. Receipts live outside the web root in `RECEIPT_DIR`
(700/600), under server-generated names, and are deleted after
`RECEIPT_RETENTION_DAYS` counted from review.

## 7. Encrypted multi-key credentials with rotation rules — **verified**

Keys are Fernet-encrypted in SQLite, masked everywhere (`••••••••1234`), with 429
→ `Retry-After` cooldown + rotation, 401/403 → quarantine, 400/415/422 → no
rotation, and bounded retries for 5xx/network failures. Evidence:
`tests/test_provider_credentials.py` (14 tests + 3 subtests), including
"secrets never reach the logs" and "all keys exhausted produces a clean,
secret-free error".

## 8. Administrator panels — **verified (rendered text and callbacks asserted)**

| Panel | Entry point | Tests |
|---|---|---|
| 💳 پرداخت‌ها (payments) | `admin:payments`, `/payments` | payment listing/detail/approve/reject |
| 🩺 وضعیت سرویس‌ها (health) | `admin:health`, `admin:health:refresh` | status text, cached badge, per-key test button |
| 🔑 API Keys (credentials) | `admin:credentials` | add/enable/disable/delete/reorder/test, mask only |
| ⏱ اعتبار کاربران (user credits) | `admin:credits`, `admin:credit:add:<id>` | balance split, entitlements, ledger, adjustment |

Every one of them was rendered in a local harness with an admin account and an
empty database, and the exact text and buttons are asserted by tests. Nothing
above required a Telegram connection.

## 9. Telegram UX menus — **verified**

The main menu, admin menu and every submenu carry the section-40 labels; the
full set is asserted through the callbacks in `tests/test_payment_flow.py` and
`tests/test_provider_health.py`. Insufficient-credit messages state available vs
required duration and offer 💳 خرید اشتراک.

## 10. Security audit — **verified where a test exists**

* **IDOR / callback spoofing:** receipts and every admin action are refused for a
  non-admin sender and in group chats (`test_a_non_admin_cannot_open_a_receipt_or_a_payment`,
  `test_group_chat_cannot_reach_the_admin_payment_panel`).
* **Path traversal:** `_safe_receipt_path` refuses `/etc/passwd`, a file outside
  the receipt root and `../` escapes (`test_receipt_paths_outside_private_storage_are_refused`).
* **Secret/receipt leakage:** masking asserted in panels, audit entries and logs;
  no receipt path is exposed on the user-facing screens.
* **Duplicate approval / quota races:** transactional single-transaction approval
  and reservation tests in `test_billing.py` and `test_payment_flow.py`.
* **Prompt/injection and prompt-injection of admin input:** rejection reasons and
  notes pass through `clean_human_text()` (ZWNJ/ZWJ preserved, control
  characters dropped).

## 11. Regression tests from §47 — **verified for the areas below**

The new regression module set is `tests/test_payment_flow.py`,
`tests/test_provider_health.py`, `tests/test_provider_credentials.py`
(extended), `tests/test_billing.py` (extended) and
`tests/test_docx_layout_requirements.py`; the whole suite passes
(673 passed, 1 skipped, 1,620 subtests).

| Regression area | Where it is covered |
|---|---|
| free grant is once-per-user and not renewed by `/start` | `test_billing.py` |
| legacy `free_lifetime_1h` → `free_1h` migration, no double grant | `test_billing.py` |
| paid plans, cumulative balance, earliest expiry consumed first | `test_billing.py` |
| concurrent reservation cannot overspend or create a negative balance | `test_billing.py`, `test_regressions.py` |
| release on cancellation/STT failure/pagination failure | `test_bot_presentation_flow.py`, `test_billing.py` |
| receipt intake, pending state across restart, duplicate approval | `test_payment_flow.py` |
| a lecture file is never swallowed by an open payment | `test_payment_flow.py` |
| rejection grants nothing and sanitizes the reason (ZWNJ kept) | `test_payment_flow.py`, `test_audit.py` |
| receipt privacy: admin-only, path-traversal-proof, group chats refused | `test_payment_flow.py` |
| 429 cooldown + rotation, 401/403 quarantine, 400/422 no rotation | `test_provider_credentials.py` |
| all keys exhausted → clean secret-free error; success clears state | `test_provider_credentials.py` |
| no secret in logs, panels or audit entries | `test_provider_credentials.py`, `test_provider_health.py` |
| health states, cache, cost-safe probes, concurrency, masking | `test_provider_health.py` |
| DOCX page layout contract and static TOC (no F9 / no `TOC` field) | `test_docx_layout_requirements.py` |
| RTL/LTR runs, tables, header/footer, element ordering | `test_docx_polish.py`, `test_docx_export.py`, `scripts/validate_docx.py` |

The brief's 60 items were **not** enumerated one-by-one against a checklist in
this report; the rows above are the areas this revision added or re-verified. No
existing test was removed or weakened (the only assertion change replaced an
over-strict expectation of exactly one cache invalidation with the correct
single production call).

## 12. Real DOCX validation — **partly verified**

`scripts/validate_docx.py` passed with 0 structural failures; the corpus was
rendered with `scripts/render_docx_pages.py` (no blank page, no overflow, no
orphan heading, in any sample). The offline renderer needed one real fix found
during this audit: it read only direct `w:r` children, so every hyperlinked TOC
row rendered blank — after the fix the topic list shows its titles and page
numbers. **Not verified:** real Word/LibreOffice rendering. LibreOffice is not
installed in this environment (`curl` reaches the network but `apt-get` cannot
resolve `libreoffice-writer`).

## 13. cPanel compatibility — **preserved, verified by inspection and preflight**

No Docker, no Redis, no external database, no new daemon, no root requirement.
SQLite (WAL) plus `RECEIPT_DIR` on disk; migrations run automatically at
startup, are transactional and idempotent — verified by applying migration 004
to a pre-004 database, restarting three times, and confirming the same balance
and no duplicated plan, entitlement or ledger row. `scripts/cpanel_preflight.py`
runs; the only FAIL in this sandbox is the intentionally absent `.env`.

## 14. Documentation updated — **verified**

`README.md` (plan table + plan keys, purchase/balance/credit/health/key
management, credential rotation, validation corpus), `.env.example` (seven plan
keys, receipt retention wording), `docs/DEPLOY_CPANEL.md` (state-on-disk table,
panels, retention), `docs/DEPLOY_FA.md` (menus, adjustments, health panel,
rotation) and `docs/AUDIT.md` (revision log with findings 1–11 and what was and
was not validated).

## 15. No unverified claims — **the rule applied to this report**

* No page-number accuracy is claimed: the numbers inspected came from an
  injected page map and the offline renderer, never from a real office render.
* No live provider health result is claimed: every probe test injects its HTTP
  response; no provider was contacted and no probe was executed against a real
  endpoint.
* Payment verification is stated everywhere as manual-administrator-by-design.
* No Telegram login, no paid STT/LLM request and no bank interaction happened
  while producing this revision.

## 16. Health checks are cost-safe and leak nothing — **verified**

Probes only call read-only list endpoints (Speechmatics jobs, Deepgram projects,
Gemini/Anthropic/OpenAI-compatible models). No transcription job is submitted
and no model content is generated; a deployment that cannot be probed safely is
reported as *configured* with a note (`probe_supported=False`). The panel is
manual and cached for 5 minutes, so it is never executed per Telegram event, and
probe results run concurrently to keep the panel responsive. Provider error text
reaches the screen only through `sanitize_detail()`; only the masked tail of a
key is ever shown, and a test asserts no key material appears in the panel text.

## 17. Minimum-change rule — **honoured**

No working subsystem was rewritten. The DOCX author, STT layer, structuring
layer and MediaWorker are untouched apart from the one renderer fix in
`scripts/render_docx_pages.py`. Existing modules gained additive functions
(`clean_human_text`, `add_audit_entry`, `admin_user_credit_overview`,
`credential_for_test`, `reorder`); no existing behaviour was replaced, no
working test was deleted, and the single assertion that was relaxed was
over-strict rather than protective (it demanded exactly one cache invalidation
where the correct design performs one production call, now asserted as such).

## 18. Remaining, explicitly open — **not verified, do not claim otherwise**

| Item | Status | What closes it |
|---|---|---|
| Real rendered page numbers / Word pagination / hyperlink navigation | not verified | install LibreOffice + `pypdf` on the host, run a long document through the pipeline, inspect the delivered PDF/DOCX |
| `soffice`-based visual validation | not verified | same |
| Live provider health check and live rotation under a real 429 | not verified | run the panel once against a real provider account |
| Live Telegram flows (menus, receipt upload, approval notification) | not verified | a manual smoke test on the deployment host |
| Real bank transfer approval | manual by design | an administrator verifies the transfer and approves |

---

## Final report (2026-09) (`FINAL_REPORT.md`, removed)

Branch `arena/1f5ae9d9-gamas-bot`, based on `main` (`faeb8cd`, "Production
hardening: plans, receipts, provider health, TOC validation (#27)"). This report
covers the full-repository audit of that revision and the changes made on top
of it. Every claim below is either backed by a command run in this environment
or marked **not performed**. Payment verification is **manual admin approval by
design**; no automatic gateway exists and none is claimed.

Verification commands (this session, this sandbox):

```
$ .venv/bin/python -m pytest tests/ -q
678 passed, 1 skipped, 1622 subtests passed
$ .venv/bin/python -m unittest discover -s tests
OK (skipped=1)
$ .venv/bin/ruff check gamas_bot scripts tests passenger_wsgi.py --select E9,F
All checks passed!
$ .venv/bin/python -m compileall -q gamas_bot scripts tests passenger_wsgi.py   # ok
$ .venv/bin/python -m pip check                                              # no broken requirements
$ .venv/bin/python -m scripts.validate_docx --out … --render                 # PASSED, offline render
$ .venv/bin/python /tmp/docx_validation/run_validation.py                     # PASSED, 0 failures
```

---

## 1. Files changed (this session)

| File | Change |
|---|---|
| `requirements.txt` | Declared `arabic-reshaper` and `python-bidi` — the offline page renderer (`scripts/render_docx_pages.py`, also used by `scripts.validate_docx --render`) imports both to shape Persian and reorder mixed lines; they were missing, so Persian pages rendered as disconnected letters. |
| `tests/test_billing.py` | +4 regression tests: paid entitlement expires exactly `starts_at + 30 days` (§47 #26); one approval creates exactly one entitlement even when clicked twice (#37); a cancelled job releases the full reservation exactly once (#33); a duplicate reservation is impossible — same-duration retry is idempotent, conflicting duration refused, balance charged once (#34). |
| `tests/test_media_intake.py` | +1 pipeline test: insufficient balance rejects the job **before** STT/LLM — neither `transcribe` nor `structure_transcript` is awaited, the reservation is recorded `insufficient` with 0 reserved seconds, the user gets the available/required message with a tracking code, and nothing is charged (§47 #29 at the bot level, §18). |
| `docs/FINAL_REPORT.md` | This report. |

No production code needed changes: the audit (§1 of the task) found the
billing/payment/credential/DOCX implementation from `faeb8cd` complete against
§0–§53 except the items above. Everything else below describes the audited
system as it stands.

## 2. Migrations added

None this session. Existing, audited and idempotent:

- `migrations/001_initial.sql` — users, submissions, transcriptions, broadcasts.
- `migrations/002_presentations.sql` — presentation columns + `presentation_clips`.
- `migrations/003_billing_and_credentials.sql` — `plans` (seeded `free_lifetime_1h`,
  `paid_25h_30d`, `paid_50h_30d`), `payment_requests`, `entitlements`
  (unique partial index: one `free_lifetime` per user), `usage_reservations`,
  `usage_ledger`, `admin_audit_log`, `provider_credentials` (ciphertext only),
  plus the one-time migration granting existing users their single free hour.
- `migrations/004_production_hardening.sql` — renames the legacy free-plan code
  to `free_1h` (guarded, never duplicates), adds `receipt_file_id`/`admin_note`,
  and the indexes the payment/credential/health panels query.

The runner (`Database._apply_migrations`) applies files in name order inside a
transaction, records them in `schema_migrations`, tolerates interrupted
`ALTER TABLE` replays, and is covered by
`test_migrations_can_be_replayed_on_an_existing_schema` and
`test_restarting_the_database_reapplies_nothing_twice`.

## 3. DOCX root cause and exact fix (RTL)

Root cause class: paragraph alignment alone cannot make a document RTL — Word
derives direction from `w:docDefaults`, style `w:pPr`, paragraph `w:bidi`,
paragraph-mark `w:rPr/w:rtl`, and run `w:rPr/w:rtl` + complex-script slots.
The fix (already in `faeb8cd`, audited line-by-line this session) lives in
`gamas_bot/docx_export.py` and `gamas_bot/bidi.py`:

- `w:docDefaults` run/paragraph defaults carry the Persian language and RTL
  base direction (`_configure_document_defaults`).
- Styles `Normal`, `Title`, `Subtitle`, `Heading 1–3`, table/callout/quote/TOC
  styles are marked RTL (`_configure_styles`, `_configure_toc_entry_styles`).
- Persian-dominant paragraphs get `w:bidi` + right alignment + `w:rtl` on the
  paragraph mark (`_enable_bidi`, `_set_paragraph_mark_direction`).
- Runs are split by `bidi.split_direction_runs`: RTL segments get `w:rtl`,
  `w:cs`, `w:szCs` and the Persian face; Latin/technical segments are explicitly
  LTR (`<w:rtl w:val="0"/>`) with `w:ascii`/`w:hAnsi` and the Latin face.
  Logical order is never reversed manually.
- Tables get `w:bidiVisual` (RTL visual column order), RTL cell paragraphs,
  repeating header rows (`w:tblHeader`), and schema-ordered `w:tblPr` children
  (`scripts/validate_docx.py` re-checks `pPr`/`rPr`/`tblPr`/`trPr` ordering).
- Header and footer are RTL too; the footer keeps a real `PAGE` field
  (`w:fldChar` + `w:instrText PAGE`), which is the only field left in the file.
- Section marks set `w:rtlGutter`; fonts are per-role (Persian body/heading,
  Latin, fallback advertised via `w:altName`), never embedded.

## 4. Static TOC implementation

The Word TOC field (`w:fldChar`/`w:instrText TOC`/F9 placeholder/
`w:updateFields`) is gone. `build_notes_docx` now:

1. writes a skeleton TOC table (`_add_static_toc_skeleton`) with fixed-width
   placeholder page numbers, followed by an explicit page break;
2. fills it from the real bookmarked headings (`_fill_static_toc`), honouring
   `DOCX_TOC_LEVELS` (default `1-1`) and `DOCX_TOC_MIN_SECTIONS`;
3. renders the document and maps every entry to a real page
   (`_rendered_toc_page_numbers`), fills the numbers
   (`_set_static_toc_page_numbers`), and re-renders until the numbers are stable
   (`_finish_static_toc`, up to 3 iterations + final verification render).

No `w:fldChar`, no `TOC` instruction, no `w:updateFields`, and no user-visible
F9 instruction remains — asserted by tests and re-verified on real generated
files this session.

## 5. Page-number calculation method

Production: LibreOffice (`soffice --headless --convert-to pdf`) + `pypdf`.
Every TOC hyperlink's PDF link destination is resolved to its physical page;
if the renderer is missing, the PDF is unusable, the link count differs from
the entry count, or a target lands before page 3, generation **fails
explicitly** (`DocxPaginationError`) — page numbers are never guessed. This
failure path was exercised live this session: with no `soffice` in the sandbox,
`build_notes_docx` on a TOC-worthy document raised
`DocxPaginationError("…LibreOffice (soffice) در سرور پیدا نشد.")`.

Sandbox validation: LibreOffice cannot be installed here (no root, no package
mirror). For §48 the measurement pass was substituted with an oracle built on
the repository's own offline renderer (`scripts/render_docx_pages.py`), which
performs a real page layout of the .docx. Each TOC number written into a
validated document therefore comes from a real rendered layout, and was then
**cross-checked against an independent render of the final document**: every
number in the TOC equals the physical page where that heading actually starts.
This is a validation-harness substitution only; production code still requires
LibreOffice and still fails explicitly without it.

## 6. Bookmark/hyperlink implementation

- Every body heading gets a unique bookmark `GamasHeading{n}`
  (`_add_bookmark`) — valid OOXML name, unique by counter, so duplicate topic
  titles still get unique anchors.
- Each TOC row's title cell is a `w:hyperlink w:anchor="GamasHeading{n}"` with
  `w:history="1"` and a tooltip; runs inside keep their bidi segmentation.
- Verified on real files: bookmarks unique and name-valid, every anchor
  resolves to a `w:bookmarkStart`, hyperlink count equals TOC row count.

## 7. Billing schema

Integer seconds everywhere; no float hours in accounting. `plans`
(id/code/name/included_seconds/validity_days/price_toman/enabled/sort_order),
`entitlements` (granted/remaining seconds, `starts_at`, `expires_at`, status
`active|expired|revoked|exhausted`, `source` `free_lifetime|payment|admin_credit`,
`payment_id`, `granted_by_admin_id`), `usage_reservations`, `usage_ledger`
(event types `reserve|consume|release|denied|grant|adjustment`), plus the
unique partial index `idx_entitlement_free_once_per_user`. The canonical
catalogue lives in `gamas_bot/billing.py::plan_catalog`, is upserted by
`Database.sync_plan_catalog` at startup, and is overridable only through the
seven `FREE_PLAN_HOURS`/`PLAN_*` environment variables resolved in
`gamas_bot/config.py`.

## 8. Reservation/usage logic

`Database.reserve_usage` runs in a `BEGIN IMMEDIATE` transaction: it verifies
submission ownership, expires stale entitlements, sums available seconds,
refuses with an audited `denied` ledger row when the balance is short, and
otherwise allocates from the earliest-expiring active entitlement first
(free/lifetime entitlements last). `finalize_usage` consumes the actual media
seconds and refunds the unused remainder per entitlement; `release_usage`
returns the full reservation. All three are idempotent and guarded by
`WHERE status='reserved'`, so retries, restarts
(`_recover_interrupted_submissions` releases reservations of jobs that cannot
survive a restart) and double-clicks cannot double-charge or lose credit.
Cross-process safety is tested with two `Database` connections racing on one
balance (`test_two_database_connections_cannot_reserve_the_same_credit_twice`).

## 9. Payment workflow

`💳 خرید اشتراک` → plan picker → instructions showing plan, hours, validity,
price, and the canonical card (`5022 2913 3290 6625` / امیرعلی غمخوار /
بانک پاسارگاد — normalized to digits in config, grouped by the single
`format_payment_card` helper) → "رسید پرداخت را به‌صورت تصویر ارسال کنید".
An image (JPEG/PNG/WebP, magic-byte checked, ≤ `MAX_PAYMENT_RECEIPT_BYTES`,
stored under `RECEIPT_DIR` — outside the web root, dir `700`/file `600`,
random hex name, never the user's filename) creates a `pending`
`payment_requests` row. **No credit is granted at submission time.** Receipts
never enter the STT/PPT pipeline (image-with-open-payment is intercepted first;
tests `test_receipt_image_with_an_open_payment_is_never_a_media_job` etc.).
Old receipts are deleted only after the configurable retention measured from
admin review.

## 10. Admin workflow

Admin-only, private-chat-only panels: `💳 پرداخت‌ها` (pending list → detail with
the private receipt → `✅ تایید` / `❌ رد`), `⏱ اعتبار کاربران` (per-user
free/paid/total, entitlements with expiry, usage history, manual `seconds |
reason` adjustment with audit), `🩺 وضعیت سرویس‌ها` (manual, 5-minute cache,
cheap read-only probes; states configured/healthy/degraded/rate_limited/
authentication_failed/unavailable/disabled/not_configured; masked credentials,
sanitized errors, cooldown display), `🔑 API Keys` (add → metadata → secret
message deleted before storage; enable/disable/delete/reorder/test), `📜 گزارش
مدیر` (audit log), plus stats/users/broadcast/ban/unban. Every payment decision,
credential change and adjustment writes an `admin_audit_log` row without
secrets or receipt contents.

## 11. Credential architecture

`gamas_bot/provider_credentials.py::ProviderCredentialManager` pools
per-(service, provider) keys from `provider_credentials` (Fernet ciphertext at
rest; the master key is env-only via `PROVIDER_CREDENTIALS_ENCRYPTION_KEY` and
never touches SQLite), ordered by `priority, id`, skipping quarantined and
cooling-down keys, with the legacy environment key as a trailing fallback.
`gamas_bot/provider_health.py` runs the manual probes and feeds the same
rotation state. Both STT (`gamas_bot/stt.py`) and notes
(`gamas_bot/structuring.py`) obtain credentials through the manager via
`current_provider_credentials()` / `use_provider_credentials()` and report
results through `record_result`.

## 12. 429 rotation logic

On HTTP 429: the credential is marked `cooldown` with `cooldown_until` from
`Retry-After` (bounded 1 s … 7 days, default 30 s), excluded from selection
until it expires, and the next healthy credential is tried — the same key is
not hammered. On 401/403 the credential is `quarantined` (excluded until an
admin re-enables it). On 400/415/422 the request is treated as invalid and the
credential is **not** rotated. 5xx/network errors get bounded exponential
retries (`STT_MAX_ATTEMPTS`, `STT_RETRY_*`; note API `NOTE_API_RETRIES`), then
rotation/provider fallback. Success clears cooldown/quarantine. All-credentials
exhausted returns a clean, secret-free error. Tests:
`test_429_cools_down_the_first_key_and_rotates_to_the_next`,
`test_401_quarantines_the_first_key_and_rotates_to_the_next`,
`test_bad_request_does_not_rotate_credentials`,
`test_retry_after_header_is_honoured_and_bounded`,
`test_retries_are_bounded_by_the_configuration`,
`test_provider_secrets_never_reach_the_logs`, and more in
`tests/test_provider_credentials.py`, `tests/test_provider_health.py`,
`tests/test_provider_robustness.py`, `tests/test_provider_contracts.py`.

## 13. Security changes (audit result)

No new holes found; the existing controls were verified by test and inspection:
admin callbacks require `is_admin` and private chat; `billing:*` callbacks are
private-only and ownership-checked in SQL (`cancel_payment_intent`,
`submit_payment_receipt` both scope `WHERE user_id=?`); payment actions are
whitelisted; receipt paths are re-resolved and confined to the private root
(`_safe_receipt_path`); receipt bytes are never logged; API keys are
Fernet-encrypted, masked (`••••…last4`) in every UI/audit/log path, and
rejected from labels/URLs/notes that contain key fragments; settings `repr`
hides secrets; SQL is parameterized throughout; the audit log truncates and
never stores secrets. New this session: the insufficient-balance path is
covered end-to-end at the pipeline level (no STT call, no charge).

## 14. cPanel impact

None. No new daemon, no Redis, no Docker, no external DB, no root requirement.
SQLite + WAL with owner-only modes, receipts under `RECEIPT_DIR` outside the
web root, migrations applied automatically at startup, the env-supplied
encryption master key, and the existing `passenger_wsgi.py`/launcher/systemd
paths are unchanged. `scripts/cpanel_preflight.py` and
`tests/test_cpanel_deploy.py` pass. Restart persistence is tested
(`test_payment_and_entitlement_state_survive_a_restart`).

## 15. Tests executed

- `python -m pytest tests/ -q` → **678 passed, 1 skipped, 1623 subtests**
  (673 before this session; +5 new tests: 4 billing + 1 pipeline).
- `python -m unittest discover -s tests` → **OK** (CI entry point).
- `ruff check … --select E9,F`, `compileall`, `pip check` → **clean**.

## 16. Test results

All green. The §47 list is covered as follows (test names in
`tests/test_billing.py`, `tests/test_payment_flow.py`,
`tests/test_provider_credentials.py`, `tests/test_provider_health.py`,
`tests/test_docx_export.py`, `tests/test_docx_layout_requirements.py`,
`tests/test_docx_polish.py`, `tests/test_media_intake.py`,
`tests/test_provider_robustness.py`, `tests/test_provider_contracts.py`):
DOCX #1–#20 ✓ (RTL doc/styles/paragraphs/runs/tables/glossary/header/footer,
static TOC, real titles+numbers, no F9, no `w:fldChar`, TOC page 2, body page
3, unique bookmarks, resolving hyperlinks, duplicate titles, reopen, schema
order). Billing #21–#38 ✓ (incl. this session's #26/#33/#34/#37 and the
pre-existing #21–#25, #27–#32, #35, #36, #38). Payments #39–#47 ✓.
Provider credentials #48–#60 ✓ (multi-key, encryption, masking, no log leaks,
429 cooldown + Retry-After, rotation, 401/403 quarantine, 400/422 no-rotate,
bounded 5xx/network, recovery on success, exhausted-pool error, admin-only
health panel).

## 17. Real DOCX validation results

Performed this session with the repository's own tooling
(`scripts/validate_docx.py`, `scripts/render_docx_pages.py`):

- `python -m scripts.validate_docx --out … --render` → **PASSED**, offline
  render, "no layout warnings (no blank page, no overflow, no orphan heading)".
  (This command previously degraded Persian because `arabic-reshaper`/
  `python-bidi` were undeclared; fixed in `requirements.txt`.)
- Four real sample documents generated and validated end-to-end:
  - `persian_only_4topics` — 4 pages, TOC 6 rows, numbers [3,3,3,3,3,4]
  - `mixed_medical_10topics` — Persian+English medical (HbA1c, eGFR,
    Metformin 500 mg, 120/80 mmHg, mg/dL, %, URLs, emails, Cyrillic/CJK
    stress text), 5 pages, TOC 14 rows
  - `long_22topics` — 22 topics, long titles, duplicated titles, 7 pages,
    TOC 24 rows, numbers [3…7]
  - `numerical_tables` — numeric tables, ranges, formulas, URLs, dates,
    3 pages, TOC 5 rows
- For every document: structural validation ok; XML checks ok (no TOC field, no
  F9 text, no `updateFields`, footer `PAGE` field intact, bookmarks unique/
  valid, hyperlinks resolve and match TOC rows, docDefaults/Normal/headings/
  tables/header/footer RTL, Latin runs explicitly LTR, complex-script fonts);
  real render with **zero layout warnings**; **page 1 = cover only, page 2 =
  static TOC, page 3 = first body heading**; and every TOC page number equals
  the physical page where that heading starts in an independent render of the
  final file.
- Pages 1–3 of the mixed-medical and long documents were **visually inspected**
  (rendered PNGs): RTL cover with brand/quote, RTL TOC with page numbers in
  the left column, mixed-script titles and paragraphs in correct logical
  order, RTL tables with the first logical column on the right, centered LTR
  formulas, header/footer with the page field, page frame — all correct.
- Explicit-failure path verified live: without a renderer, a TOC document is
  **not** produced (raises `DocxPaginationError`).

Honesty note: these page numbers come from the repository's offline renderer
used as the measurement oracle, because LibreOffice is not installable in this
sandbox. In production the numbers come from LibreOffice itself, and without it
the document is not delivered at all. Word's own pagination may differ from
any renderer by a line; the numbers are correct for a real rendered layout, not
guaranteed pixel-identical to Microsoft Word.

## 18. Remaining limitations

1. **LibreOffice absent in this sandbox** — production TOC pagination was
   verified only through its explicit-failure path plus the offline-renderer
   oracle; a deployment run with real `soffice` has not been executed here.
2. **No live provider health check was executed** — no real API keys exist in
   this environment; health/rotation behaviour is verified with scripted HTTP
   fakes only.
3. **Payment verification is manual by design** — card-to-card receipt review
   by an administrator; there is no automatic gateway and none is claimed.
4. The offline renderer is a layout approximation (DejaVu faces, approximate
   justification, no tab leaders drawn); it is used for validation, not as a
   Word clone. Missing-glyph boxes for CJK in the sandbox renders are a font
   limitation of the renderer, not a document defect.
5. The 1 skipped test is a pre-existing POSIX/Windows-conditional skip
   (unchanged from `main`).

