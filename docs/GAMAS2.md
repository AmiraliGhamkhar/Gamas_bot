# GAMAS 2.0 — implementation, deployment, migration and rollback

This document is the operator's entry point for the 2.0 release. It describes
what changed, what was deliberately *not* changed, how to deploy it, how to roll
it back, and how to verify it. English is used for engineering text; all user-
facing copy stays Persian.

## 1. What changed

| Area | Implementation | Notes |
| --- | --- | --- |
| Speaker diarization | `gamas_bot/stt.py` `ProviderSpec.supports_diarization`, `order_specs_for_diarization()`, Speechmatics `diarization=speaker` + `speaker_sensitivity`, Deepgram `diarize_model` | A diarizing provider is preferred when `STT_DIARIZE` is on, without waiting for a busy slot. A pass-through Whisper gateway is never preferred for diarization. No provider is called with an undocumented parameter. |
| Speaker-aware transcript | `gamas_bot/transcript.py` `Word.confidence/overlaps/speaker_id`, `SpeakerTurn`, `build_speaker_turns()`, `mark_overlaps()`, `TranscriptDocument.turns_to_json()` | Anonymous labels only (`گویندهٔ ۱` …). Unknown attribution stays explicitly unknown. Custom names are owner-supplied. Overlap spans and word confidences are preserved. |
| Audio preprocessing | `gamas_bot/media_worker.py` `adaptive_filters()`, `--adaptive`, `--stereo`; `gamas_bot/media.py` command builders | Mandatory when `STT_PREPROCESS=1`: every upload is cleaned by a bounded PyAV chain. The original file is never modified. Stereo is preserved only with `STT_PRESERVE_STEREO=1` **and** a genuinely 2-channel source; the WAV budget doubles so the file can never exceed a provider ceiling. |
| Deliverables | `gamas_bot/artifacts.py`, `bot_jobs._deliver_results`, `bot._show_result_set` | One completion message with independent buttons: booklet DOCX, raw transcript (chat), raw TXT download, key points, edit in Studio, quiz, flashcards. No raw transcript push, no chat preview, no intermediate draft booklet (`NOTE_PROGRESSIVE_BOOKLET=0`). |
| Central library | migration 026 `submission_artifacts`, `db_library.artifact_upsert/artifact_kinds` | One submission, many independent artifacts. Buttons are derived from the registry, so a missing artifact never produces a dead button. |
| RTL booklet | `gamas_bot/notes.py` `speakers`/`timeline`, `docx_export.build_notes_docx` appendix | A source-derived «گویندگان و زمان\u200cبندی» appendix uses real turn labels and real paragraph timings. Nothing is generated or inferred. |
| Gamas Studio | `gamas_bot/mini_app.py`, `mini_app_store.py`, `mini_app/*` | Home, Leaderboard (all/weekly/monthly), Booklet Studio (edit, revision history, restore, DOCX export), speaker rename API. All endpoints re-verify Telegram `initData`. |
| Main menu | `bot._reply_keyboard` | Persistent, non-glass `ReplyKeyboardMarkup` with a real WebView button for Studio when `MINI_APP_URL` is set. |
| Credits | `gamas_bot/billing.py` integer arithmetic, `config` tariff guard, `scripts/credit_price_preview.py` | 100 000 credits = 3 h = 10 800 s, `ceil` on seconds and `floor` on credits, exact `Fraction` math. Minute-based billing copy is gone. |
| Evaluation | `gamas_bot/diarization_eval.py`, `scripts/evaluate_diarization.py` | Offline DER, JER, WER, CER with greedy label alignment. No network, no model calls. |

### Deliberately unchanged (billing invariants)

The ledger stays **append-only integer seconds**: FIFO lots, holds, expiry,
refunds, referrals, free/one-shot grants and unlimited entitlements are intact.
Credits are a *display currency* derived from seconds, never a second ledger.
Payment snapshots keep their frozen prices. No existing applied migration (001–025)
was edited; migration 026 is additive.

## 2. Price safeguard (read before touching tariffs)

The brief's §9.2 heading says 75, but its literal value is
`CREDIT_PRICE_PER_100K_TOMAN=65`. The live catalogue sells 100 000 credits for
**60 000 تومان** (20 000/hour). A ~923× discrepancy would destroy revenue if
applied silently, so:

* `65` is preserved **literally** — it is never read as 65 000 or written over
  production prices;
* `scripts/credit_price_preview.py` prints the unit, the ratio, the untouched
  catalogue, read-only live plan rows and a **non-executed** SQL draft;
* activation requires an explicit `V2_TARIFF_ACTIVATED=1` *and* a reviewed
  plan migration; `config.check()` emits a diagnostic whenever the candidate
  differs from the live tariff while activation is off;
* the script performs **no writes** (`writes_performed: false`).

```bash
python -m scripts.credit_price_preview --candidate 65 --db /path/to/bot.sqlite3
```

## 3. Configuration

New keys (all documented in `.env.example`):

```dotenv
DIARIZATION_ENABLED=true            # alias; wins over STT_DIARIZE when set
STT_PRESERVE_STEREO=false           # opt-in; doubles PCM upload size
DEEPGRAM_DIARIZE_MODEL=              # latest | v1 | v2; empty = legacy diarize=true
NOTE_PROGRESSIVE_BOOKLET=0          # 2.0 default: no intermediate booklets
CREDIT_PRICE_PER_100K_TOMAN=65      # preview only (literal)
V2_TARIFF_ACTIVATED=0               # explicit activation gate
MINI_APP_URL=https://…              # shows the Studio button
```

`STT_PREPROCESS` remains `true`. Setting it to `0` is the only A/B escape hatch;
with it off, the old upload-as-is path returns. `STT_Diarize` alias handling is
in `config.from_env`.

## 4. Migration 026

`migrations/v3/026_gamas2_studio.sql` is additive:

```sql
ALTER TABLE transcriptions ADD COLUMN speaker_turns_json TEXT;
ALTER TABLE transcriptions ADD COLUMN diarization_attempted INTEGER ...;
ALTER TABLE transcriptions ADD COLUMN overlap_count INTEGER ...;
CREATE TABLE speaker_names (...);
CREATE TABLE diarization_reference_turns (...);
CREATE TABLE submission_artifacts (...);
CREATE INDEX idx_submission_artifacts_user ...;
CREATE TABLE booklet_revisions (...);
CREATE INDEX idx_booklet_revisions_item ...;
CREATE TABLE quiz_point_events (...);
CREATE INDEX idx_quiz_point_events_time ...;
```

`APPLICATION_TABLES` in `db_core.py` was extended to match; the migration gate
test (`tests/test_migration_schema.py`) verifies the exact table set. The bot
applies migrations itself on startup (WAL, `BEGIN IMMEDIATE`, one-record-per-file
in `schema_migrations`).

Deployment is safe in either order. The Mini App probes optional tables and
returns empty results when they are missing; the bot's
`save_transcription()` / `transcription()` probe `PRAGMA table_info` and omit
the three new columns on a pre-026 database. No `no such column` error and no
HTTP 500 can occur during a rolling deploy.

### Deploy order

1. Back up the SQLite database and `.env` (`cp bot.sqlite3 bot.sqlite3.bak`).
2. Deploy code, `pip install -e .` (no new mandatory dependencies: PyAV,
   python-docx, aiosqlite and Telethon were already required).
3. Run `python -m scripts.cpanel_preflight` (or `python -m gamas_bot.config` diagnostics)
   and resolve any `CREDIT_PRICE_PER_100K_TOMAN` diagnostic.
4. Start the bot once so migration 026 applies; confirm
   `SELECT name FROM schema_migrations WHERE name='026_gamas2_studio.sql'`.
5. Restart the Mini App WSGI worker (Passenger "Restart" or gunicorn restart).
   The app degrades gracefully if it is deployed *before* the migration.
6. Smoke test: `/health`, then open the app from Telegram and load Home.

### Rollback

Migration 026 adds columns and tables only; it rewrites no row. To roll back:

1. Stop the bot and the Mini App worker.
2. Deploy the previous revision.
3. Optionally drop the new objects (the old code never references them):

```sql
DROP TABLE IF EXISTS quiz_point_events;
DROP TABLE IF EXISTS booklet_revisions;
DROP TABLE IF EXISTS submission_artifacts;
DROP TABLE IF EXISTS diarization_reference_turns;
DROP TABLE IF EXISTS speaker_names;
DELETE FROM schema_migrations WHERE name='026_gamas2_studio.sql';
```

`ALTER TABLE ... DROP COLUMN` is unnecessary: SQLite ignores extra columns for
the old INSERT/SELECT statements, and the old `save_transcription` SQL names
its columns explicitly. Do **not** roll back by restoring a pre-migration
backup unless you are willing to lose submissions created after deployment.

Behavioural rollback without SQL: set `STT_PREPROCESS=0`,
`NOTE_PROGRESSIVE_BOOKLET=1`, `STT_PRESERVE_STEREO=0` and clear `MINI_APP_URL`.
The old flow then resumes on the same schema.

## 5. Verification

```bash
python -m pytest -q                    # full suite
python -m ruff check .
python -m mypy
python -m scripts.credit_price_preview
python -m scripts.evaluate_diarization --reference ref.json --hypothesis hyp.json
```

Reference/hypothesis JSON shape:

```json
{"text": "متن مرجع", "turns": [{"speaker_label": "استاد", "start": 0.0, "end": 4.2}]}
```

`--db PATH --from-db SUBMISSION_ID` evaluates the turns the bot actually stored.

### Acceptance checks

* A multi-speaker recording produces `گویندهٔ ۱/۲` headings and a speaker
  appendix in the DOCX; a solo lecture produces no invented speakers.
* A WAV with overlapping voices reports `overlap_count > 0`.
* A finished job sends exactly one completion message; pressing the TXT button
  downloads a real `.txt`, and quiz/flashcards buttons lead to working screens.
* Weekly/monthly leaderboards reflect `quiz_point_events` written in the same
  transaction as a correct answer; all-time still uses `users.quiz_points`.
* An edited booklet has a revision row and its DOCX export matches the edited
  text; a stranger's `initData` cannot read or edit it.
* `seconds_to_credits(10800) == 100000` exactly, and no plan price changed.

## 6. Security notes

* `initData` is verified with `HMAC-SHA256("WebAppData", bot_token)`, constant-
  time comparison, `auth_date` freshness and a future-date guard on **every**
  API request. `initDataUnsafe`, `localStorage` and client-supplied user ids are
  never trusted.
* All Mini App queries are scoped to the authenticated `user_id`; revision
  restore and speaker rename verify submission ownership inside a transaction.
* DOCX responses carry `no-store`, `nosniff` and `no-referrer`; the static shell
  keeps its CSP.
* Adaptive analysis scans at most 600 frames / 30 seconds; transcript TXT and
  DOCX exports are bounded by `MAX_BOOKLET_CHARS`; no resource ceiling was
  raised.
* Logs remain metadata-only (ids, counts, durations), never transcript text,
  names, tokens or file contents.
