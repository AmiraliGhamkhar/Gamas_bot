# Changelog

Entries describe user- and operator-visible change. For anything older than
v3.0.0, read the git history — the v2 provider platform those entries describe
no longer exists in the code.

## Unreleased — 2026-10-18 — audit fixes: money path, recovery, fidelity

Fixes for the 2026-10-18 audit of `edc5b2c` (`AUDIT_2026-10-18_edc5b2c.md`).

### Billing

* **A payment now carries its purchase terms.** `payments` snapshots
  `purchased_seconds`, `purchased_price` and `purchased_validity_days` when the
  request is created (migration `v3/003`), and approval credits that snapshot.
  Deactivating, deleting or re-pricing a plan between purchase and review can no
  longer approve a payment for zero seconds. An approval whose snapshot is
  missing is refused instead of reported as success, and the admin revenue figure
  sums what users were quoted.
* **One open request per user** is enforced by a partial unique index, and a
  second tap on the same plan reuses the existing row.
* **Rejections require a reason**, which is stored on the payment and in the
  audit entry; the audit row now commits in the same transaction as the status
  change.
* **`release_hold` takes the database write lock.** It could previously commit
  another coroutine's pending ledger insert, which could make a debit durable
  while its hold stayed open.
* **The free plan is granted atomically**, so concurrent `/start` handlers can
  no longer both grant it.

### Recovery, retention, storage

* Stale `submission-*` / `deck-*` / `office-*` work directories are removed at
  startup, as MEDIA.md always claimed.
* Receipt retention sweeps once at startup and then daily, so a bot that
  restarts at least daily still enforces `RECEIPT_RETENTION_DAYS`.
* `Database.connect` forces `0600` on the database and its WAL sidecars, and the
  importer does the same, independent of the entry point's umask.
* The bot creates `TEMP_DIR` and `RECEIPT_DIR` with `0700` at startup.

### Correctness and safety

* A file whose duration cannot be measured is refused before transcription
  instead of being reserved at 600 s and settled at 0.
* Audio extraction always receives a byte budget, so an unmeasurable duration
  cannot expand into an unbounded PCM WAV.
* Excel keeps zero-padded identifiers (`007`, `09123456789`) as text; only
  plain numbers become numeric.
* A Free Office heading with no body becomes a slide instead of disappearing,
  and a slide-cap truncation is logged.
* Office text uploads are size-checked before download, closing the one intake
  path that bypassed the queue's back-pressure.
* The raw-text fallback pages up to 40 messages instead of 4.
* A failed note chunk cancels its siblings instead of leaving provider requests
  running after the job failed.
* Broadcasts pass `parse_mode="html"` (falling back to plain text per page) and
  read ids only, instead of loading every user's balance first.
* The banned flag is enforced on messages and callbacks, with audited `/ban` and
  `/unban`; admins can no longer grant credit to another admin.
* Deck entry names containing control characters are rejected, and deck part
  names are single-lined before logging.
* The receipt image signature is re-checked before an admin views it.
* The v2 importer's balance verification uses the same predicate as the ledger
  replay (active, not expired) and ignores the deliberate century credit for
  unlimited users, so ordinary v2 databases import again.

### Documentation

`SECURITY.md`, `BILLING.md`, `ARCHITECTURE.md`, `OFFICE.md`, `MEDIA.md`,
`TESTING.md`, `STT_PROVIDERS.md` and `TROUBLESHOOTING.md` now describe what the
code does (including the Speechmatics `auto` → `multi` mapping and the
normalisation the Office section performs). `__version__` matches
`pyproject.toml`.

## Unreleased — 2026-10-17 — every slide word reaches the note model

A deck's own words are the first input of the booklet prompt. Three paths
could still keep them out of the request, and none of them said so.

### Fixed

* **A deck python-pptx cannot open no longer loses its text.** Slide text is
  now read straight from the OPC package — `p:sp`/`a:r` runs, grouped shapes,
  tables, speaker notes — whenever python-pptx is missing or raises on a deck.
  The reader was checked against python-pptx on the sample decks; without this
  fallback the note request silently held the narration only.
* **SmartArt and chart text is read.** `ppt/diagrams/dataN.xml` and
  `ppt/charts/chartN.xml` hold words that are not shape text frames, so a
  diagram-only or chart-only slide read as empty — and a deck with no other
  text was rejected as "no slide text and no audio". Diagram nodes, chart
  titles, series names and category labels now join that slide's outline.
* **A deck with no readable text says why.** The raw transcript (and the
  delivered `.txt`) carries the reason, instead of the booklet quietly becoming
  a narration-only transcript, and the job logs
  `slides=… outline_chars=… clips=…` so an empty extraction is visible.
* **ODP/OTP uploads are refused at intake** with a message naming the format to
  send instead of being queued and failing later with a misleading reason.
* **Soft line breaks are normalised.** U+000B (PowerPoint's in-run break) and
  `a:br` are folded into a space, so Persian titles such as `Data \v Science`
  reach the model as clean prose.

## Unreleased — 2026-10-17 — the Free Office section

A second front door: the user's own text becomes a real Office file.

### Added

* **📄 آفیس رایگان** in the user menu. Pick Word, Excel, PowerPoint or PDF,
  send the text (or upload a `.txt`/`.csv`/`.tsv`/`.md` file), and the
  document is delivered — right-to-left and in the same brand as the lecture
  booklet.
* **The section is free.** It never reserves, charges or releases credit; a
  document costs the user nothing, and a failed build costs them nothing
  either. Only a `submissions` row of kind `office` is recorded.
* **The content and the format may arrive in any order**, and several messages
  may be collected into one document. «➕ افزودن متن» extends a document that
  was just delivered.
* **Word** reuses the booklet engine (`docx_export.py`), so the document has
  the cover, the topic list, the page frame, the running header and the footer
  page field. A pasted grid becomes a **real Word table**.
* **Excel** is a right-to-left sheet with a navy repeating header row, frozen
  headings, zebra rows and sized columns. A cell that is only an ASCII number
  becomes a real number; Persian digits stay text.
* **PowerPoint** is 16:9 with a title slide, an agenda, one slide per section
  and a closing slide; the full text of each section is kept in the notes pane.
  A text with no headings of its own is cut into slides of six lines, and its
  first line titles the deck.
* **PDF** is rendered by `reportlab` from the same parsed blocks: the
  booklet's cover, an optional clickable topic list, the page frame, the
  running header and the footer page number. Persian is shaped with
  `arabic-reshaper` + `python-bidi` and set in the bundled Vazirmatn face
  (SIL OFL, `assets/fonts/`). The canvas is `invariant`, so the same text
  always yields the same bytes, and — like the other formats — no model is
  ever called for it.
* **`reportlab`, `arabic-reshaper` and `python-bidi`** join the dependency
  list (`.pdf` writing); they are pure Python, like the rest of the Office
  stack, and `pypdf` is added as a dev-only extra so the tests can read the
  produced files back.
* **One text parser** (`gamas_bot/mdblocks.py`) now reads both the lecture
  pipeline's Markdown and the user's text, so the two can never disagree about
  what a line is.
* **The model layer is optional and checked.** A model may only *arrange* the
  user's text (slide order, table); every line and cell it proposes is
  validated against the source, and anything invented is dropped. With no
  provider configured, the deterministic fallback runs and the section still
  works.
* **Configuration**: `OFFICE_ENABLED`, `OFFICE_MAX_INPUT_BYTES`,
  `OFFICE_MAX_CHARS`, `OFFICE_MAX_SLIDES`, `OFFICE_SESSION_TTL_MINUTES`.
* **`openpyxl`** joins the dependency list (`.xlsx` writing); it is pure
  Python, like the rest of the Office stack.

## Unreleased — 2026-10-16 — matched free-model pair, larger note requests

The note chain's two defaults are now chosen as a pair, and every size limit
is derived from that pair instead of from a generic 32K assumption (provider
data re-verified 2026-10-16; see `docs/PROVIDERS.md`).

### Changed

* **The OpenRouter default is a pinned free model, not the free router.**
  `openrouter/free` selects a model per request, so the window serving a
  request was unknown when the bot sized it. The default is now
  `google/gemma-4-31b-it:free` (served 262,144-token context, 32,768
  completion cap, `response_format`-capable). Nara's default is unchanged
  (`agnes-2.5-flash`).
* **Note requests are sized for the matched pair.** `NOTE_CONTEXT_TOKENS`
  defaults to 131072 (half the matched 262K served window, leaving margin
  for Nara's unpublished gateway cap) and `NOTE_CHUNK_CHARS` to 64000, so a
  lecture takes roughly a third as many map requests as before.
  `NOTE_MAX_OUTPUT_TOKENS` stays 16384, within both models' completion caps.
* **The compile pass budget is token-derived.** The fixed 48,000-character
  bound that silently skipped the editorial compile for larger booklets is
  replaced by a budget derived from the context window and the actual compile
  prompt (with an absolute ceiling); a positive `NOTE_COMPILE_MAX_CHARS` can
  further lower that budget, while `0` means automatic.

### Added

* **`NOTE_REASONING_EFFORT`** (unset by default): `reasoning_effort` for the
  Nara/OpenRouter note routes only; unknown values resolve to "send nothing".
* **Per-request usage logging.** Every completed note request logs its served
  model and real input/output token counts, so the conservative
  character-to-token estimate can be recalibrated from Persian traffic.

## Unreleased — 2026-10-12 — documentation rewritten against the code

### Fixed

* **The documentation had drifted far from the code.** An audit found
  `ARCHITECTURE.md` listing seven modules that do not exist (`structuring.py`,
  `editorial.py`, `qa.py`, `units.py`, `database.py`, `provider_credentials.py`,
  `provider_health.py`); `SECURITY.md` and `DEPLOY_CPANEL.md` documenting a
  Fernet-encrypted credential vault and a `provider_credentials` table that v3
  removed; `DEPLOY_CPANEL.md` requiring LibreOffice and `pypdf`, which no code
  path uses; and eighteen environment variables documented across the pages that
  `config.py` does not read (`MAX_CONCURRENT_JOBS`, `DATABASE_PATH`,
  `TELEGRAM_PROXY`, `DOCX_TOC_ENABLED`, `PROVIDER_CREDENTIALS_ENCRYPTION_KEY`
  and others — the real names are `MAX_ACTIVE_JOBS`, `DB_PATH` and so on).
  `BILLING.md` named three tables that do not exist; the real ones are `ledger`
  and `holds`. `OPERATIONS.md` still advertised the removed v2 STT platform.
  Every page is now rewritten from the code, and the six point-in-time audit
  reports (`AI_PLATFORM_AUDIT`, `AI_PROVIDERS`, `AUDIT_HISTORY`,
  `ENGINEERING_AUDIT`, `OVERHAUL_PLAN`, `REPORT_PRODUCTION_IMPROVEMENTS`) are
  deleted: between them they cited roughly a hundred files and tests that no
  longer exist, and they remain recoverable from git history.
* **`--check`'s "credential but no model" diagnostic is a FAIL, not a WARN.**
  `CONFIGURATION.md` claimed it was advisory; it is not in `ADVISORY_PROBLEMS`,
  so it blocks startup. The documentation now says FAIL.

## Unreleased — 2026-10-12 — NaraRouter contract: 413, output budget, request_id

Audited `gamas_bot/llm.py` against NaraRouter's published contract
(<https://router.bynara.id/docs> and the live `/api/plans`). The endpoint, Bearer
authentication, request fields, default model (`agnes-2.5-flash`, on the published
Free plan) and the withholding of `response_format` were already correct.

### Fixed

* **HTTP 413 was treated as a permanent rejection, so an oversized input was
  never split.** NaraRouter documents `413 / bad_request` as "The request body or
  input is too large", but context overflow was only tested on `status == 400`,
  and NaraRouter's 413 wording names no context window — so it fell through to
  `LLMRequestError`. The note pipeline catches only `LLMContextOverflowError`, so
  a document refused for size failed the whole job (raw transcript, no booklet)
  instead of being halved at a paragraph boundary. 413 is now a context overflow
  on its own status, and the "too large" family joined the wording markers for
  providers that answer 400.
* **A truncated answer was indistinguishable from a broken one.** The parser
  ignored `finish_reason`, so a reasoning model that spent its whole `max_tokens`
  thinking (documented NaraRouter behaviour: the answer then comes back empty)
  surfaced as "the answer was empty", and JSON cut off mid-object as "invalid
  JSON". Both were handed `complete_json`'s recovery retry, which re-sends the
  same budget and cannot succeed. `finish_reason: "length"` — with Gemini's
  `MAX_TOKENS` and Anthropic's `max_tokens` — now raises the new
  `LLMOutputBudgetError`, which names `NOTE_MAX_OUTPUT_TOKENS` and is permanent
  for that provider.
* **The provider's `request_id` was discarded.** NaraRouter's error envelope
  carries it precisely so a failure can be matched against its logs. A logged
  failure now includes `[request_id=…]`, scrubbed for key material.

### Added

* `LLMOutputBudgetError`, the output-side sibling of `LLMContextOverflowError` —
  the two size failures now have distinct classes because only one of them is
  fixed by asking again with less input.
* 14 tests, including an end-to-end note-pipeline test that a 413 splits the
  transcript and merges both halves with no content lost. It fails against the
  previous `llm.py`.

## Unreleased — 2026-10-12 — file delivery, ordered note failover, transcript safety

### Fixed

* **File delivery used an API that does not exist.** `_send_file` called
  `message.reply(file=…, caption=…, force_document=True)`. `Message.reply`
  forwards to `TelegramClient.send_message`, which has **no `caption`
  parameter** — so every raw-transcript and Word delivery died with
  `send_message() got an unexpected keyword argument 'caption'`. Deliveries now
  go through `TelegramClient.send_file`.
* **The test double hid that defect.** `FakeJobEvent.message` accepted any
  keyword, so the suite stayed green while production failed. The doubles now
  bind every call against the installed Telethon signature.
* **The transcript is persisted before it is delivered.** A Telegram failure
  during raw delivery no longer discards a finished transcription: the
  `transcriptions` row is written first, and a refused `.txt` upload falls back
  to paginated chat messages before the job may fail.
* **Bot-level tests were reaching the network.** They now run with no note
  endpoint configured and use a scripted session.
* **`scripts/cpanel_preflight.py` probed a guessed note host** (`api.openai.com`,
  always). It now probes exactly the eligible routes.

### Added

* **Ordered note providers.** `NOTE_PROVIDERS` (default `nara,openrouter`) *is*
  the failover policy, with per-provider configuration. A provider is eligible
  only when it is listed **and** has its own credential. The older
  `NOTE_PROVIDER`/`NOTE_API_KEY`/`NOTE_BASE_URL` slot keeps working and is
  classified by host, so a NaraRouter base URL stays the Nara leg rather than
  becoming a duplicate gateway.
* **Token-aware request sizing.** `NOTE_CONTEXT_TOKENS` minus the output budget,
  system prompt and contextual material becomes the chunk budget;
  `NOTE_CHUNK_CHARS` stays an upper bound. An oversized request is halved at a
  paragraph boundary and retried, never re-sent unchanged.
* **`response_format` is only sent to providers that document it** — NaraRouter
  never receives an unsupported parameter.
* **Diagnostics that say what is wrong**: a provider with a credential but no
  model, a token budget with no room for the transcript, and the eligible
  provider list in `--check`.

## Unreleased — 2026-10-12 — special (unlimited) users

* Admins run `/unlimited <user_id> on <reason>` or `… off`. A special user's jobs
  skip the credit check and reservation and are never debited, while the
  submission still records its duration. A job already running when the flag is
  set is released, not charged. Balances show «نامحدود» and the admin list marks
  these users ⭐. Admins cannot be made special; every change is audited.
  Schema: `migrations/v3/002_special_users.sql`.

## Unreleased — 2026-10-12 — separated admin and user panels

* **The user panel is the root screen for every account.** `/start` always
  renders the user menu; admins get one extra button, «🛠 پنل مدیریت», into their
  own panel. Previously the root menu swapped to the admin panel, so its "back"
  button looped and the user flow was unreachable.
* **The admin panel is its own screen tree** under `adm:*` with a tab bar on
  every screen (overview, users, payments, broadcast, audit).
* **Receipts are reviewable from the payments screen** — `adm:rcpt:<id>` sends the
  image to the reviewing admin with approve/reject buttons on that message.
* **Panel boundaries are enforced server-side.** `payv:*` is now admin-only;
  previously any user could approve a payment by crafting callback data.

## v3.0.0 — 2026-10-11 — complete core overhaul

* Replaced the v2 AI/STT platform stacks and their route, catalog, quota and
  credential-management layers with a compact v3 application: `stt.py`,
  `transcript.py`, `llm.py`, `notes.py`, `db.py`, `jobs.py` and a user-focused
  `bot.py`.
* Made the raw transcript a first-class output: timing-aware paragraphing,
  fidelity-gated typography, boundary-aware note chunks, and delivery before note
  generation so a model failure cannot hold back the transcript.
* Reworked persistence around a fresh eight-table schema and an append-only
  credit ledger, with a one-shot `scripts/import_v2.py` for supported user,
  payment and balance data.
* Simplified provider configuration to environment credentials and ordered
  failover. Runtime dependencies are generated from `pyproject.toml`.
* Updated CI to test the real v3 database API and schema, run both supported test
  runners, and smoke-test the PyAV/`ppt2pptx` media worker.

## Before v3.0.0

The v2 change history — the 15-provider registry, encrypted credential vault,
route and quota platforms, admin AI/STT panels and their audits — is preserved in
git history at tag `v3.0.0^`. None of it describes code that ships today.
