# GAMAS 2.0 — Final Engineering Report

Branch: `arena/b8b5c39a-gamas-bot` · Date: 2026-11-10 · Repository: `AmiraliGhamkhar/Gamas_bot`

## Executive summary

GAMAS 2.0 is implemented as an **additive, backward-compatible change set** on
the existing v3 codebase. No module was rewritten wholesale, no applied
migration was edited, no dependency was added, and no resource ceiling was
raised. The integer-second FIFO billing ledger, holds, expiry, refunds,
referrals, free/one-shot grants and unlimited entitlements are untouched.

The release delivers capability-aware speaker diarization with an anonymous,
speaker-aware transcript model; mandatory adaptive PyAV preprocessing with
optional stereo preservation; a single completion message with independent
download buttons; a professional RTL booklet appendix derived from real turns
and timings; Gamas Studio (Home / weekly-monthly Leaderboard / Booklet Studio)
with revision history, DOCX export and verified `initData` authentication; a
persistent non-glass reply keyboard with a real WebView launch button; exact
integer credit arithmetic with a guarded tariff preview; a central submission
artifact registry; and an offline DER/JER/WER/CER evaluator.

**Verification:** `1693 passed, 244 subtests passed` (full `pytest -q`),
`ruff check .` → `All checks passed!`, `mypy` → `Success: no issues found in 66
source files`. Baseline before the change was 1640 passed + 16 OCR failures
caused solely by a missing optional `curl-cffi` extra; after installing
`[ocr]` the baseline was green. Three operator scripts were also smoke-tested
against a read-only temporary database (see §4).

---

## 1. Requirement-by-requirement delivery

| # | Requirement | Delivered | Key files |
| --- | --- | --- | --- |
| 1 | Speaker diarization, capability-aware STT routing, speaker-aware transcript, Persian labels, DER/JER/WER/CER | `ProviderSpec.supports_diarization` + stable preference ordering; Speechmatics `diarization=speaker`/`speaker_sensitivity`; Deepgram `diarize_model` (opt-in) or legacy `diarize=true`; `Word.confidence/overlaps/speaker_id`, `SpeakerTurn`, `build_speaker_turns`, overlap spans, custom-name API; offline evaluator | `gamas_bot/stt.py`, `transcript.py`, `diarization_eval.py`, `scripts/evaluate_diarization.py`, `docs/STT_PROVIDERS.md` |
| 2 | Mandatory adaptive audio preprocessing via PyAV, preserving original, stereo, bounded resources | Every upload re-encoded when `STT_PREPROCESS=1`; bounded 600-frame/30-second analysis selects a subset of the documented chain; `aformat` sink pins s16; stereo only for genuinely 2-channel sources, with doubled WAV budget; worker reports `layout` and `filters` | `gamas_bot/media_worker.py`, `media.py`, `bot_jobs.py::_prepare_audio`, `docs/MEDIA.md` |
| 3 | Stop auto raw transcript / preview / intermediate booklets; one completion message with independent buttons | Raw/preview pushes were already dead; `NOTE_PROGRESSIVE_BOOKLET` now defaults to `0`; completion screen offers booklet DOCX, raw transcript (chat), raw TXT download, key points, edit, quiz, flashcards — each with a working handler and derived from the artifact registry | `gamas_bot/config.py`, `artifacts.py`, `bot_jobs.py::_deliver_results`, `bot.py::_show_result_set`, `texts.py` |
| 4 | Professional RTL booklet improvements | `StructuredNotes.speakers/timeline` (source-derived only) rendered as a «گویندگان و زمان\u200cبندی» appendix by `build_notes_docx`; custom names win over Persian defaults; Markdown/payload round-trip | `gamas_bot/notes.py`, `docx_export.py`, `bot_jobs.py::_booklet_context` |
| 5 | Telegram Mini App "Gamas Studio" — Home / Leaderboard / Booklet Studio, validated `initData`, revision history, DOCX export, lightweight WSGI/Python | Three-tab no-build frontend; `/api/home`, `/api/leaderboard` (all/weekly/monthly), `/api/booklets`, `/api/booklet`, `/api/booklet/export`, `/api/revisions`, `/api/revision`, `/api/speakers`; HMAC `WebAppData` + constant-time compare + freshness on every request; all queries owner-scoped; bounded revision history | `gamas_bot/mini_app.py`, `mini_app_store.py`, `scripts/mini_app_wsgi.py`, `mini_app/*`, `docs/MINI_APP.md` |
| 6 | Persistent non-glass ReplyKeyboardMarkup main menu | Existing `persistent=True`, `resize=True` keyboard retained and extended with a `ButtonTypeSimpleWebView` Studio button shown only when `MINI_APP_URL` is configured | `gamas_bot/bot.py::_reply_keyboard`, `texts.py` |
| 7 | Credits replacing minute terminology; 100 000 credits = 3 h = 10 800 s; integer ceil; centralized tariff | Exact `Fraction` arithmetic (`CREDITS_PER_UNIT`, `SECONDS_PER_UNIT`), `ceil`/`floor`; all wallet/plan copy already credit-based; central catalogue unchanged; price safeguard with literal 65, read-only preview and `V2_TARIFF_ACTIVATED` gate | `gamas_bot/billing.py`, `config.py`, `scripts/credit_price_preview.py`, `.env.example`, `docs/BILLING.md` |
| 8 | Library as central submission with independent artifacts; preserve all existing features | Migration 026 `submission_artifacts` with `UNIQUE(submission_id, kind)`; `artifact_upsert/artifact_kinds/artifact_list`; buttons derived from registry; registration, membership, STT failover, quizzes, referrals, payments, tickets, admin, bans and subscriptions untouched | `migrations/v3/026_gamas2_studio.sql`, `gamas_bot/db_library.py`, `db_core.py` |
| — | Full tests, deployment / migration / rollback docs, env example, final report | `1690` tests green, ruff/mypy clean, new `tests/test_gamas2.py`; `docs/GAMAS2.md` with deploy order and rollback SQL; updated 5 existing docs; `.env.example` extended and consistency-tested | `tests/`, `docs/`, `.env.example`, this report |

## 2. Design decisions and constraints honoured

* **No fabricated identities or counts.** `resolve_speakers` keeps only labels
  passing a share/word floor, caps at 12, and resolves a solo lecture to one
  voice. Unknown attribution stays `speaker_unknown` with `display_name=None`.
  Custom names are only ever owner-supplied through `/api/speakers` or
  `db.set_speaker_name`.
* **No undocumented provider parameters.** Speechmatics uses the documented
  `diarization=speaker` + `speaker_sensitivity`; Deepgram's `diarize_model`
  is opt-in and the default keeps the legacy `diarize=true` call byte-identical.
* **Accuracy over visuals.** The adaptive chain can only *remove* stages from
  the documented chain; filter output is pinned back to s16 so the encoder and
  inter-clip silence frames agree; sample counts remain exact, so billing still
  charges the uploaded duration. Diarization preference never waits on a busy
  slot — the capacity pool still overflows immediately.
* **Bounded resources.** Adaptive analysis ≤ 600 frames / 30 s; overlap
  detection is linear in word count; timeline ≤ 40 markers; revisions ≤ 20 per
  booklet; DOCX/TXT exports bounded by `MAX_BOOKLET_CHARS` / `MAX_REQUEST_BYTES`.
  `MEDIA_WORKER_CONCURRENCY`, `MAX_FILE_SIZE`, timeouts and queues were not
  increased.
* **No dead buttons.** The Studio button exists only with a configured URL;
  quiz/flashcards buttons only with the caller's own booklet item; artifact
  buttons only for registered kinds; revision/restore only for owned rows.
* **No private data via Mini App.** Home returns counts, credits and points —
  never phone numbers, tokens, other users' names beyond the public leaderboard
  `first_name`, or transcript text outside the owner's own booklet endpoint.
* **Billing invariants.** The ledger is still integer seconds. Credits are a
  derived display unit; no second ledger was introduced. Payment snapshots stay
  frozen. The 65-Toman candidate is a **literal, read-only preview** and is
  never interpreted as 65 000 or written over production prices.
* **Hook Model, no manipulation.** The Home screen shows one number, one
  progress signal and two real actions; no streak pressure, fake scarcity or
  dark patterns were added. `https://t.me/passchi_bot` was used only as a
  product-direction reference; no branding or assets were copied.

## 3. Data and schema

`migrations/v3/026_gamas2_studio.sql` is purely additive: three
`ALTER TABLE transcriptions ADD COLUMN` statements (turns JSON, attempted flag,
overlap count) and five new tables (`speaker_names`,
`diarization_reference_turns`, `submission_artifacts`, `booklet_revisions`,
`quiz_point_events`) with three indexes. `APPLICATION_TABLES` was extended and
the migration gate test verifies the exact set. Weekly/monthly leaderboards read
`quiz_point_events`, written in the **same transaction** as a correct quiz
answer; all-time still reads `users.quiz_points`, so no historic total is
recomputed or mutated.

The Mini App degrades gracefully when deployed before the migration: optional
tables are probed and missing ones yield empty results rather than HTTP 500.
The bot is also compatible in the other direction: `save_transcription()` and
`transcription()` probe `PRAGMA table_info(transcriptions)` and reference the
three 026 columns only when they exist, so a pre-026 database never receives a
`no such column` error. A regression test drops the columns and proves the
write/read pair still works. The results hub now reconciles the central
artifact registry with the actual transcript/notes/booklet rows, so a partial
registry write cannot hide a real download, a stale registration cannot
advertise a dead one, and a consumed library snapshot still keeps the booklet
button. Windowed ranks exclude banned users, matching the visible leaderboard.

## 4. Verification

```text
python -m pytest -q            1693 passed, 244 subtests passed (199.60s)
python -m ruff check .         All checks passed!
python -m mypy                 Success: no issues found in 66 source files
python -m scripts.credit_price_preview --candidate 65 --db /tmp/smoke.sqlite3
  → ratio_live_over_proposed 923.08, major_discrepancy true, activated false,
    writes_performed false, live plan price unchanged at 60000
python -m scripts.evaluate_diarization --reference ref.json \
    --db /tmp/smoke.sqlite3 --from-db 12
  → der 0.0, jer 0.0, wer 0.0, cer 0.0, label_map {"0":"استاد","1":"دانشجو"}
WSGI /health                    200 OK {"service":"gamas-mini-app","ok":true,...}
WSGI /api/home (signed initData) 200 OK with credits 100000 for 10800 seconds
```

New test modules: `tests/test_gamas2.py` (35 tests, 15 subtests) covering exact
credit arithmetic, the price guard, speaker turns/custom names/overlaps,
capability ordering, adaptive chain & stereo transcode, evaluator metrics,
delivery markup, all new Mini App endpoints incl. DOCX bytes and auth failures,
migration 026 tables and the central artifact registry. Existing tests that
asserted the old passthrough/progressive behaviour were updated to the 2.0
contract rather than deleted.

### Google Drive references

All three references in the brief were reachable through the connected Drive
app and inspected:

* `تعرفه_و_رسیدگی_به_اسناد_بستری.pdf` — Persian hospital tariff / document-
  handling guide (13 pages; text extracted, truncated at 20 000 chars).
* `متنی کامل کلاس.docx` — timestamped class transcript with `دانشجو` / `استاد`
  speaker labels (181 040 bytes; text extracted, truncated at 30 000 chars).
  Its structure directly informed the turn/timestamp appendix and the honest
  `دانشجو`/`استاد`-style custom-name support.
* `متنی کامل کلاس.pdf` — the same class material as a 17-page PDF
  (379 522 bytes; text extracted, truncated at 20 000 chars).

Extraction was truncated per connector limits; no claim of a full-text review is
made. No branding, assets or wording were copied into the product.

## 5. Deployment, rollback and known limitations

Full procedure in `docs/GAMAS2.md` §4–§6. In short: back up the DB and `.env`,
deploy code, run preflight/config checks, start the bot once (applies 026),
restart the Mini App worker, smoke-test `/health` and the three tabs. Rollback
is a table drop plus `schema_migrations` row deletion; no row is rewritten, so
restoring a pre-migration backup is unnecessary and would lose later jobs.
Behavioural rollback also works by configuration alone.

Known, deliberate limitations:

* The **finished** booklet DOCX is still delivered as a file (as the library
  storage anchor) and the completion message then offers it as a button; only
  *intermediate* drafts and raw/preview pushes were removed. This keeps the
  existing Telegram-hosted library design and the full test suite intact.
* `STT_PRESERVE_STEREO` is opt-in: doubling every PCM upload would be an
  unmeasured resource increase. It activates only for a probed 2-channel source.
* `DEEPGRAM_DIARIZE_MODEL` defaults to empty (legacy v1 diarizer) so existing
  deployments see no provider-behaviour change; `latest`/`v2` is documented and
  opt-in.
* Offline DER/JER use a greedy one-to-one label map and 100 ms frames —
  deterministic and dependency-free, but not a NIST-style Hungarian/scoring
  toolkit. Overlapping speech is represented as frame sets, not per-speaker
  acoustic models.
* The Studio editor is text-based; speaker renaming is exposed through
  `/api/speakers` and the owner-scoped DB method rather than a fourth UI tab.
* The tariff candidate is **not activated**. `V2_TARIFF_ACTIVATED=1` plus a
  reviewed plan migration remains an explicit operator decision; the diagnosis
  is emitted at startup until the discrepancy is resolved or acknowledged.

## 6. Deliverables index

| Deliverable | Path |
| --- | --- |
| This report | `GAMAS2_ENGINEERING_REPORT.md` |
| Deployment / migration / rollback / acceptance | `docs/GAMAS2.md` |
| Updated operator docs | `docs/MINI_APP.md`, `STT_PROVIDERS.md`, `MEDIA.md`, `BILLING.md`, `TESTING.md` |
| Migration | `migrations/v3/026_gamas2_studio.sql` |
| Environment template | `.env.example` |
| Evaluator CLI | `scripts/evaluate_diarization.py` |
| Tariff preview CLI | `scripts/credit_price_preview.py` |
| New tests | `tests/test_gamas2.py` (plus targeted updates in 6 existing test modules) |
| Mini App frontend | `mini_app/index.html`, `app.js`, `app.css` |
| Mini App backend | `gamas_bot/mini_app.py`, `mini_app_store.py`, `scripts/mini_app_wsgi.py` |
