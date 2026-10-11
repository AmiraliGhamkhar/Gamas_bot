# Engineering audit — Gamas Bot

Audited revision: `c9cee9b` (`main`, merge of PR #43) plus the changes listed in
§6 of this document. Every claim below was checked against the checked-out
source, the migrations applied to a real temporary database, or a recorded
command run in this workspace. Where a claim could not be tested here, §9 says so
instead of implying a result.

Scope: Telegram intake, job queue, media extraction, STT, provider routing and
billing, transcript structuring, QA/repair/compilation, DOCX generation,
delivery, the 12 migrations, and the test/benchmark tooling.

---

## 1. Verified architecture and end-to-end call graph

Verified by reading the call sites, not the documentation. Line numbers are from
the audited revision.

```
Telegram update
  bot.py handler (@client.on(events.NewMessage) …)          gamas_bot/bot.py
    └─ _accept_media / _accept_presentation                  bot.py:3067 / :3117
         ├─ db.create_submission(...)          → audio_submissions (status='pending')
         ├─ JobProgress.create(...)                            progress.py
         └─ _enqueue(QueuedJob)                                bot.py:651
              ├─ queue full → _reject_job(...)  → status='failed', user told (bot.py:665)
              └─ worker: _worker(index)        → _job_semaphore(max_concurrent_jobs)
                   └─ _process_submission(...)                 bot.py:3422
                        ├─ status='processing'; tempfile.mkdtemp(prefix='deck-<id>-')
                        ├─ download_media() → media.py probe/extract (child process)
                        │     media.py → media_worker.py (PyAV, hard timeout)
                        ├─ _reserve_submission_usage(...)       bot.py:3027
                        │     └─ db.reserve_usage → usage_reservations + usage_ledger
                        ├─ stt.transcribe(...)                  stt.py:1398
                        │     ├─ db.stt_quota_reserve (budget row, BEGIN IMMEDIATE)
                        │     ├─ _attempt_provider → _attempt_with_retries
                        │     │      (per-credential rotation, quarantine, cooldown)
                        │     └─ db.stt_quota_reservation_finalize(commit=success)
                        ├─ textnorm.normalize / units.extract
                        ├─ structure_transcript(...)  | structure_presentation(...)
                        │     structuring.py
                        │       ├─ split_transcript (lossless)
                        │       ├─ _lecture_context (outline, optional, non-fatal)
                        │       ├─ per part: _notes_for_part → _structured_notes_for
                        │       │      ├─ _structure_chunk → job.generate (AI router)
                        │       │      │      ai/routing.py → ai/adapters.py → HTTP
                        │       │      │      + ai/usage.py record → ai_usage_records
                        │       │      ├─ parse_structured_notes (scrub + validate)
                        │       │      └─ one bounded JSON-repair pass
                        │       ├─ _repair_notes_if_needed (QA-driven, optional)
                        │       └─ _compile_final (editorial pass, acceptance-gated)
                        ├─ qa.run_note_qa (deterministic gates)
                        ├─ db.finalize_usage(measured seconds)   bot.py:3326
                        ├─ _deliver_result_documents(...)         bot.py:3700
                        │     ├─ build_notes_docx → docx_export.py
                        │     ├─ send .docx, then .txt transcript (independent guards)
                        │     └─ on build failure: in-chat text (documented fallback)
                        └─ finally: rmtree(workdir); release reservation on any failure
```

Cross-cutting:

* `config.py` is the only environment reader; `Settings.from_env` validates every
  value and `tests/test_config_consistency.py` pins `.env.example` ↔ `Settings`.
* `database.py` owns schema + migrations, the ledger, credential storage and the
  AI/STT platform tables; one connection, an `asyncio.Lock`, `BEGIN IMMEDIATE`
  for writes, WAL, `foreign_keys=ON`, `busy_timeout=5000`.
* `provider_credentials.py` (Fernet) and `provider_health.py` supply credentials
  and cached probes; `admin_ai.py`, `admin_stt.py` render SQLite only.

Duplicate/legacy surfaces confirmed and left alone deliberately:

* `stt.py` keeps a *legacy* provider path (`speechmatics`, `deepgram`,
  `openai_compatible`) next to the native `stt_platform` adapters; the router
  treats the legacy three as an allowlisted compatibility tier
  (`stt_platform/router.py`), so pre-existing deployments keep working. It is the
  documented migration path, not dead code.
* `structuring.py` keeps a router-less path (`_structure_chunk_once` through
  `current_provider_credentials()`) for installs without the AI platform, and the
  byte-compatible `SYSTEM_PROMPT` for single-part lectures.

---

## 2. Database schema and migration inventory

Inventory produced by opening a **fresh temporary database** and reading
`sqlite_master` / `PRAGMA table_info` / `PRAGMA foreign_key_list`:
**30 tables, 42 indexes** (4 `UNIQUE`), all 12 migrations applied in filename
order and recorded in `schema_migrations`.

| Migration | Adds |
| --- | --- |
| `001_initial.sql` | `users`, `audio_submissions`, `transcriptions`, `admin_broadcasts` |
| `002_presentations.sql` | `presentation_clips`, submission source/slide columns |
| `003_billing_and_credentials.sql` | `plans`, `payment_requests`, `entitlements`, `usage_reservations`, `usage_ledger`, `provider_credentials`, `admin_audit_log`, `schema_migrations` |
| `004_production_hardening.sql` | free-plan code rename to `free_1h`, receipt provenance (`receipt_file_id`, `admin_note`), health/credential indexes |
| `005_special_users_and_plan_panel.sql` | `users.is_unlimited` + provenance, custom plans |
| `006_ai_provider_platform.sql` | `ai_provider_registry`, `ai_provider_settings`, `ai_models`, `ai_provider_routes`, `ai_usage_records`, `ai_usage_daily`, `ai_quota_snapshots`, `ai_events` |
| `007_provider_billing_attestations.sql` | credential billing/attestation columns, `free_endpoint` |
| `008_provider_entitlement_and_pass_policy.sql` | entitlement attestation, per-provider pass flags (outline/repair/compile), budgets |
| `009_provider_registry.sql` | registry metadata columns (`metering_unit`, `experimental_only`, aliases…) |
| `010_provider_policy_overrides.sql` | provider policy override columns |
| `011_alibaba_service_scope.sql` | Alibaba service scope metadata |
| `012_stt_platform.sql` | `stt_provider_registry`, `stt_provider_settings`, `stt_routes`, `stt_model_registry`, `stt_quota_snapshots`, `stt_quota_budgets`, `stt_quota_reservations`, `stt_usage_records`, `stt_provider_events`, `provider_credentials.metadata_json` |

Identity / relationships (verified from `PRAGMA foreign_key_list`):

* `users(telegram_id)` → `audio_submissions.user_id` (cascade), and every
  accounting table (`entitlements`, `usage_reservations`, `usage_ledger`,
  `payment_requests`) cascades from the user.
* `audio_submissions` 1↔1 `usage_reservations` (`submission_id` is **UNIQUE**) and
  1↔1 `transcriptions`; `presentation_clips` cascades from the submission.
* `usage_ledger` references `usage_reservations`, `entitlements`, the submission
  (SET NULL on delete) and the user; a **UNIQUE** index
  (`idx_usage_ledger_one_grant_per_entitlement`) makes a second `grant` for one
  entitlement impossible.
* `entitlements.payment_id` is UNIQUE (one grant per approved payment) and the
  partial unique index `idx_entitlement_free_once_per_user` allows exactly one
  lifetime free row per user.
* `payment_requests` has a partial unique index
  (`idx_payment_one_open_request_per_user`) so a user cannot open two live
  purchase requests.
* STT tables hold no transcript text: `stt_usage_records`/`stt_provider_events`
  store provider, model, credential id, durations, latency, status and an error
  *category* only (see the table comment in `012_stt_platform.sql`).

### Credit and quota transitions (traced end to end)

`reserve_usage` (BEGIN IMMEDIATE) → `usage_reservations(status='reserved')` plus
one `usage_ledger` `reserve` row per entitlement consumed, with the entitlement
update guarded by `WHERE id=? AND status='active' AND remaining_seconds>=?` and a
`rowcount != 1` RuntimeError. `finalize_usage` writes `consume` rows for the
measured seconds and `release` rows for the unused remainder, then flips the
reservation to `consumed`/`partially_consumed`/`released`.
`_release_reservation_in_transaction` returns early unless the row is still
`reserved` and updates `WHERE id=? AND status='reserved'`, so a release can
happen exactly once even when two paths race.

**Verified invariants** (adversarial probe on a temporary database):

| Invariant | Result |
| --- | --- |
| Same reservation cannot be consumed twice | `finalize_usage` #2 returns `False`; balance unchanged |
| Release after finalize refunds nothing | returns `0`; balance unchanged |
| Double release refunds nothing | returns `0`; balance unchanged |
| Restart releases an interrupted job exactly once | 3440 s → 3540 s after `open()`; submission marked `failed`; no second refund |
| A replay of `reserve_usage` cannot double-charge | `submission_id` is UNIQUE; a replay with different seconds answers `ok=False, already_finalized` |
| Ledger is append-only and consistent | `reserve/consume/release` rows sum to the balance change |

### Migration testing

* **Fresh install**: all 12 migrations apply, `schema_migrations` equals the file
  list, and a second and third `open()` are no-ops (no duplicate-column failure).
* **Upgrade path**: a database created with only `001`–`005` (files copied into a
  temporary `MIGRATIONS_DIR`), then populated with a user, a finalized job, an
  approved payment and a receipt, and finally reopened against the full set:
  the remaining migrations applied in order and **every pre-existing row
  survived** (user count, remaining balance, payment history and receipt
  metadata unchanged).
* Interrupted `ALTER TABLE` statements are tolerated (the runner ignores
  “duplicate column name”), which is what makes 004/012 safe on a database
  restored from a pre-migration backup.

No migration was modified, and no new migration is required by this audit.

---

## 3. Provider integration and routing matrix

Read from `ai/registry.py` (15 providers) and each adapter in `ai/adapters.py`.

| Provider | Protocol / auth | Endpoint | Free class | Notes verified in code |
| --- | --- | --- | --- | --- |
| `nara` | OpenAI-compatible / bearer | `https://router.bynara.id/v1` | free_plan | default candidate #1; Free-plan intersection required for eligibility |
| `openrouter` | OpenAI-compatible / bearer | `https://openrouter.ai/api/v1` | free_plan | default candidate #2 |
| `gemini` | native `generateContent` / `x-goog-api-key` | `…/v1beta` | free_plan | `responseMimeType: application/json`, `maxOutputTokens` |
| `groq` | OpenAI-compatible / bearer | `https://api.groq.com/openai/v1` | account_unverified | generation blocked until an admin attests the account entitlement |
| `mistral`, `sambanova`, `zai` | OpenAI-compatible / bearer | vendor `/v1` | free_plan | `generation_allowed_in_free_only=False` for mistral and zai |
| `nvidia`, `cohere`, `cerebras` | OpenAI-compatible / bearer | vendor `/v1` | trial_only | never routed by default |
| `cloudflare` | OpenAI-compatible / bearer | per-credential (account-scoped; registry `base_url` is `None`) | free_plan | URL must come from credential metadata |
| `huggingface` | OpenAI-compatible / bearer | `https://router.huggingface.co/v1` | paid_only | |
| `alibaba` | OpenAI-compatible / bearer | `https://dashscope.aliyuncs.com/compatible-mode/v1` | region_restricted | free only in the listed region scopes (migration 011) |
| `anthropic` | native Messages / `x-api-key` | `https://api.anthropic.com/v1` | paid_only | `temperature` omitted for Opus-4.7+; never `top_p` with `temperature` |
| `openai_compatible` | OpenAI-compatible / bearer | `https://api.openai.com/v1` | paid_only | opt-in `response_format` only |

Structured-output strategy is explicit per attempt (`ai/adapters.py`):
`strict_schema`, `json_schema`, `json_object`, `gemini_mime`, `gemini_schema`,
`prompt` — “registered” never implies “JSON mode supported”, and the strategy that
was actually sent is recorded on the usage row.

Routing requirements re-verified:

* NaraRouter and OpenRouter remain the preferred candidates
  (`scripts/validate_provider_platform.py`: `nara-route-first` PASS), and a saved
  administrator route is never overwritten by seeding
  (`tests/test_route_seed_upgrade.py`, `tests/test_route_failover_eligibility.py`).
* Free-only and billing attestations are enforced before a request is built:
  `groq-blocked-without-attestation`, `paid-fallback-closed`, `paid-legs-stay-last`
  and `trial-providers-never-routed` all PASS in the platform validator (25/25).
* Provider, model, credential identity and usage rows reflect the attempt that
  actually ran: `requests-were-recorded`, `failed-attempt-is-accounted` and
  `groq-429-observed` are PASS.
* Panels do not spend money: `tests/test_admin_panel_presentation.py` fails the
  build if a redraw opens a provider session
  (`test_page_rendering_never_touches_a_provider`) or if the health panel probes a
  provider, and a failed catalog sync keeps the last valid rows by design.
* Health checks and catalog discovery are separate actions from generation
  permission; opening or redrawing any AI/STT screen reads SQLite only.

Contract coverage: `tests/test_provider_contracts.py`,
`tests/test_provider_adapters.py`, `tests/test_provider_robustness.py`,
`tests/test_provider_platform_gaps.py` and `tests/test_ai_schema_compat.py` pin
URL, headers, authentication placement, payload shape, token-budget field,
response parsing, usage extraction and error translation per provider — all with
mocked HTTP. No live key was used anywhere in this audit.

STT providers (registered vs. usable) are kept honest by the registry itself:
`_AWS_TRANSCRIBE` is `enabled=False` with `persian_batch=False`, and the native
adapter list (`stt_platform/adapters.py`) covers only the protocols it
implements. The STT-specific findings are F4 and F6 in §4.

---

## 4. Findings

Ranked by severity. Each entry names the file, the function, the mechanism and a
reproduction that was actually executed.

### F1 — CRITICAL (content/security): an echoed Google API key reached the student’s notes

* **Where**: `gamas_bot/structuring.py`, `_KEY_PREFIX` (the scrubber behind every
  model-supplied field through `_bounded_text`).
* **Mechanism**: the pattern required a separator after the vendor prefix
  (`[_-]`). Google’s keys have none (`AIza` + 35 characters), so an echoed key
  passed through unchanged and was printed in the summary, a definition term and
  the delivered `.docx`. Groq (`gsk_…`) and NVIDIA (`nvapi-…`) keys were missed
  for a related reason: the alternation anchored the short prefixes with `\b`, and
  those tokens do not start there.
* **Reproduction**: `parse_structured_notes` with
  `"summary": "… کلید AIzaSyC9dKq3WvBn7XyZ1lMnOpQrStUvWxYz123"` returned the key
  verbatim in `notes.summary`; `count_backend_artifacts` reported `0`, so the leak
  was not even counted.
* **Fix**: two key shapes are matched — separator-carrying prefixes (`sk`,
  `sk-nry`, `sk-ant`, `ghp`, `gho`, `github_pat`, `hf`, `gsk`, `nvapi`, `xox…`)
  and Google’s run-on form `AIza[A-Za-z0-9_-]{12,}`. Regression tests:
  `tests/test_backend_artifact_scrub.py`.

### F2 — HIGH (content): the “as an AI assistant” disclaimer was never removed in the form models write it

* **Where**: `gamas_bot/structuring.py`, `_AI_DISCLAIMER`.
* **Mechanism**: the pattern was case-sensitive and required a lowercase `as`
  before a capitalised `AI`. A model that opens its answer with
  `As an AI assistant, …` therefore matched nothing and the sentence survived
  into the title, summary, learning objectives, review questions and the Word
  booklet. The all-lowercase form failed too, because `AI` had to be uppercase.
* **Reproduction**: `_AI_DISCLAIMER.search("As an AI assistant")` returned `None`;
  `scrub_backend_artifacts` left the sentence untouched and
  `count_backend_artifacts` scored it `0`.
* **Fix**: case-insensitive, and covering the shapes a disclaimer actually takes —
  a role word after `AI` (`assistant`, `language model`, `model`, `chatbot`,
  `bot`), a first-person continuation (`As an AI, I cannot…`), `I’m/I am an AI …`,
  the Persian equivalents, and the line-initial label form (`As an AI assistant:`).
  Deliberately **not** removed: imperative prompt echoes (`Be an AI assistant:`,
  `Act as an AI assistant:`) and mid-sentence academic mentions (`a chatbot acts
  as an AI assistant to the user`, `an LLM, as a language model, predicts the next
  token`) — those can be legitimate course content. Regression tests:
  `tests/test_backend_artifact_scrub.py::ConservativeFilteringTests`.

### F3 — HIGH (job execution/content): output-token exhaustion threw away the whole part

* **Where**: `gamas_bot/structuring.py`, `_provider_response` (three truncation
  raises) and the per-part loop of `structure_transcript` /
  `structure_presentation`.
* **Mechanism**: a provider that stopped because the answer hit
  `max_output_tokens` raised a generic `StructuringError`, indistinguishable from
  “the provider is down”. The pipeline therefore lost **all** notes for that part
  of the lecture even though the source text was fine and only needed to be asked
  for in smaller pieces. The JSON-repair retry could not help — it re-sends the
  same document with a *longer* prompt, i.e. the identical output-budget problem.
* **Reproduction**: a provider double that raises truncation for a whole part and
  answers normally for each half produced one failed job before the fix
  (`tests/test_output_truncation_recovery.py`).
* **Fix**: a distinct `NoteOutputTruncated` (subclass of `StructuringError`, so
  existing callers are unaffected) for `finish_reason=length`,
  `finishReason=MAX_TOKENS` and `stop_reason=max_tokens`. The affected part is
  re-requested as **two smaller parts cut from the same text**
  (`split_document_for_truncation`: paragraph → line → sentence → space boundary,
  nearest to the middle) and merged additively (`_notes_for_part`), bounded by
  `MAX_TRUNCATION_SPLITS = 4` splits per job. When the budget is spent or the text
  cannot be split usefully the condition is reported and the documented transcript
  fallback delivers the lecture. Only truncation is split; a 502, a 429, an auth
  failure or an invalid request propagates unchanged. Regression tests:
  `tests/test_output_truncation_recovery.py` (13 tests).

### F4 — MEDIUM (configuration): `GROQ_STT_MAX_UPLOAD_BYTES` was parsed, validated and never read

* **Where**: `gamas_bot/config.py` (`Settings.stt_provider_max_uploads`, validated
  in `from_env`, documented in `.env.example`) versus
  `gamas_bot/stt.py::_native_provider`, which returned
  `STT_PROVIDER_REGISTRY[slug].max_file_size` unconditionally.
* **Mechanism**: the operator’s explicit cap never reached the routing gate
  (`stt_platform/router.py::evaluate_candidate`, `facts.max_upload`), so a file the
  account cannot accept was still selected, uploaded in full and rejected by the
  provider — losing the attempt and the wall-clock time. The value could even
  abort startup while doing nothing.
* **Reproduction**: with `stt_provider_max_uploads=(("groq", 10_000_000),)` a
  12 MB file was still routed to Groq (registry limit 25 MB); after the fix the
  refusal names the configured cap.
* **Fix**: `_native_max_upload(slug, settings)` gives a configured override
  precedence over the published registry value and falls back to it when unset.
  Regression test:
  `tests/test_stt_platform_routing.py::test_configured_provider_upload_cap_overrides_the_registry_default`.

### F5 — MEDIUM (verification honesty): the offline renderer could report “no layout warnings” for a page of tofu

* **Where**: `scripts/render_docx_pages.py` and
  `scripts/validate_docx.py::_render_status`.
* **Mechanism**: the renderer draws with whatever face it finds (DejaVu here).
  DejaVu has no Arabic coverage, so every Persian word is a box — yet the report
  said “no layout warnings”, which an operator reads as “the Persian renders
  correctly”. That is the failure mode §8 warns about: a check implying more than
  it performed.
* **Reproduction**: the rendered pages of the sample booklet are boxes for all
  Persian text while the report read “no layout warnings (no blank page, no
  overflow, no orphan heading)”.
* **Fix**: `face_renders_persian()` probes the selected face (renders one Arabic
  letter and compares it with an undefined codepoint, because Pillow does not
  report missing glyphs) and the result is reported as `capability_notes`,
  separately from document defects, by both the renderer and
  `validate_docx --render`. A capability note never fails a document. Regression
  tests: `tests/test_page_renderer.py::RendererCapabilityTests`.

### F6 — LOW (documentation/configuration): inert AWS Transcribe settings

`stt_aws_s3_bucket`, `stt_aws_access_key_id`, `stt_aws_secret_access_key` and
`stt_aws_session_token` are read from `AWS_TRANSCRIBE_*` and shown in
`.env.example`, but nothing consumes them. This is consistent with the registry
entry (`stt_platform/registry.py`: `enabled=False`, `persian_batch=False`, batch
Persian documented as unsupported), i.e. the provider is honestly disabled rather
than broken. Reported, not changed: removing the parsing would break deployments
that set the variables, and real support (AWS batch through S3) is a feature, not
an audit repair. Residual risk: an operator may believe configuring these enables
the provider.

### F7 — LOW (content, residual): residual diagnostic phrasing survives a single-marker line

A line with exactly one plumbing marker keeps its non-marker text, e.g.
`سرویس gemini خطای HTTP 429 داد` becomes `سرویس gemini خطای ••• داد`. The policy
is deliberately conservative (a networking lecture legitimately contains “HTTP
404”), so one marker is not treated as a diagnostic cluster — deleting the whole
line on one marker would delete course content. Reported as a residual risk
rather than “fixed” by a broader filter. Nothing else was observed to leak:
`scripts/validate_provider_platform.py` re-verifies `diagnostic-cluster-removed`,
`request-id-removed`, `secret-token-removed`, `no-secret-in-events` and
`no-transcript-in-events` (all PASS).

### F8 — LOW (observability): a leg that exhausted its retries left no failure event

* **Where**: `gamas_bot/ai/routing.py` — `ProviderRouter.execute`, both the
  HTTP-failure path and the transport-failure path.
* **Mechanism**: an attempt that *would* be retried emitted
  `note_request_retry`; an attempt that had run out of budget (or whose
  `Retry-After` exceeded the cap) emitted **nothing**. The durable usage row still
  recorded `result=failure`, but the event stream — the surface the admin log
  filters by event name — contained no terminal failure at all.
* **Reproduction**: with both legs answering 502 the events were
  `note_request_retry`(nara) → `note_request_fallback` → `note_request_started`
  (groq) → `note_request_retry`(groq) → `provider_quota_blocked`; not a single
  `note_request_failed` existed for a total outage. The regression test against
  the previous revision fails with `['note_request_retry'] != ['note_request_failed', 'note_request_retry']`.
* **Impact**: an operator filtered the log for failures and saw only warnings
  about retries, so a provider outage looked like a transient blip.
* **Fix**: one content-free `note_request_failed` event per attempt that ends a
  leg without a retry (status, provider, canonical, model, request type, route
  position, attempt, latency, error class, job id — never a response body). A
  JSON-strategy downgrade deliberately does *not* emit it: that failure is
  followed by a re-attempt on the same leg, not by a dead provider.
* **Regression test**: `tests/test_http_502_scenario.py::Routed502Scenario::test_routed_502_metadata_is_exact_and_accounted`.

### F9 — LOW (verification honesty): the page renderer reported a defect its own drawing created

* **Where**: `scripts/render_docx_pages.py` (`_layout_paragraph`,
  `_measure_paragraph`, `_draw_paragraph`).
* **Mechanism**: the painter draws a shaded callout box four pixels beyond its
  text (a bordered paragraph two pixels), but the height reserved *before* placing
  the block counted only the text. A callout landing near the bottom margin was
  drawn past it, and the pagination loop then reported
  `page 4: content overflows the bottom margin`.
* **Reproduction**:
  `tests/test_page_renderer.py::RendererTests::test_a_callout_box_on_the_page_bottom_is_not_a_false_overflow`
  — the same fixture against the previous revision fails with exactly that
  warning; against the fixed revision the booklet renders as 5 pages with
  `warnings == []`, every page carrying blocks.
* **Impact**: the repository's only *visual* evidence tool was inventing a
  document defect, and the page it flagged was the page an operator would inspect
  by hand — the fastest way to lose trust in a verifier.
* **Fix**: `_box_padding()` is reserved by the measurement and consumed by the
  painter so pagination and painting agree. Only the bottom padding is reserved
  (the box's top padding overlaps the previous block — a deliberate, cosmetic
  ≤4 px overlap, not a layout error), no content is dropped, and the genuine
  overflow detector still fires (`test_overflow_and_blank_page_are_detected`).

### Inspected and found correct (no change)

* Reservation/refund/recovery invariants (§2 table).
* Restart recovery marks `pending`/`processing` submissions failed and releases
  exactly once; no job stays `processing` after a restart.
* Queue back-pressure (`_enqueue` → `QueueFull` → `_reject_job`), shutdown
  (`_drain_queue`), and the reservation being taken *inside* the worker (so a
  rejected job has nothing to refund).
* Delivery: a failed `.docx` build or upload does not prevent the transcript send,
  and a pagination error is raised only after the transcript is delivered.
* DOCX direction: `w:bidi` on paragraphs and styles, `w:rtl` on complex-script
  runs, `w:jc` declared for every construct, `w:bidiVisual` on every table
  (section tables, definitions, glossary, static TOC), `w:sectPr/w:bidi`.
* `structuring.py` lossless splitting and the additive merge (only blocks that are
  identical modulo whitespace/letter variants collapse).
* Media: fixed argument vector, `shell=False`, hard child-process timeout, and the
  temp directory removed in `finally` on both success and failure.

---

## 5. Test evidence and measurements

All commands were run in this workspace against the audited revision plus the
fixes above (Python 3.12.3, virtualenv built from `requirements.txt`).

| Command | Result |
| --- | --- |
| `python -m compileall -q gamas_bot scripts tests passenger_wsgi.py` | exit 0 |
| `ruff check .` | “All checks passed!” |
| `python -m mypy` | “Success: no issues found in 12 source files” |
| `python -m scripts.sync_requirements --check` | “requirements.txt is up to date” |
| `python -m pytest -q` | **1256 passed, 1 skipped, 1881 subtests passed** (baseline before this audit: 1220 passed, 1 skipped) |
| `python -m unittest discover -s tests` | **Ran 1257 tests … OK (skipped=1)**, exit 0 |
| `python -m pip check` | “No broken requirements found.” |
| `python -m scripts.benchmark_notes` | deterministic, offline; see §5.3 |
| `python -m scripts.validate_provider_platform` | **25 passed, 0 failed** |
| `python -m scripts.validate_docx --render` | **PASSED: 0 structural failure(s)**; 18 XML checks ok; offline render of 3 pages with the capability note reported separately |
| `python -m scripts.benchmark_queue --json` | back-pressure holds (p99 queue wait 452.0 ms at 50 jobs / 3 workers) |
| `python -m scripts.benchmark_admin_panels --json` | admin:ai routes 45.6 ms, router.plan 35.6 ms, home overview 22.2 ms, users page 2.0 ms, AI providers 1.2 ms on a 20 k-row seed |
| `git diff --check` | clean |

New regression tests added by this audit (27 tests, all passing):

* `tests/test_backend_artifact_scrub.py` — 9 tests: every provider key shape, the
  disclaimer forms, and the *final* artifacts (`to_json()` and the generated DOCX
  text) rather than the scrubber in isolation; plus
  `ConservativeFilteringTests`, which pins the academic sentences that must
  survive.
* `tests/test_output_truncation_recovery.py` — 13 tests: split-and-merge
  recovery, source preservation (word-for-word reconstruction of the two
  re-requested parts), bounded cost, no split for non-truncation failures, the
  distinct condition for all three provider shapes, and the splitter’s
  numerical-content safety.
* `tests/test_stt_platform_routing.py` — 1 test: the configured upload cap is
  enforced end to end.
* `tests/test_page_renderer.py` — 5 tests: capability detection, the note being a
  renderer limit rather than a document defect, its presence in the validator
  report, and the reproduced false overflow (F9).
* `tests/test_http_502_scenario.py` — 8 tests: the mandatory HTTP 502 scenario in
  routed and legacy mode (§5.2), with the keys and a transcript marker planted so
  that any leak into recorded metadata or a user-visible message fails a test.

### 5.1 Rendered-page evidence

`python -m scripts.validate_docx --out build/ --render` wrote `build/notes.docx`,
`build/plain.docx`, `build/pages/page-0{1,2,3}.png` and a contact sheet.
Inspected:

* page 1 — cover: brand block, Persian title area, metadata lines, quote box,
  centred, inside the page frame;
* page 2 — TOC block, summary, sections, definitions, examples, formulas, key
  points, callouts, a two-column table and a glossary table; **all body text is
  right-aligned**, boxed callouts span the text width, and a table’s first logical
  column renders on the **right** (`w:bidiVisual` working);
* page 3 — key-points box and the glossary table with a repeated header row; no
  overflow, no blank page, no orphan heading.

A second fixture exercised the table of contents with the shipping default
(`toc_enabled=True`, six sections, mixed Persian/English prose, a laboratory
table, a glossary): 5 pages with `warnings == []`, and **page 2 carries the table
of contents** — the «فهرست مطالب» heading plus eight entry rows (six sections,
«نکات کلیدی», «واژه‌نامه»), every row anchored to its heading, with the
page-number column left **empty** because no PDF reader is available here and the
exporter refuses to invent numbers (`DOCX static-TOC page numbers unavailable (no
renderer/PDF reader); delivering a link-only table of contents`). Toggling
`toc_enabled` off in the same fixture renders 4 pages, which is how the TOC page's
own presence is pinned rather than assumed.

**Limitation (§9):** the offline renderer draws with DejaVu, which has no Arabic
glyphs, so Persian text appears as boxes; shaping and joining could not be
inspected here. LibreOffice is not installed, so no Word-equivalent render was
possible. The renderer now reports that limit itself (F5).

### 5.2 The mandatory HTTP 502 scenario (offline, deterministic)

`tests/test_http_502_scenario.py` answers the eight required questions against a
scripted gateway that answers 502 — no key, no network, no billable request, and
both keys plus a transcript marker planted so that any leak fails a test.

| Question | Observed (routed mode) |
| --- | --- |
| 1. Route planned and why | `[nara, groq]`, both `free_only=True`; the deployment's NaraRouter-first preference survives the dead first leg |
| 2. Provider and model chosen | leg 0 `nara` / `agnes-3-flash` (attempt 1 + its one retry), leg 1 `groq` / `qwen/qwen3.8-27b` |
| 3. Credentials eligible | the leg's own pool entry; a 5xx is never blamed on a key — no quarantine, no cooldown (`test_routed_502_does_not_blame_the_key_or_quarantine_it`) |
| 4. Retry budget | `min(profile.max_retries, NOTE_API_RETRIES, AI_MAX_GENERATION_RETRIES) + 1`; the dead leg spends exactly that, with real backoff between attempts |
| 5. Rotation / failover | no key rotation (the wrong lever for a provider-wide failure); one `note_request_fallback` + `provider_route_changed` onto leg 1 |
| 6. Recorded metadata | per attempt: provider, canonical, model, request type, route position, `http_status=502`, `error_class=server`, `credential_id`, `result=failure`, `total_tokens=None`; the terminal attempt additionally logs `note_request_failed` (F8). Blob scan: no key, no transcript text |
| 7. Final result | leg 1 answers → normal notes; with **both** legs dead the run raises `AllProvidersFailedError`, a `StructuringError` — exactly the contract `bot.py` catches to deliver the raw transcript with its honest Persian notice |
| 8. Reservations | `router._budget_reservations == {}` after the run: the per-request local budget reservation is released on every failed attempt, not only on success |

Legacy mode (`ai_routing_enabled=False`, provider `gemini`) is covered
independently: `test_legacy_502_is_retried_inside_the_configured_budget_then_succeeds`
and `test_legacy_502_exhausting_the_budget_reports_the_provider_status` pin the
`ProviderHTTPError(status=502)` surface (status in the message, never the key), and
`test_legacy_502_rots_across_a_multi_key_pool_without_a_retry_storm` pins the
bound: the pool is rotated, each candidate gets its own attempt budget, so *N*
keys cost at most *N* × (`NOTE_API_RETRIES` + 1) requests — a bounded budget, not
a hotter loop.

Two honest notes from the traces: in the all-dead case the *local* per-provider
budget refused the second `groq` attempt before it reached the network
(`provider_quota_blocked`), so the aggregate failure for that leg is reported as
`rate_limited` with no HTTP status — the free-only guard doing its job, not a
provider answer; and `_DowngradeRequested` (a JSON-mode downgrade) is deliberately
not treated as a leg failure, because the same leg is immediately re-attempted
with a simpler strategy.

### 5.3 Performance

* The truncation recovery adds **no** cost to the ordinary path: it is entered
  only from `except NoteOutputTruncated`, the splitter is never called on a
  successful answer, and `test_a_part_that_fits_still_costs_one_call` pins the
  one-call behaviour. The worst case is bounded by `MAX_TRUNCATION_SPLITS`
  (4 extra splits per job).
* `scripts/benchmark_queue --json` shows the bounded queue still applying
  back-pressure instead of growing: 3 concurrent jobs, p50 latency 153 ms, p99
  queue wait 452 ms, RSS ≈ 80 MiB (50 jobs, 3 workers: 11 accepted, 39 rejected).
* `scripts/benchmark_notes` (deterministic, offline, no key) on this revision:
  mean compression ratio **1.017**, mean signal coverage **1.0**, mean semantic
  coverage **1.0**, mean factual preservation **1.0**, mean quality score
  **100.0**, `fixtures_needing_repair = 0` across the six lecture fixtures. Its
  probes also measure the long-lecture path end to end: a 61,660-character
  lecture into 3 chunks drafted 83 sections and merged them to 81 with **0**
  duplicate blocks, **0** split topics and both chunk boundaries reunited
  (`merge_duplicate_rate = 0.0`, `continuation_wrong_joins = 0`,
  `continuation_prose_losses = 0`). The document probe renders 30 sections into
  274 RTL paragraphs with 168 LTR runs and 345 complex-script faces, 5 tables
  (all with `w:bidiVisual` and repeated headers), cover, page field and page
  border present. These are the numbers for the *happy* path, which the F3 fix
  does not touch.
* DOCX generation was not modified, so no document before/after comparison is
  needed; `validate_docx` reports the same structural checks as before.

---

## 6. Confirmed fixes and the reasoning behind them

| Finding | Files changed | Why this shape of fix |
| --- | --- | --- |
| F1 | `gamas_bot/structuring.py` | Add the missing key *shapes* rather than a generic “long token” heuristic: a heuristic redacts lecture content (hashes, chemical names), while a prefix list only removes tokens that can never be course material. |
| F2 | `gamas_bot/structuring.py` | Make the existing rule work (case-insensitivity) and narrow it to the shapes a disclaimer takes, so an AI lecture keeps its sentences; the label form is handled only line-initially, where it cannot be a sentence. |
| F3 | `gamas_bot/structuring.py` | A distinct exception plus a lossless splitter: content preservation (priority 1) needs the lecture restored, and job execution (priority 2) needs the failure to be recoverable. A smaller budget or a blind continuation would either drop the tail or duplicate it. |
| F4 | `gamas_bot/stt.py` | Give the operator’s explicit value precedence over published metadata — the same precedence `STT_OPENAI_MAX_UPLOAD_BYTES` already has — keeping the registry as the default. |
| F5 | `scripts/render_docx_pages.py`, `scripts/validate_docx.py` | Report the renderer’s capability limit separately from document defects: a document must not fail for a missing system font, but an operator must not read a tofu page as verified Persian. |
| F8 | `gamas_bot/ai/routing.py` | Emit the failure event the event vocabulary already defines, at the one place that knows the attempt will not be retried. Logging `note_request_failed` on every attempt would have mislabelled retried ones. |
| F9 | `scripts/render_docx_pages.py` | Reserve the padding the painter itself adds instead of suppressing the overflow warning: a verifier that errs in the reassuring direction is still wrong, and this warning is the one that tells an operator to look at a page. |

No production route preference, free/paid restriction, quality gate or
administrative setting was changed. No existing test was weakened, skipped or
removed.

---

## 7. Required migration and configuration changes

* **Migrations**: none. No schema change, no backfill, no dropped table, and no
  released migration was edited. The upgrade test in §2 shows a pre-006
  installation upgrading in place with its rows intact.
* **Configuration**: none required. Two clarifications for operators:
  * `GROQ_STT_MAX_UPLOAD_BYTES` (and any future per-provider cap) now takes
    effect; leaving it unset keeps the registry’s published limit.
  * `AWS_TRANSCRIBE_*` remains inert because that provider is disabled in the
    registry; do not configure it expecting batch Persian transcription.
* **Deployment**: no step change. Safe procedure for a live install:
  1. back up the SQLite file (`*.sqlite3` plus its `-wal`/`-shm` sidecars, or copy
     it while the bot is stopped);
  2. `git pull`, `pip install -r requirements.txt` (unchanged), restart the
     service;
  3. confirm in the log that `Database opened … interrupted_jobs_marked_failed=0`
     (a non-zero value only means a job was interrupted by the restart, and its
     reservation was released exactly once);
  4. optionally re-run the gates in §5.
  `MAX_TRUNCATION_SPLITS` is a module constant, not an environment variable; it
  needs no `.env.example` entry and `tests/test_config_consistency.py` is
  unaffected.

---

## 8. Remaining risks and deliberate non-changes

1. **Persian shaping is not visually verified in this environment** (§9). Layout,
   direction and alignment are verified from rendered pages; glyph joining is not.
2. **English disclaimer variants outside the covered shapes** are still printed:
   imperative prompt echoes (`Be an AI assistant:`, `Act as an AI assistant:`) and
   a Persian line-initial label (`به‌عنوان یک هوش مصنوعی: …`). Filtering them risks
   deleting legitimate AI-course prose, so they are reported rather than removed.
3. **Single-marker diagnostic phrasing** (F7) survives in truncated form.
4. **Truncation recovery can cost up to four extra provider calls per job**; if a
   half still truncates, the job fails as before (loudly, with the transcript
   delivered).
5. **STT remains whole-file only**: a file above every configured engine’s cap is
   refused rather than split — documented behaviour, unchanged here.
6. **`aws_transcribe` is registered but disabled**, and its configuration surface
   is inert (F6).
7. **A local per-provider budget can refuse a retry** that a provider-side 502
   would otherwise have triggered (observed in §5.2: the second `groq` attempt
   never left the process and the leg is reported as `rate_limited` with a
   `provider_quota_blocked` event). This is the free-only guard working as
   designed; it is listed because the aggregate failure then names a local limit
   rather than the upstream status.

---

## 9. What could not be verified in this environment

* **LibreOffice / Microsoft Word rendering.** `soffice`/`libreoffice` are not
  installed and the sandbox refuses to install system packages (`sudo` is blocked
  by the “no new privileges” flag), so TOC page numbers, internal hyperlink
  destinations and Word’s own layout were not verified. The offline Pillow
  renderer supplied layout, direction, alignment, pagination and table-order
  evidence instead, and now reports its own limits.
* **`validate_docx --render` with an office engine**: the Pillow fallback ran; the
  LibreOffice branch did not.
* **Persian glyph shaping/joining** in the rendered pages (no Arabic-capable face
  installed; see §5.1).
* **Live provider behaviour.** No live API key was used and no paid request was
  made; every provider test uses mocked HTTP by design, so real rate-limit header
  shapes across providers remain unverified here.
* **Real Telegram delivery** (upload/download through Telegram’s servers,
  flood-wait behaviour, `.docx` rendering on a user’s device): the tests use
  doubles and no Telegram login was performed.
* **The Python 3.11 / 3.13 CI matrix**: this host runs 3.12.3 only. The changes
  use no version-specific syntax, `ruff` targets `py311`, and CI covers 3.11–3.13.
* **A production database copy**: the upgrade test used a constructed pre-006
  database, not a real deployment file, which was not available here.
