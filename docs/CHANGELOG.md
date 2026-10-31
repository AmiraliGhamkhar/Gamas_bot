# Changelog

## Unreleased — reliable broadcast retries and a working console entry point

- **Flood waits no longer skip the recipient that triggered them.** A broadcast retries the same
  Telegram message after the full server-specified wait (with a small clock-boundary margin) and
  keeps going after permanent per-user failures. The admin now sees delivered and failed totals;
  the audit record stores counts without storing broadcast text.
- **The installed `gamas-bot` command now starts correctly.** Its setuptools entry point is
  synchronous and no-argument, as console scripts require; the async bot lifecycle remains an
  internal coroutine. `python -m gamas_bot` is unchanged.

## Unreleased — opt-in OCR for scans and photos (i2ocr), reviewed and hardened

- **A scanned PDF or a photo can now become a study document — if the operator opts in.** The
  document pipeline stays text-layer only by default: with `OCR_ENABLED=false` (the default) a
  text-poor PDF and every photo are refused exactly as before. With `OCR_ENABLED=true`, only the
  text-poor pages of a document (never a page that already has text, never a `.docx`/`.pptx`) are
  sent to i2ocr.com and the recognised text is stored as ordinary page sections; an uploaded photo
  becomes a one-page document (`source_type='image'`, migration 017). The transport is the optional
  `.[ocr]` extra (`curl-cffi`), so `requirements.txt` and a plain cPanel install are unchanged, and
  `check()` refuses to start when the switch is on without it. See `docs/OCR.md`.
- **The upstream `i2ocr-py` client was replaced, not vendored.** Fourteen defects are fixed and
  pinned by tests: the `unicode_escape` fallback that turned Persian into `Ø³` garbage whenever a
  page contained a lone backslash, HTML entities leaking out of the textarea variant, one-page-only
  PDFs (one upload now serves every page and the service's own page name is honoured), no retries,
  mute CAPTCHA failures, an unsanitised file name in the multipart header, unbounded uploads,
  opaque JSON errors, a trusted CAPTCHA range, a fabricated `took`, an eager unretried session, no
  throttling, a hard-coded `kazakh` default, and blocking calls with no thread contract.
- **Bounded by construction.** `OCR_LANGUAGE` (`persian` default, `fa`/`farsi` aliased),
  `OCR_MAX_PAGES` (20) and `OCR_TIMEOUT_SECONDS` (600) cap what leaves the host and how long it may
  take; one client serializes requests with a 1.5 s floor between them, so two scans queue instead
  of racing the free tier. A failed service call stores nothing and says
  «خواندن تصویری سند ناموفق بود…», so a resend is clean; a successful but empty recognition asks
  for a clearer photo instead of saving an empty document.

## Unreleased — learning generation never loses a paid answer to an anchor mismatch

- **An item is anchored, not discarded, when its locator cannot be verified.** The remaining way a
  fully-populated, fully-grounded model answer still ended as «ساخت محتوا ناموفق بود» was the
  locator check in the chunk generator: a model that omitted `source`, invented one, or named the
  section in its own words lost **every** item in every chunk, both attempts failed and a billed
  generation was refunded. The chunk an item was generated from *is* its source, so
  `learning.anchor_items` now stores either (1) the chunk's own spelling of a locator the model
  actually named, (2) a page/slide marker that literally occurs in the text the model read, or
  (3) the chunk's own label. Nothing the model invented is ever stored, and
  `learning.verified_source` is the single place that decides this, so the parser and the generator
  cannot disagree. The stored spelling is canonical: two cards from one page now cite it identically.
- **A short answer is topped up instead of failing the chunk.** A model that returned 3 of 5
  questions used to leave the chunk short (that count was the result). The second, still-bounded
  attempt now asks only for the items that are **still missing** («دقیقاً ۲ سؤال») and explicitly
  tells the model not to repeat what it already produced; the merge is de-duplicated per chunk.
- **A failed generation says why.** `learning.GenerationShortfallError` carries the counts, and the
  failure frame now reads «سرویس هوش مصنوعی پاسخ معتبری نداد» / «از این بخش‌ها هیچ مورد معتبری ساخته
  نشد» / «فقط ۲ مورد معتبر از ۶ مورد ساخته شد؛ حداقل ۵ مورد لازم است» above the unchanged retry
  hint and the refund notice. «ناموفق بود» alone was not diagnosable for a user or an operator.
- **A provider that rejects structured output still answers.** The learning calls always sent
  `response_format`/`responseSchema`; a route that refuses it failed *every* quiz/flashcard request
  while note generation — which honours `NOTE_STRUCTURED_OUTPUT` and retries without the schema —
  kept working, which is exactly the asymmetry a user reports as "booklets work, quizzes do not".
  The bounded repair attempt now sends no schema (the prompt's own JSON shape and the parser's
  validation still apply), so a rejected parameter is never repeated.
- **Tolerant field reading, never tolerant grading.** Providers that ignore the JSON schema answer
  `explanation`, `question`/`answer`, or «منبع» instead of the example's field names; those are
  read now. An answer key written as a letter («ب», `B`) or counted from 1 is mapped exactly once —
  and only when the payload actually contains a `4` **and** no `0`, so a legitimate 0-based quiz
  whose key never points at the first option is never shifted by one (a silently wrong answer key
  is worse than a dropped question).
- **The one short title is used by the artifact itself, in every section.** Media booklets already
  carried it; document-note booklets, Free Office exports and flashcard Word exports now do too: the
  file's own cover/title, its file name, its caption and its library row are the same 2–4-word name
  (`short_title`), instead of a library row that disagreed with the file it opened. Transcript and
  document-mirror library rows store that name as well, so no row is a Telegram caption
  («📝 متن خام — GMS-000123») any more.
- **The remaining actions show a bar before they start.** Deck → Word export, library «دریافت مجدد»
  and document «دریافت فایل اصلی» are Telegram round trips for possibly-large files; each now shows
  a `WorkProgress` frame before the first request, ends on the completed frame (with its own
  buttons) or on the 🛑 frame with the reason, and never leaves a frozen panel.
- Offline regression tests: `anchor_items`/`verified_source` (invented locator never stored, inner
  marker kept, missing locator anchored), letter/one-based/key-shift grading, renamed fields, a
  quiz with no `source` at all reaching the preview, a 3+2 top-up reaching the full count, the
  reason line on a zero result, and the bar/title assertions in `tests/test_library.py`,
  `tests/test_learning.py`, `tests/test_document_ingestion.py` and `tests/test_office_flow.py`.

## Unreleased — quiz/flashcard reliability, short library titles, action progress bars

- **Flashcard and question generation no longer fail on a cosmetic mismatch.** Two strict checks sat
  between a fully grounded model answer and the user's preview: the quiz parser demanded
  `difficulty` to be exactly `medium`/`hard` (a route without structured-output enforcement answers
  «سخت»/«متوسط» in the prompt's language), and the chunk generator required the model's `source`
  locator to be *byte-identical* to the chunk's own label (a re-typed «بخش 1 (1/3)» or «[بخش ۱]»
  did not match «بخش ۱ (۱/۳)»). Both dropped **every** item, the single repair retry failed the
  same way, and the user saw «ساخت محتوا ناموفق بود» after a billed generation.
  `learning.parse_difficulty` now maps English and Persian labels (an unknown/missing tag is simply
  not recorded, per S5) and `learning.source_matches`/`source_key` fold digit shape, Arabic letter
  variants, brackets, quotes, slashes and spacing for comparison only; a fabricated locator is never
  stored (see the entry above, which anchors such an item to its chunk), and prefix matching is
  token-aligned so «بخش ۱» can never swallow «بخش ۱۰». A page/slide marker that the chunk itself carries
  (`learning.source_grounded_in`, e.g. «[صفحهٔ ۲]» inside a booklet) is accepted as well, because it
  can be verified in the very text the item was generated from. A chunk with no locator at all
  (legacy content) no longer rejects everything, and a numeric `source` is read instead of
  discarded.
- **Every library row now has a real name.** A booklet was indexed as
  «📖 جزوهٔ Word — GMS-000123», which is not a title. `gamas_bot/titles.py` derives a deterministic
  2–4-word title from the model's lesson title (falling back to the upload name, `جزوهٔ درسی` last),
  and that one name is used for the booklet cover, the Word filename, the Telegram caption and the
  library row — so the booklet a user opens from the list is the booklet that was named in the list.
  Free Office rows follow the same contract with their format token («گزارش فروش — Word»), and the
  library screen paints the row icon per kind (📖 booklet, 📝 transcript, 🗂 Office, 📄 document)
  instead of putting 📖 in front of every artifact.
- **Quiz, flashcards, document extraction and Free Office builds show a progress bar.** They used to
  post one plain sentence («در حال ساخت…») and then sit still for minutes. `progress.WorkProgress`
  sends a 0 % bar *before* the first model call, advances per chunk/step
  («در حال تولید سؤال‌ها · بخش ۳ از ۷»), keeps the last frame on failure with a 🛑 bar, and its edits
  are strictly decorative — a refused or flood-waited edit can never hold a model slot, fail an
  Office build or change a charge. `progress_text` gained a `headline`, and the shared
  `completed_progress_text`/`failed_progress_text` frames let a screen keep its own buttons on a
  finished bar. The file pipeline's own bar is unchanged.
- Offline regression tests: `tests/test_titles.py`, `tests/test_progress_bars.py`, plus new cases in
  `tests/test_learning.py` (retyped locator accepted, fabricated locator refused, Persian difficulty
  recorded), `tests/test_document_ingestion.py`, `tests/test_office_flow.py` and
  `tests/test_library.py`.

## Unreleased — agent system prompt

- Added `AGENTS.md` at the repository root: the model-agnostic system prompt / maintainer contract
  for AI coding agents, engineered from a full audit of v3.0.0. It records the non-negotiable
  invariants (R/B/P/S/D), billing-integrity rules, Persian/RTL rules, provider rules, coding
  conventions and the full offline test gate (T1), plus the §16 task-intake template. Documentation
  only; no runtime behavior, configuration, schema or dependency change. Linked from
  `docs/README.md`.

## Unreleased — Free Office and model-output hardening

- **Excel no longer refuses a text file with control characters.** A `.txt`, `.csv` or `.tsv` upload is
  read verbatim, so a form feed, vertical tab or stray `\x01` reached the builders unchanged. Word,
  PowerPoint and PDF normalized them, but the spreadsheet writer raised
  `openpyxl.IllegalCharacterError` for the whole document and the workbook metadata raised
  `ValueError` for such a character in the title — the user saw only the generic callback error and
  never received the file. Text and title are now normalized once at the section's single ingestion
  point (`docx_export.portable_text`, also used by the Word engine), so every format renders the same
  wording: VT/FF become a space, unstorable characters are removed. The same fix removes the
  `_x000C_`-style escape artifacts python-pptx used to show in a slide title.
- **A model table with an unnamed column is refused instead of shifting cells.** `office_ai` filtered
  the empty header out, which pulled every later cell one column left and silently discarded the cells
  that no longer fit — a sheet that no longer said what the user's text said. Such a plan now falls
  back to the deterministic sheet, which keeps every line.
- Regression tests for both: three builder-level tests and one end-to-end upload→Excel test fail
  against the previous code, as does the unnamed-column test.

## Unreleased — reliability re-audit

- **Cancellation-safe membership checks.** The one-request-per-user sharing in
  `SubscriptionService.check_membership` awaited a bare `asyncio.Future`. A
  cancelled owner never resolved it, so a concurrent interaction of the same
  user waited forever, and a cancelled waiter cancelled the future — killing the
  verification for the owner and for unrelated co-waiters. Waiters now await the
  shared request under `asyncio.shield`, and a cancelled owner releases its
  waiters with an *unknown* result (fail open, never a denial).
- **Monotonic-sentinel fixes.** `time.monotonic()` has an arbitrary epoch (the
  host's uptime), so `0.0` cannot mean "never happened". The subscription admin
  alert throttle no longer swallows the first alert — the most important one,
  "the bot is not a channel administrator" — on a freshly booted host, and the
  Free Office per-user throttle no longer refuses a new user's first export
  there. `OfficeSession.updated_at` likewise defaults to the current monotonic
  time instead of `0.0`, so a draft can never look expired on creation.
- Regression tests for both classes were added; the changed tests fail against
  the previous code and pass with these fixes.

## Unreleased — Free Office restoration

- Restored the optional **🗂 آفیس رایگان** menu and private, owner-scoped message/text-file drafts for
  `.txt`, `.md`, `.markdown`, `.csv` and `.tsv`; generated files use the existing RTL Word/Excel/
  PowerPoint/PDF builders and library references. `OFFICE_ENABLED` is `true` by default.
- Reconnected uploaded PDF/DOCX/PPTX cards to full source-grounded booklets, separate compact
  revision notes, all-text exports, original download and library navigation. Full and compact notes
  offer both Word and selectable-text PDF. PowerPoint audio remains explicit and separate.
- Added strict source-completeness fallback for planned decks, lossless source limits, formula-like
  XLSX cells stored as text, private draft expiry, bounded Office work and a library migration that
  preserves existing rows. Free Office/document-note operations do not write billing or free-use
  state; deterministic fallbacks remain available with no note provider.
- Updated README and Office/architecture/configuration/cPanel/testing/security documentation and
  added end-to-end Office flow and CSV formula-injection regression coverage.

## Unreleased — workflow re-audit

- Migration 015 records paid quiz/deck operations atomically with their debits. Artifact save
  finalizes them transactionally; cancellation, expiry and startup recover an unsaved charge once.
  Legacy pre-migration paid debits cannot be automatically matched to interrupted previews.
- Telegram callback answers now preserve specific alerts; textless narrated presentations expose an
  explicit audio-processing action; ranged downloads validate every range and complete short writes.
- Admission fingerprints cover queued/running uploads; shutdown joins admission and stops queued
  progress. Partial receipts are removed on cancellation, while a committed receipt remains attached.
- Active quiz sources survive library pruning; transient Telegram forwarding failures no longer
  permanently disable a library item. Old unavailable flags are repaired after a successful retry.
- Document splitting is linear in the extracted text size; identical concurrent uploads share an
  extraction, document parsers are concurrency-limited, extracted text is capped at 5,000,000
  characters, and progress edits cannot occupy a model slot. Learning chunk concurrency now honors
  its full configured 1–8 range. The separate document cache still has no retention/delete policy.

## Unreleased — v2 (hubs, identity, free first try, document ingestion, quality)

- **Admin hubs (R1).** The nine sibling admin buttons collapse into three hubs with new callback
  data — `adm:hub:pay`, `adm:hub:usr`, `adm:hub:sys` — each opening on its most-used view:
  pending receipts, user search, live system status. History, statistics, pending-receipt users,
  the blocked list, user cards and processing details are one tap deeper. Broadcast, support
  management, the audit trail and the leaderboard are untouched, and every old callback
  (`adm:pay`, `adm:ph`, `adm:pd`, `adm:users`, `adm:ovu:`, `adm:ovp:`, `adm:ovpay:`, `adm:ovs`) still
  routes, so a pre-deploy keyboard never dead-ends. Billing transitions, the ledger and the
  `ADMIN_IDS` check are unchanged.
- **Identity (R2).** Migration 013 adds `users.username`, `users.last_name` and `users.last_seen_at`.
  The upsert refreshes them on every interaction and every admin list or card renders
  `@username · شناسه: <id> · <name>` (em dash when Telegram reports no handle). Telegram ids are
  `INTEGER` end to end, so values above 2^53 round-trip exactly; a foreign-key integrity check
  guards user references, and a double `/start` never creates a second row.
- **Free first try (R3).** The first AI flashcard deck and the first quiz are free for every user —
  full quality, same models, claimed by one atomic `UPDATE … WHERE used = 0` before any model call.
  Failures, cancellation, preview expiry and failed delivery restore the matching claim; a unique
  token prevents stale refunds after reset. Migration 014 atomically finalizes claims with artifact
  persistence and recovers unfinished free claims at startup. Admin reset is confirmed and audited,
  and both entry screens carry a «🎁 اولین بار رایگان» badge.
- **Document ingestion (R4).** PDF, `.docx` and PowerPoint text (plus best-effort legacy `.doc`/
  `.ppt` by extension) is extracted into locator-tagged sections by the new
  `gamas_bot/doc_extract.py`, capped at `DOC_MAX_BYTES` (default 50 MB). PowerPoint extraction now
  precedes the optional original presentation/audio pipeline. Encrypted PDFs and
  image-only documents get Persian explanations, a PDF with less than ~10 characters per page is
  refused as scanned instead of storing an empty row, and an identical file is deduped by SHA-256
  and reuses the stored extraction. Documents live in the new `documents` table, appear under
  «📄 اسناد» in the library with a ~300-character preview, and offer «🃏 ساخت فلش‌کارت» /
  «❓ ساخت سؤالات» directly. New dependency: `pypdf` (pure Python, no OCR).
- **Quality and speed (R5).** Prompts now demand atomic one-fact cards, cloze-first phrasing, no
  yes/no items, four-option MCQs with misconception distractors, a short explanation, a difficulty
  tag, and a `source` locator per item; items that cannot be anchored are discarded. Generation
  parses once, chunks the text, runs `LEARNING_CHUNK_CONCURRENCY` calls (default 4, range 1–8) through
  `llm.complete_json` with `asyncio.gather`, retries an invalid chunk once, skips it after a second
  failure, retains valid partial results, dedupes near-duplicates and reports incomplete coverage instead of failing the
  run. A preview/curation screen lets the user delete unwanted items before saving, with live
  progress («در حال تولید… بخش ۳ از ۷») and three size presets: «⚡ سریع» (10/5),
  «⭐ استاندارد» (20/10), «🎯 کامل» (40/20), distributed proportionally across chunks.

- Quiz prompts mix recall, comprehension and application (~40% `medium`, ~60% `hard`) with plausible
  misconception distractors. New generation requires a valid difficulty and an exact locator from
  its input chunk; legacy stored questions without these fields remain readable.

Learning loop add-on — quizzes, leaderboard and flashcards:

- Booklets with extracted text gain an in-bot quiz (5/10/20 presets, legacy 15-question callbacks,
  `QUIZ_COST_SECONDS_PER_QUESTION`
  debited per question before generation) and AI flashcard decks (`FLASHCARD_AI_COST_SECONDS` flat
  charge, `FLASHCARD_AI_DECK_SIZE` pairs), plus manual decks, flip study mode and Word export.
  Every non-durable generation path — model failure, malformed/short output, database error or
  cancellation — refunds the full debit; unlimited users are never charged. A per-user in-flight
  guard and lock reject parallel generations before any second debit, and admission caps
  (`QUIZ_MAX_ACTIVE_HOURS`, `QUIZ_MAX_SESSIONS_PER_HOUR`, `FLASHCARD_MAX_DECKS_PER_USER`) are
  enforced before charging.
- All-time leaderboard (`🏆 برترین‌ها`) ranks users by quiz points (+10 per correct answer),
  hides Telegram identifiers and excludes banned users. Admins can gift a paid plan's credit from
  the board; the gift is audited, cooldown-limited against double taps, refused for admins/banned
  users, and announced by best-effort DM.
- Migrations 011 and 012 add display names, extracted booklet text and the quiz/flashcard tables.
  Foreign keys cascade ended quiz rows when a library item is pruned while flashcard decks survive
  with a nulled item reference; active quiz sources are protected from pruning. Manual flashcard
  conversations are disarmed at every navigation, `/start`, `/cancel` or other command.

One-tap flow, wallet and Telegram-backed library (v4):

- `/start` and every return to the user panel explain that a lecture file is the only action; audio,
  video and PowerPoint uploads now always run the full configured pipeline without an `stt`/`make`
  mode state. Transcript delivery remains before note generation, and absent note providers mean a
  transcript-only job.
- The user menu is now Library, Wallet, Support and Help (plus the separate admin entry for admins).
  The wallet combines balance, plan purchase and recent payments; the library stores Telegram
  message references for each delivered transcript/booklet and normally prunes to `LIBRARY_MAX_ITEMS`
  while preserving active quiz sources.
- Migration 010 adds the library reference table without copying generated files to the server.
  The former Free Office bot UI and queue are retired; its pure export modules remain covered by
  offline tests, and `OFFICE_ENABLED` now defaults to `false`.

Admin statistics dashboard (migration 009):

- Replaced the flat lifetime overview with a period-aware dashboard: users / processing /
  payments / system health, each with `امروز | ۷ روز | ۳۰ روز | کل` windows and a drill-down
  screen, plus an explicit «نیازمند توجه» list (pending receipts, failures by category, credit
  expiring within 7 days, database errors, provider timeouts/rate limits). Metric definitions are
  in [Admin panel](ADMIN.md).
- Submissions now record `started_at`, `error_kind` and `stt_engine`. That makes queue wait,
  processing time, failure categories and STT provider usage measurable from authoritative rows for
  a chosen window; provider usage also survives transcript retention, which previously erased the
  only record of it. Pre-upgrade rows keep NULL and are excluded from those figures rather than
  counted as zero.
- Failures are categorised once (input/download/media/stt/notes/delivery/billing/database/
  cancelled/capacity/banned/internal) at the point they are recorded, so a period report no longer
  has to guess from error text.
- New reporting indexes (submissions by `created_at`/`finished_at`/`kind`, users by `joined_at`,
  payments by `created_at`, credit ledger by `kind, expires_at`). Windows are a sargable
  `created_at >= ?` bound; the first version used `(COALESCE(?, '') = '' OR created_at >= ?)`,
  which is not index-usable, so the same 7-day report measured 50.0 ms instead of 29.5 ms at
  20 000 submissions (see [Performance](PERFORMANCE.md)).
- `python -m scripts.admin_report_bench` reproduces the dashboard timings on synthetic data.
- Fixed a stale changelog line that claimed the shipped STT order leads with Deepgram; the default
  remains Speechmatics primary (`DEFAULT_STT_PROVIDERS`).

Concurrency and capacity-wait fixes:

- Fixed single-provider capacity starvation: with only Speechmatics configured (2 in-flight
  slots against `MAX_ACTIVE_JOBS=3`), the third concurrent job waited just 15 s for a slot and
  then failed outright even though running transcriptions hold slots for minutes. The
  all-providers-full waits are now sized against attempt durations
  (`STT_SLOT_WAIT_TIMEOUT_SECONDS` 15 -> 600, `LLM_SLOT_WAIT_TIMEOUT_SECONDS` 30 -> 300); the
  zero-wait overflow pass is unchanged. `config.check` now emits a startup advisory when
  `MAX_ACTIVE_JOBS` exceeds the configured STT slots or `NOTE_CONCURRENCY` exceeds the
  configured LLM slots.
- A job arriving while every size-eligible STT provider is in a short post-error cooldown now
  waits out the shortest cooldown once (bounded by `STT_SLOT_WAIT_TIMEOUT_SECONDS` and the chain
  deadline, holding no slot) instead of failing instantly; counted as `cooldown_waits` in metrics.
- Free Office builds run on a dedicated bounded lane (`OFFICE_JOB_CONCURRENCY`, default 2), so a
  seconds-long text->document build no longer queues behind multi-hour transcription jobs.
- Capped the shared `asyncio.to_thread` executor at 8 threads: on shared hosting
  `os.cpu_count()` reports host cores and the default pool could spawn up to 32 threads.

- Added `tests/test_acceptance_mission.py`: post-merge acceptance coverage mapping the manual
  payment workflow, the two-way support tickets and the main-menu copy to one executable check
  each, including the exact Persian messages users and administrators see.

Payment, support and Persian UX fixes:

- Bounded and paginated the admin pending-payment screen so large review backlogs remain usable.
- Fixed the admin new/open ticket filter to include freshly created `awaiting_admin` tickets as well
  as reopened `open` tickets.
- Migration 008 enforces forward-only payment transitions and complete review metadata without
  rewriting production rows. Tests cover concurrent approvals through independent SQLite connections.
- Submitted orders remain readable when new card-to-card purchases are disabled; simplified the main
  menu copy and made unavailable-receipt status wording accurate.
- Manual review now requires a stored receipt and an explicit approval confirmation. Payment status,
  credit and audit writes are atomic, and migration 006 enforces one credit ledger row per payment.
- Added user/admin payment histories, order details, complete admin receipt notifications, rejection
  reasons, safe activation retries, and Telegram image-document receipt support.
- Ticket creation and its first message are atomic. Support prompts survive input mistakes, commands
  disarm stale replies, and long histories are delivered completely in Telegram-sized chunks.
- Simplified the main menu into purchase, balance, payment history and support actions with concise
  formal Persian labels and Persian currency grouping.

Documentation and maintenance pass:

- Rewrote every guide for two platforms only: Windows PowerShell (local) and Linux cPanel shared
  hosting. Removed the expired performance plan; its measured facts are now a short
  [Performance](PERFORMANCE.md) page.
- Fixed transcript retention to include `partial` jobs (booklet failed after the raw transcript was
  delivered), so finished text expires on schedule.
- `--check` now names an empty/unknown `STT_PROVIDERS` list instead of the misleading "no provider
  configured" message.
- The cPanel preflight now runs the real configuration validation, so it can no longer report
  `PASS` for a deployment the bot would refuse to start; removed a dead branch and an unnecessary
  network probe.
- Shutdown cancels and awaits background cleanup/prewarm tasks before closing the database.
- Free Office's last-document cache evicts the least recently built document, not the first key.
- Removed dead plan metadata, duplicated single-chunk note code, and stale ignore/config comments.

Performance defaults (already shipped):

- STT order leads with Deepgram Nova-3; Speechmatics stays as fallback.
- Note layer: 25 s first-byte guard, 60 s hedge, 6 concurrent map requests, structured output,
  progressive booklet.
- Audio: `application=audio, compression_level=0` Opus, `AUDIO_FORMAT_POLICY=auto` chooses WAV or
  Opus from encoder CPU plus the job's measured transfer rate.
- Large lectures compile and repair in bounded section groups instead of skipping those passes.

## 3.0.0 — 2026-10-11

- Rebuilt around a bounded job queue, SQLite billing ledger and recoverable reservations.
- Ordered STT and note-provider failover; raw transcript delivered first, then an RTL Word booklet.
- PowerPoint narration support and the free text-to-Office menu.
- cPanel cron/Passenger launch support. Single-process by design; not horizontally scalable without
  a job broker.

For older history, use `git log`.
