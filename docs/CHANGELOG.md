# Changelog

Entries describe user- and operator-visible change. For anything older than
v3.0.0, read the git history — the v2 provider platform those entries describe
no longer exists in the code.

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
  replaced by a budget derived from the context window (with an absolute
  ceiling); `NOTE_COMPILE_MAX_CHARS` overrides it, `0` means automatic.

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
