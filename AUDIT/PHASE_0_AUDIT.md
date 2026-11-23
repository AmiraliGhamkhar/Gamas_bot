# Phase 0 — Repository audit and implementation plan

**Date:** 2026-11-23 (UTC)
**Branch:** `arena/9a305ce5-gamas-bot` (base `fb427d57bc3420f9c07769a445b481b6cb724033`, status `active`)
**Scope:** source, migrations, tests and Mini App in this checkout. No `.env`, Telegram
session, production database, or live Telegram/STT/LLM endpoint was accessed.

This document is the Phase 0 deliverable. It records what already exists, what is
broken, and the minimal plan for Phases 1–8. **No production code was changed in
Phase 0.**

---

## 1. Executive summary

The repository already contains a substantial and *working* implementation of the
target architecture: migration `037_study_packs.sql`, `038_review_and_explore.sql`,
`db_study_packs.py`, `study_storage.py`, `telegram_auth.py`, a stdlib WSGI API
(`webapp_api.py`), a deterministic recommender (`explore_recommender.py`), the Explore
seed module (`explore_seed.py`) and a four-tab React Mini App (`miniapp/src/App.tsx`).

The gaps are **integration and correctness**, not greenfield work:

1. The Study Pack tables exist but **no production code path ever creates a pack**.
   `grep -rn "study_packs" gamas_bot/` matches only `db.py`, `db_study_packs.py` and
   `webapp_api.py`. The Mini App Library therefore shows an empty list forever.
2. Review is still **session-based**, and the question list is truncated at 50 rows
   across *all* sessions.
3. Reward redemption **deducts points and reports `credit_seconds_granted` without
   writing any credit to the billing ledger** (a fake success).
4. The "200 unique" Explore bank is **10 questions repeated 20 times** with a
   `(پرسش N)` suffix, and nothing seeds it in production.
5. The Explore publication path has no dedup, no idempotency and no rate limiting.
6. The Mini App Account tab reports **`credit_seconds: 0` for every user** (the API
   reads a column that does not exist), and `/api/v1/me/subscription` raises
   `AttributeError` (`Settings.required_channel` does not exist).

The branch also starts **red**: 36 failing tests, `ruff check .` fails with 11 errors,
and the CI "Migration integrity" step fails because `APPLICATION_TABLES` was never
updated for migration 038.

---

## 2. Verified baseline (commands actually executed)

| Check | Command | Result |
|---|---|---|
| Full suite | `.venv/bin/python -m pytest -q` | **36 failed, 1999 passed, 519 subtests passed** in 233 s |
| Lint | `.venv/bin/ruff check .` | **11 errors** (all in `tests/`, 9 auto-fixable) |
| Types (declared list) | `.venv/bin/python -m mypy` | Pass — 80 files, 0 issues |
| Types (new modules) | `mypy webapp_api.py explore_recommender.py explore_seed.py db_study_packs.py study_storage.py telegram_auth.py` | **20 errors** in `webapp_api.py` (modules are absent from `[tool.mypy].files`) |
| Requirements | `.venv/bin/python -m scripts.sync_requirements --check` | up to date |
| Dependencies | `.venv/bin/python -m pip check` | no broken requirements |
| Byte-compile | `.venv/bin/python -m compileall -q …` | pass |
| Media worker | `.venv/bin/python -m gamas_bot.media_worker check` | `{"av": "18.1.1", "ppt2pptx": "0.4.2"}` |
| CI migration integrity | CI workflow block, fresh temp DB | **FAILS**: `{'explore_questions', 'explore_impressions', 'user_topic_preferences'}` are in the schema but not in `APPLICATION_TABLES` |

Environment used for the audit: Python 3.11.2 in `.venv` (git-ignored). Python 3.12/3.13
were not available in this sandbox; the CI matrix is unverified here.

### 2.1 Failure breakdown (36)

| Group | Tests | Cause | Owner phase |
|---|---|---|---|
| Stale flashcard-era DB/API assertions | 11 in `test_free_uses.py`, 5 in `test_reaudit_workflows.py` | Tests call `quiz_create_session(user, item, title, cards)` (old signature) and use free-use kind `deck`; the flashcards feature is removed | Phase 1 (baseline) |
| Stale pre-Mini-App bot menu copy | 13 in `test_bot.py`, `test_diarization.py`, `test_support.py`, `test_texts.py` | Tests expect the old inline menu (`کتابخانه/اعتبار/بیشتر/پشتیبانی`) and the old multi-purpose `main_screen`; the bot now shows a Mini App launch button and a single-purpose submission prompt | Phase 6 |
| Stale mission-menu assertion | 1 in `test_acceptance_mission.py` | Same as above | Phase 6 |
| Real defect | 1 in `test_migration_schema.py` | `APPLICATION_TABLES` missing three 038 tables | Phase 1 |
| Real defect | 1 in `test_api_v1.py` | `Settings.required_channel` does not exist → `AttributeError` | Phase 1 |

The 25 "stale test" failures must be resolved by deciding which side is authoritative.
Evidence for the decision: `AGENTS.md`, `ARCHITECTURE.md` and the mission brief all state
that the Mini App is the primary interface and the bot is a minimal file-in/results-out
workflow. The current code matches the brief; the tests encode the superseded design.
They will be updated **to assert the new contract** (not deleted), each with a comment
stating the superseded expectation.

---

## 3. Current architecture (as implemented)

```
Telegram ── intake ─► jobs (Job/Submission) ─► STT/transcript ─► notes/booklet
                                                              └► PDF/DOCX/HTML in
                                                                 submission_binary_artifacts
                                                              └► library_items (chat mirrors)
                                                              └► quiz_sessions/quiz_questions
                                                                 (bot_learning.py, 20 questions)

WSGI (webapp_api.py, stdlib) ── initData HMAC ──► Database (SQLite, WAL)
   /api/me, /api/v1/me/*        account
   /api/packs, /api/v1/library  study_packs (empty in production — nothing writes them)
   /api/v1/review/*             quiz_sessions (session-based, LIMIT 50)
   /api/v1/explore/*            explore_questions (never seeded) + explore_impressions
   /api/v1/rewards/*            reward_points_ledger (redemption grants nothing)
   /app/*                       miniapp/dist (built on deploy; dist/ is git-ignored)
```

### Existing, reusable pieces (do not replace)

- `telegram_auth.validate_init_data` — correct HMAC-SHA256 check with `auth_date` freshness.
- `study_storage.save_private_asset` / `resolve_private_asset` — traversal-safe private FS,
  0600, content-hash filenames (natural dedup).
- `explore_recommender` — deterministic weighting, smoothing, decay, topic-diversity cap,
  `classify_engagement` returning the exact 3/2/1/0 contract.
- `db_billing` — append-only lot-aware ledger; `ledger` inserts are picked up by the
  `balance_dirty` triggers and flushed inside the same `BEGIN IMMEDIATE` transaction.
- `db_library.binary_artifact*` — PDF/DOCX/HTML bytes already persisted per submission.
- `db_learning.quiz_*` — 20-question sessions with exactly-once answering
  (`UNIQUE INDEX idx_quiz_answer_once`).
- Migration runner: numbered, additive, `schema_migrations` table.

---

## 4. Findings register

Severity: **S1** blocks the product or corrupts data · **S2** user-visible defect ·
**S3** hygiene.

| ID | Sev | Finding | Evidence | Phase |
|---|---|---|---|---|
| F-01 | S1 | No production code creates `study_packs`/`study_assets`; Mini App Library is permanently empty | `grep study_packs gamas_bot/` → `db.py`, `db_study_packs.py`, `webapp_api.py` only | 1 |
| F-02 | S1 | `APPLICATION_TABLES` missing `explore_questions`, `explore_impressions`, `user_topic_preferences`; CI migration-integrity gate fails | `gamas_bot/db_core.py:32-70`; verified with the CI block | 1 |
| F-03 | S1 | `/api/me` and `/api/v1/me/credits` always return `credit_seconds: 0`; `user_identity()` has no `credit_seconds` key | `webapp_api.py:179,204`; `db_users.py:585-609`; reproduced: 3600 s available → `{"credit_seconds": 0}` | 1 |
| F-04 | S2 | `/api/v1/me/subscription` raises `AttributeError: 'Settings' object has no attribute 'required_channel'` (500) | `webapp_api.py:197`; `config.py` has `subscription_channel` / `subscription_channel_url()` | 1 |
| F-05 | S1 | Reward redemption deducts points and returns `credit_seconds_granted` but never writes a `ledger` credit row — the user pays and receives nothing | `webapp_api.py:700-737` | 3 |
| F-06 | S2 | Re-answering an already answered Review question raises an unhandled `sqlite3.IntegrityError` (WSGI 500) | `webapp_api.py:405-425` vs `UNIQUE INDEX idx_quiz_answer_once`; reproduced | 2 |
| F-07 | S2 | Review questions are `ORDER BY question_id DESC LIMIT 50` across all sessions, so any pack older than ~2 sessions appears empty/half-empty | `webapp_api.py:369-375` | 2 |
| F-08 | S2 | Review is grouped by `quiz_sessions` (`بسته آزمون #id`) with no Study Pack identity or title | `webapp_api.py:345-360`; `miniapp/src/App.tsx:600-640` | 2 |
| F-09 | S2 | The 200-question Explore bank is 10 base questions repeated 20×; `scripts/validate_explore_bank.py` passes only because the suffix makes the text unique | `explore_seed.py:10-53`; verified: 200 rows / **10 distinct topics**, `(پرسش 11)`… | 4 |
| F-10 | S1 | Nothing seeds `explore_questions` in production (no startup hook, no migration, no admin command) → Explore feed is empty on a fresh deploy | `grep seed_explore_questions` → module + tests only | 4 |
| F-11 | S2 | Publication to Explore has no dedup, no idempotency, no rate limit and no link back to the source question/pack; repeated taps create unlimited `pending` rows | `webapp_api.py:432-485` | 5 |
| F-12 | S2 | `theme_*`, `badge_*`, `discount_*` rewards take points and grant nothing (no entitlement storage exists) — a fake success | `webapp_api.py:_REWARD_CATALOG`, `:700-737` | 3 |
| F-13 | S3 | `classify_engagement` exists but the engagement endpoint re-implements the 2/1/0 thresholds inline (duplicated business logic) | `webapp_api.py:581` vs `explore_recommender.py:27-35` | 3 |
| F-14 | S3 | `/api/me` exposes the removed `deck` free-use flag to the UI | `webapp_api.py:183`; `db_users.free_use_state` | 6 |
| F-15 | S3 | `POST /api/packs` lets any client mint unlimited empty packs with arbitrary titles (no submission link, no cap) | `webapp_api.py:246-270` | 1 |
| F-16 | S3 | Bot `_user_menu` shows only the Mini App button + admin entry; 25 tests still assert the superseded menu | `bot.py:641-654`; failure table §2.1 | 6 |
| F-17 | S3 | Explore tab label is `کاوش`, the brief specifies `اکسپلور`; Account tab has no subscription-purchase/pricing section | `miniapp/src/App.tsx:903-908`; Account tab has channel text only | 7 |
| F-18 | S3 | Five new modules are outside the mypy gate and contain 20 type errors | `pyproject.toml [tool.mypy].files`; measured | 8 |
| F-19 | S3 | New users log a spurious `balance cache mismatch` and `balance_repair` metric on every connect (no `user_balances` row is created) | `db_billing.py:189-199`; observed in a clean DB | 8 |
| F-20 | S3 | `BOOKLET`/`PPTX`-only helpers: `db_study_packs.backfill_from_library` is untested, non-idempotent (creates a pack per row on every run) and never called | `db_study_packs.py:213-243` | 1 |

No SQL injection, no missing `initData` validation, no cross-user asset read, and no
private-content leak into Explore were found: every API query is parameterized and scoped
by the authenticated `user_id`, and the feed endpoint selects only public columns.

---

## 5. Phase plan (each phase = one prompt, one reviewable diff)

### Phase 1 — Study Pack integration with the real processing pipeline

**Migration `039_study_pack_pipeline.sql`** (additive, nullable, safe default for old rows):
- `ALTER TABLE study_packs ADD COLUMN submission_id INTEGER` (nullable).
- `CREATE UNIQUE INDEX idx_study_packs_submission ON study_packs(user_id, submission_id) WHERE submission_id IS NOT NULL` — makes pack creation idempotent per submission.
- `ALTER TABLE quiz_sessions ADD COLUMN pack_id INTEGER` (nullable) + index — used by Phase 2.
- Add the three 038 tables to `APPLICATION_TABLES` (F-02).

**Repository (`db_study_packs.py`)**
- `study_pack_ensure_for_submission(user_id, submission_id, title, source_type)` — idempotent, one `BEGIN IMMEDIATE`, returns `(pack_id, created)`.
- `study_pack_finalize(user_id, pack_id, status)` and `study_pack_attach_asset(...)` reusing
  `study_storage.save_private_asset` (sha-based dedup already exists in `study_asset_add`).
- Replace the non-idempotent `backfill_from_library` with an idempotent, dry-run-first,
  admin-invoked backfill (F-20).

**Pipeline hooks (additive, no change to STT/booklet logic)**
- Create the pack at submission admission with `status='processing'`.
- Finalize in `bot_jobs._deliver_results` (`bot_jobs.py:1618`) — the single delivery choke
  point — from data that is *already durable*: transcript text, `submission_binary_artifacts`
  (PDF/DOCX/review HTML). Audio/video originals live only in the per-job workdir and are
  **not** copied (cPanel disk quota); this is recorded as `original_source` metadata, not bytes.
- Document path (`bot_documents.py:823`) and quiz creation (`bot_learning.py:750`) attach
  their `pack_id`.
- Failures are logged with `exc_info=True` and mark the pack `failed`; they never abort a
  delivery that already succeeded, and start-up recovery closes orphaned `processing` packs.

**Also in Phase 1 (baseline restoration):** F-03 (real credit balance), F-04
(`subscription_channel`), F-02, F-15 (cap/validate pack creation), and the 16 stale
flashcard-era DB test assertions (rewritten to the quiz-only contract).

**Acceptance criteria**
- One submission ⇒ exactly one pack; replaying delivery creates no duplicate pack/asset.
- Assets: transcript + every available binary artifact, with correct mime/size/sha.
- Failure path: pack ends `failed`, not `processing`, and logs one warning.
- `/api/me` returns the true `available_seconds`.
- `pytest tests/test_study_packs.py tests/test_webapp_api.py tests/test_api_v1.py tests/test_free_uses.py tests/test_reaudit_workflows.py tests/test_migration_schema.py` green; full suite no worse than baseline.

### Phase 2 — Study Pack-based Review
`/api/v1/review/packs` returns packs with their question counts and titles; per-pack
question listing replaces the global `LIMIT 50`; double answers become an idempotent
`200` (F-06) — the existing `INSERT OR IGNORE`-style guard plus a "already answered"
flag, never a 500. Ownership stays `user_id`-scoped. Backfill `quiz_sessions.pack_id`
from `library_items.submission_id`. Frontend groups by pack title. Tests: grouping,
ownership, double answer, legacy sessions without a pack.

### Phase 3 — Transactional reward redemption + credits
One `BEGIN IMMEDIATE` that (a) enforces per-user limits, (b) checks the balance,
(c) inserts the negative ledger row keyed by an idempotency ref (`UNIQUE(user_id, ref_id)`),
(d) inserts the `ledger` credit row for credit rewards, so `balance_dirty` refreshes
`user_balances` on commit. Response reports what was actually written. Idempotent replay
returns the original outcome. Non-implementable catalog items (F-12) are disabled rather
than faked — **needs the user's decision** (§7). `classify_engagement` becomes the only
threshold implementation (F-13). Tests: double-spend, replay, insufficient points,
limit reached, credit balance delta, rollback on failure.

### Phase 4 — Unique Explore seed bank
200 genuinely distinct Persian MCQs across ≥8 topics with 4 options and an explanation
each; deterministic ordering; seeded by an additive migration (or an idempotent startup
step) guarded by a unique index on normalized question text; `scripts/validate_explore_bank.py`
tightened to reject suffix-duplicates. Tests: exactly 200 distinct texts, valid shape,
idempotent re-seed, feed never exposes counts/indices.

### Phase 5 — Safe Explore publication
`explore_questions` gains `source_question_id`/`source_hash` (nullable) with a unique
partial index for dedup; publication validates ownership, normalizes and hashes the text,
caps pending submissions per user, writes an audit entry, returns `duplicate` instead of
a second row, and keeps the public payload limited to `topic/question_text/options/
explanation` (never pack title, transcript, or user identity). Admin approve/reject stays
`admin_ids`-gated and gains an audit record.

### Phase 6 — Bot UX simplification
Keep the minimal workflow; fix the 14 stale menu/text tests to the current contract; drop
the removed `deck` flag from `/api/me` (F-14); confirm no prices appear in the submission
flow (verified: only `texts.py:341` refers the user to the purchase section).

### Phase 7 — Mini App UX/completion
Four tabs only, `اکسپلور` label (F-17), Account subscription-purchase/pricing section fed
by the existing plan catalog (prices unchanged), pack-centric Review, Explore polish
(skeletons, empty states, no counts anywhere), referral link from server-provided data,
`npm run build` gate, and a small navigation/unit test for the tab set (dev-only
dependency decision below).

### Phase 8 — Integration and production readiness
Full gate (ruff, mypy incl. F-18 modules, pytest, unittest discovery, migration integrity,
requirement sync, media worker, frontend build), docs (`docs/CHANGELOG.md`,
`docs/DEPLOY_MINIAPP.md`, `API_CONTRACT.md`, `IMPLEMENTATION_PLAN.md`), final diff review.

---

## 6. Data-integrity commitments for all phases

- Only additive migrations; no `DROP`, no destructive `ALTER`, no rewriting applied SQL.
- New columns are nullable or carry defaults that leave historical rows valid.
- Pack, asset, impression and reward writes are idempotent by unique index, not by
  trusting the caller.
- `is_unlimited`, banned, and subscription gates keep their current fail-closed semantics.
- No change to prices, plans, STT providers, fallback logic or booklet generation.

## 7. Decisions needed from the maintainer

1. **Reward catalog items without an implementation** (`theme_*`, `badge_*`,
   `discount_*`): disable them, or build a `reward_entitlements` table now? The brief
   forbids fake success and unnecessary scope; my recommendation is **disable**.
2. **Original source file as a Study Pack asset**: copying every upload doubles private
   disk usage on cPanel. Recommendation: persist transcript + PDF + DOCX + review HTML,
   and for documents only, keep the extracted text; do **not** archive audio/video bytes.
3. **Frontend tests**: the repo has no JS test runner. Recommendation: adopt `vitest`
   (dev-only) in Phase 7 for the four-tab navigation; otherwise the frontend gate stays
   `tsc -b && vite build`.

## 8. Known risks / limitations of this audit

- Python 3.12/3.13 and the CI matrix were not executed here (sandbox has 3.11.2).
- No live Telegram/Passenger deployment was exercised; `/app` static serving and
  `initData` were validated only through the WSGI test harness.
- cPanel disk/quota behaviour for private assets is assumed from `AGENTS.md`, not measured.
- `AUDIT/REPORT.md` describes an older checkout; its claims were not reused as evidence.
