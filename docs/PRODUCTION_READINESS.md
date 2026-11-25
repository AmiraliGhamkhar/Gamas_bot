# Production readiness — Phase 8 final verification

> **Historical verification record — not current documentation.** Every number
> below was measured on one earlier checkout (`fb427d5`); test counts, line
> numbers and file lists have moved since. The residual-risk list in §7 is still
> the useful part. For current guidance read [docs/](README.md) and
> [Testing](TESTING.md).

Scope: verify the integrated system produced by phases 1–7 (Study Packs, Review as a
pack view, atomic reward redemption, Explore seed bank, minimal bot UX, Mini App).
No feature was added and no product code was changed while verifying; this page
records what was executed, what it proved, and what it did not.

Branch: `arena/9a305ce5-gamas-bot` (from `fb427d5`, uncommitted at the time of
writing). Every number below was produced on this working tree.

---

## 1. Architecture summary

One Python service and one static bundle, on one origin:

```
Telegram ──► Telethon bot (long polling, one process)
              │  intake: submissions (media) / documents (PDF, DOCX, PPTX)
              │  jobs:   probe → extract → STT → notes → booklet/DOCX → delivery
              ▼
           SQLite (WAL, single writer, append-only ledger)
              │  study_packs (one per source, unique per submission/document)
              │  study_assets (references into the store that owns the bytes)
              │  quiz_sessions/questions linked to the pack that produced them
              │  explore_questions (pending → approved/rejected, seed_key identity)
              │  explore_impressions (dwell/signal/is_correct)
              │  reward_points_ledger ↔ reward_redemptions ↔ ledger (credit lots)
              ▼
           Passenger WSGI app  (same host)
              ├── /api/v1/*   initData-authenticated JSON
              └── /app/*      the built Mini App (Vite SPA, RTL, Persian)
```

* **Ownership is the access rule.** Packs, assets, sessions and questions are read
  through `user_id` joins; a foreign id resolves to 404, never to data.
* **Two accountings, one bridge.** Reward points and processing credit are separate
  ledgers. `reward_redemptions` is the single row that ties a points debit to a real
  credit lot, and it is written in the same transaction as both.
* **Nothing is duplicated by design.** Unique partial indexes guard the pack/source
  link, the asset reference, the publication request, the redemption key and the
  seed key, so a retry lands on the existing row.
* **The client never computes money.** Balances, prices, awards and plan windows are
  read from the server; the Mini App only formats them (Persian digits, Jalali dates).

## 2. Feature checklist

| Phase | Delivered | Evidence |
| --- | --- | --- |
| 1 — Study Pack pipeline | one pack per accepted source, assets by reference, terminal statuses | `tests/test_study_pack_pipeline.py` (10), migration `039`, probe §5.3 |
| 2 — Review as a pack view | questions grouped and titled by their pack, legacy groups kept honest | `tests/test_review_study_packs.py` (15), `040`, probe §5.3 |
| 3 — Atomic redemption | points debit + real credit lot + idempotent retry in one transaction | `tests/test_reward_redemption.py` (23), `041`, probe §5.1 |
| 4 — Explore seed bank | 200 curated questions / 12 topics, stable `seed_key`, idempotent seeding | `tests/test_explore_seed_bank.py` (22), `042`, §5.4 |
| 6 — Minimal bot UX | `/start` = one message + one Mini App button, no reply keyboard, no flashcards | `tests/test_bot_ux.py` (27) |
| 7 — Mini App | exactly four tabs (مرور، کتابخانه، یک‌سؤال، حساب من), token design system, real APIs | `miniapp/src/test/app.test.tsx` (6), build, §5.2 |

Journey steps 1–9 (open bot → send file → pack created → processing → transcript →
outputs stored → questions attached → ready → delivery actions) are covered by the
pipeline tests plus the seam probe; steps 10–18 are covered by the API journey probe
(§5.1) — including the credit ledger and the pending → approved publication gate.

## 3. Files changed

Modified (44): `.env.example`, `DATABASE_DESIGN.md`, `README.md`, `docs/CHANGELOG.md`,
`docs/DEPLOY_MINIAPP.md`, `pyproject.toml`, `scripts/validate_explore_bank.py`,
`gamas_bot/{artifacts,bot,bot_documents,bot_jobs,bot_learning,bot_ui,config,db,db_billing,db_core,db_learning,db_library,db_study_packs,explore_seed,texts,webapp_api}.py`,
`miniapp/{index.html,package.json,package-lock.json,vite.config.ts,tailwind.config.js}`,
`miniapp/src/{App.tsx,api.ts,index.css,main.tsx}`,
`tests/{test_acceptance_mission,test_api_v1,test_booklet_wallet_ux,test_bot,test_diarization,test_explore_and_rewards,test_gamas2,test_library,test_office_flow,test_support,test_texts,test_webapp_api}.py`.

Added (24): `gamas_bot/{db_review,db_rewards,explore_bank,rewards}.py`;
`migrations/v3/039_study_pack_pipeline.sql`, `040_review_pack_links.sql`,
`041_reward_redemptions.sql`, `042_explore_seed_keys.sql`;
`tests/{test_study_pack_pipeline,test_review_study_packs,test_reward_redemption,test_explore_seed_bank,test_bot_ux}.py`;
`miniapp/src/{lib/format.ts,lib/telegram.ts,lib/useAsync.ts,ui/Icon.tsx,ui/primitives.tsx,ui/states.tsx,features/account/AccountTab.tsx,features/library/LibraryTab.tsx,features/review/ReviewTab.tsx,features/explore/ExploreTab.tsx,test/app.test.tsx,test/setup.ts}`,
`miniapp/vitest.config.ts`.

No `requirements.txt` change; `pyproject.toml` only adds the new modules to the mypy
strict list.

## 4. Database migrations

| Migration | Content | Risk |
| --- | --- | --- |
| `039_study_pack_pipeline` | 7 nullable/defaulted columns on `study_packs`, 2 on `study_assets`, `quiz_sessions.pack_id`; 2 unique partial indexes + 1 index | additive |
| `040_review_pack_links` | `explore_questions.source_question_id` + unique partial index; 2 indexes; one backfill that only fills `quiz_sessions.pack_id` where a real library/pack relation exists | additive + safe UPDATE |
| `041_reward_redemptions` | new `reward_redemptions` table, unique `(user_id, idempotency_key)`, unique `reward_points_ledger.ref_id` for `redemption:%` | additive |
| `042_explore_seed_keys` | `explore_questions.seed_key` + unique partial index | additive |

Applied by the existing runner on connect (`schema_migrations`), v3 only, no
destructive statement, and `_without_applied_columns` skips a re-added column.

Upgrade evidence (§5.5): a base-commit database (migration 038) with users, a
library item, a quiz session/questions/answers, a legacy pack, an approved payment,
a live credit lot, a referral and four approved Explore rows was opened by the
current code. All four migrations applied, every legacy row survived byte-for-byte,
new columns stayed `NULL`/`''`, the unlinked session was **not** guessed into a pack
(Review reports it as a legacy group), a second open changed nothing, and
`PRAGMA integrity_check` / `foreign_key_check` came back clean.

## 5. Test execution report

### 5.1 Integration probes (run for this phase, not committed)

| Probe | Result |
| --- | --- |
| API journey: account → library → pack detail → download → review groups/questions → answer → share → admin approve → approved-only feed → 5 correct answers → 10 points each → redeem 50 points → 300 s ledger credit → replay → refusal | **64/64 checks passed** |
| Data integrity inside that journey: one pack, two assets, no orphan asset/question, one redemption, one debit, one credit lot, credit ledger = 300 s, one publication row | all passed |
| Security inside that journey: unsigned request 401, foreign pack 404, foreign download 404, foreign question unanswerable (404), moderation queue 403 for non-admins, pending question absent from the public feed | all passed |
| Bot → API seam: run the **real** media job, then read the same database through `/api/v1` | **7 passed** (1 new seam test + 6 pipeline tests) — the library lists the job's pack, the PDF/transcript downloads return the stored bytes, `question_count` and Review pick up the questions the quiz stage linked to that pack, another account sees nothing |

### 5.2 Frontend

| Check | Result |
| --- | --- |
| `npm test` (vitest + jsdom, 6 tests) | **6 passed** — four tabs exactly, account figures from the API payloads, library → pack → review handoff, review verdict, explore award + no refetch on tab switch, browser-without-Telegram screen, retryable 500 state |
| `npx tsc --noEmit -p tsconfig.json` | clean (strict) |
| `npm run build` | pass — `index.js` 194.61 kB (61.87 kB gzip), `index.css` 20.95 kB (5.22 kB gzip), Vazirmatn woff2 bundled locally |

### 5.3 Backend

| Suite | Result |
| --- | --- |
| Full suite `pytest -q -p no:randomly` | **2167 passed, 20 failed, 517 subtests passed** (2187 collected, 4 m 30 s) |
| Same suite at the base commit | 2035 collected; the two failing modules fail there too: **20 failed / 44 passed** |
| New modules only | `test_study_pack_pipeline` + `test_review_study_packs` + `test_reward_redemption` + `test_explore_seed_bank` + `test_bot_ux` + `test_api_v1` → **154 passed** |
| Migration / API / initData | `test_migration_schema` + `test_api_v1` + `test_webapp_api` → **24 passed** |
| Admin, booklet, providers, billing, library | `test_admin_hubs`…`test_library` (10 files) → **265 passed, 70 subtests** |

The 20 failures are the same 20 at the base commit and belong to
`tests/test_free_uses.py` (14) and `tests/test_reaudit_workflows.py` (6): they assert
the chat-side flashcard/quiz-deck flow that phase 6 deliberately retired (they call
`gamas_bot.bot.complete_json`, expect "free try restored" messages and chat refunds).
They are stale tests, not product regressions; one of the 21 counted in phase 6 is
timing-flaky (it passed in this run).

### 5.4 Seed bank and SQLite

* Bank: 200 questions, 12 topics, unique keys. First CLI run inserted 200, second run
  inserted **0** (`skipped (already seeded): 200`); the database holds 200 rows, 0
  duplicate keys, 12 topics.
* `PRAGMA integrity_check` = `ok` and `PRAGMA foreign_key_check` = no rows on both the
  upgraded legacy database and a freshly seeded one; all seven guard indexes exist.

### 5.5 Upgrade / compatibility probe

See §4 — **25/25 checks passed**. It also exercised the legacy account through the
API: credit 18 000 s served unchanged, the legacy approved payment still resolves as
the current plan, the payment history keeps its recorded price, the referral still
counts, the legacy pack is listed, its questions are served as a legacy group with
their stored answers, a legacy question can still be offered to Explore, and the
legacy approved public questions still feed Explore while private pack content never
does.

### 5.6 Lint and typing

* `ruff check .` → **11 findings**, all pre-existing and all in test files (unused
  imports / one redefinition / three trailing spaces). Production modules are clean,
  including every module added in phases 1–7.
* `mypy gamas_bot` → **6 errors**, all in `webapp_api.py` handlers untouched by this
  work (row-index and `int(body.get(...))` patterns); the base commit reports **20**
  in the same file, so this work reduces the number.

### 5.7 Not executed / blocked by the environment

* Real Telegram: no Bot API call and no WebApp `initData` from a real client. The bot
  is exercised through the project's fake client; `initData` is signed with a test
  token. **The Telegram round trip is unverified here.**
* Real STT / note providers (Speechmatics, Deepgram, AvalAI, OpenAI-compatible):
  never contacted; every provider test uses scripted HTTP fakes. Fallback logic is
  covered by unit tests only.
* Real payments, real channel subscription checks, real cPanel/Passenger runtime,
  real mobile WebViews (iOS/Android Telegram), and any browser-driven E2E: not
  available in this sandbox. The Mini App preview shows its own "open inside
  Telegram" state because there is no `initData`.

## 6. Security review

| Area | Finding |
| --- | --- |
| initData validation | HMAC-SHA256 over the sorted payload with the bot token (`WebAppData` key), constant-time compare, `auth_date` freshness (24 h), `user.id` required; missing/expired/tampered → 401 `. Passed. |
| API authorization | one choke point: everything under `/api/` is authenticated before dispatch; only `/api/health` and `/api/v1/health` are public; the browser never holds credentials — it forwards `initData` only. |
| Ownership | packs, assets, review groups/questions/answers/share and moderation all filter by the authenticated `user_id`; foreign ids answer 404 (probe §5.1). |
| Downloads | `/api/v1/library/<pack>/download/<asset>` resolves the asset inside the owned pack, re-checks the owning row of every reference (artifact/transcription/document blob), refuses path escapes, and always sends `Cache-Control: private, no-store`. |
| Answer-key leakage | `correct_index`/`explanation` are only included once the user has answered; unanswered questions never expose the key. |
| Publication boundary | a user's question enters Explore as `pending`; only the admin moderation route can move it to `approved`; the feed reads `status='approved'` only. |
| Reward integrity | one transaction writes redemption + points debit + credit lot; negative balances raise and roll back; idempotency key is unique per user; the API never reports a grant the ledger does not hold. |
| Credit ledger | append-only; the Mini App balance is `available_seconds()` from the ledger, not a stored counter; the cache self-heals and logs a warning if it disagrees. |
| SQL | every user value is bound as a parameter. The only f-string SQL builds `?` placeholders and table names from a closed, validated map (`db_reporting`) or `PRAGMA table_info` from internal migration data. |
| Error handling | JSON body capped at 1 MiB; malformed JSON → 400; missing fields → 400; unknown ids → 404; redemption failure → 500 with a message that says nothing was deducted/granted. Others fall through to a generic 500 with no internal detail. |
| Client hygiene | no secret, token or authorization decision in `miniapp/`; no `innerHTML`; no third-party script beyond Telegram's own SDK. |

Residual risks are listed in §7; none is a new regression introduced by phases 1–7.

## 7. Remaining known issues

1. ~~**20 stale tests** (`test_free_uses.py`, `test_reaudit_workflows.py`) still
   describe the retired chat flashcard flow.~~ **Resolved:** both files were
   rewritten to the quiz-only contract and the retired `deck` free-use kind and
   columns were removed (migration 043), so the full suite is green again
   (`pytest` 2187 passed, `unittest discover` 2057 OK).
2. **Banned users are not refused at the API level.** The Mini App shows a blocked
   screen and the bot refuses them, but `/api/v1` still answers their own data and
   accepts Explore answers. Not new; a one-place fix if it matters.
3. **No rate limiting / abuse ceiling** on `/api/v1` (Explore answers award points).
   The write path is idempotent per question, but request volume is unbounded.
4. **Redemption idempotency depends on the client key.** Two deliberate requests
   without a key each spend points (by design, tested); the Mini App always sends one.
5. **iOS WebView downloads**: assets are fetched as blobs so the `initData` header can
   be sent; some iOS clients open a preview instead of saving the file.
6. **Explore pagination is round-based** (a cursor over seen questions); there is no
   infinite feed, and reaching the end needs "سؤالهای تازه".
7. **Static `/app` assets carry no cache headers** (correct, but every visit re-reads
   the bundle).
8. **SQLite remains a single-writer database**: fine for the current single bot
   process, not a multi-instance deployment.

## 8. Deployment requirements

Unchanged hosting (cPanel + Passenger + Python 3.11), no configuration change was made:

1. `pip install -r requirements.txt` — unchanged.
2. `cd miniapp && npm ci && npm run build` — produces `miniapp/dist` served at `/app`.
3. Environment: `TELEGRAM_BOT_TOKEN` (required, also verifies `initData`), optional
   `WEBAPP_URL=https://<host>` (bot's single menu button → `{origin}/app`) and
   `BOT_USERNAME=Gamas_bot` (Mini App deep links). Both have safe defaults.
4. Restart the application so migrations `039`–`042` apply (automatic on connect).
   Back up the database first: `sqlite3 gamas_bot.db ".backup gamas_bot.bak.$(date +%F)"`.
5. Seed Explore (idempotent, optional on a fresh host because the bot seeds at start):
   `python scripts/validate_explore_bank.py --db gamas_bot.db`.
6. BotFather → Menu Button → `https://<host>/app/`; verify `/api/v1/health`.
7. Run the smoke checklist in `docs/DEPLOY_MINIAPP.md` §6 on a real client.

## 9. Recommended next steps

1. Retire or rewrite the 20 stale tests (highest value for CI signal).
2. Decide and, if wanted, implement API-level ban enforcement next to the existing
   flag (single middleware point).
3. Add a light per-user rate limit on `/api/v1/explore/*` and `/api/v1/rewards/redeem`.
4. Stage-deploy and run the Telegram smoke checklist on Android, iOS and Desktop;
   verify purchase deep links, asset downloads and the four tabs on real devices.
5. Add cache headers (`immutable` hashed assets) for `/app/assets/*` in the WSGI
   static handler.
6. Keep the integration probes from §5.1/§5.5 as committed tests (they found the
   contract drift risk early and cost seconds to run).

## Verdict

The evidence supports **conditional production readiness**: data integrity, the
credit/reward invariants, authorization and the migration path are verified on real
code paths, and nothing in phases 1–7 regresses the existing suite. It is **not**
unconditional, because the Telegram round trip, the external STT/LLM providers and
the hosting runtime were never exercised here, and 20 stale tests keep the suite red.
Deploy to staging, run the documented smoke checklist, and retire those tests before
deploying to production.
