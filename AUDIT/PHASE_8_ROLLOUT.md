# Phase 8 — Booklet regression and rollout report

> **Historical audit — not current documentation.** This page records one earlier
> checkout and the measurements taken on it. Line numbers, file inventories,
> test counts, migration numbers and "planned" phases may no longer match the
> code. Current operator guides live in [docs/](../docs/README.md); treat
> `gamas_bot/` as the source of truth.

**Decision:** Candidate for controlled staging/canary after the offline gate. Live provider latency, quota entitlement, cost, and human educational quality remain unverified; do not treat this report as production sign-off. No deployment, push, or production-data changes were performed.

## Changes and compatibility

The common pipeline preserves normalized source blocks and locators, plans topics deterministically, splits only to fit route budgets, writes sections in bounded parallel calls, and merges them in source order. Optional synthesis edits that merge rather than rewriting the booklet. The retained result receives deterministic verification, narrowly scoped semantic checks when eligible, and bounded targeted repair. PDF, DOCX and the optional HTML review use the retained artifact without another model call. Unsafe local output can be withheld; safe incomplete results stay downloadable and are flagged. Fatal render failures do not complete the job; raw text and successful artifacts remain available.

**Phase 8 verified fixes:**

- **Capacity:** A provider cap lower than the positional primary/fallback cap let the local pool admit a request that the SQLite quota ledger would immediately reject. That was reproduced with a held AvalAI slot, a one-slot provider limit, and a three-slot local primary limit. A waiting request failed with `AvalAI: quota unavailable` despite a configured wait. Pool admission and quota reservation now use the same tighter limit; the new regression passes and both requests finish. Provider order, user settlement, quota RPM/TPM/RPD and cross-process enforcement are unchanged.
- **Table evidence:** An exact Markdown table was marked unverified because its source rows had leading/trailing pipes. In strict mode this requested semantic verification for a literal copy. The deterministic check now recognizes verbatim cells under the same ordered Markdown headers. The regression also confirms that swapping two numeric rows does not get that exemption.

**Inventory** (relative to `5af2483`, the current branch base):

| Area | Added / modified files |
| --- | --- |
| Runtime | `gamas_bot/{booklet_verification,bot_documents,bot_jobs,llm,note_planning,notes,office_notes,review_sheet}.py` |
| Tests | `tests/{test_booklet_pipeline,test_booklet_rollout}.py` added; `tests/{test_booklet_verification,test_bot,test_content_plan,test_llm_quota,test_office_flow}.py` amended |
| Benchmark and release report | `scripts/booklet_regression_bench.py`, `AUDIT/PHASE_8_ROLLOUT.md` |
| Operator docs | `.env.example`, `docs/{CHANGELOG,CONFIGURATION,FEATURES,OPERATIONS,PROVIDERS,TESTING}.md` |

Already on `main` at `5af2483`: new planning/segmentation/verification modules, `gamas_bot/{note_planning,semantic_segments,booklet_synthesis,booklet_verification}.py`, with changes to normalized extraction, transcript and presentation handling, section writing and evidence checking. See the [Phase 0 inventory](PHASE_0_PIPELINE.md) for the original data flow. Use `git diff --name-status e2148f4` after committing the candidate for the full cross-phase file inventory.

- **Deleted files:** none. Since `e2148f4`, the old full-draft compiler (`_compile_pass`, `_compile_once`, `_compile_in_groups`, `_gate_compiled`, `compile_groups`) and overlapping legacy repair routines (`_audit_and_repair`, `_repair_once`, `repair_request_document`, `merge_repaired_sections`) were retired. `ChunkOutcome` and `_block_keys` were retired with the old code. `fill_coverage_gaps` remains a compatibility entry point into the capped verifier. The standalone AI review planner still exists, but production booklet delivery no longer calls it.
- **Preserved:** approved provider configuration and fallback order, quiz/flashcard routing, user ledger and subscription gates, stored booklet JSON/readers, raw and artifact recovery, renderer templates, RTL/fonts and legacy configuration readers. Prompt credential redaction and backend-artifact scrubber ASTs are unchanged from `e2148f4`.
- **DB migrations / dependencies:** none added, removed, or modified since `e2148f4`. Use the existing lock/recovery and automatic startup migration checks; there is no migration step for this release.
- **Environment:** no new or removed variables relative to `5af2483`. New optional variables relative to `e2148f4`: `NOTE_LLM_PLANNER=false`, `NOTE_UNKNOWN_MODEL_CONTEXT_TOKENS=32768`, `NOTE_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS=4096`, `NOTE_CAPACITY_SAFETY_TOKENS=512`, `NOTE_COMPILE_MAX_OUTPUT_TOKENS=0`, `NOTE_REPAIR_MAX_OUTPUT_TOKENS=0`. No variables were removed. Start with `NOTE_COMPILE=false`, `NOTE_LLM_PLANNER=false`, and avoid changing quality mode or quota settings in the same rollout. Existing compatibility knobs remain accepted; do not repurpose them.

## Regression and quality evidence

**Final gate:** `python -m pytest -q --tb=short` — **2,009 passed, 521 subtests,
0 failures** (161.16 s). Ruff, mypy (80 source files), requirements sync,
compilation and `git diff --check` passed. The new concurrency and table tests
both failed before their fixes and passed on the final candidate.

- Real synthetic TXT, PDF, PPTX and DOCX are extracted, generated through a fake wire provider, verified, and rendered as readable PDF and DOCX. Remaining integration coverage includes audio/transcript, independent quiz/flashcards, membership gates, timeouts and fallback, credits, recovery after interruption, and failed render/delivery. The media retry fixture checks one duration debit across multiple provider attempts; Office failure leaves its source and billing snapshot untouched.
- The stress source was **306,831 characters**, with nested headings, 4,500 repetitions, definitions, measurements, a table, and distinct beginning/middle/end anchors. Under a 6,400-character fitted-body limit the ordered owned spans were contiguous, non-overlapping and reconstructed the exact input. Context was not counted as owned coverage. No prose or table rows were filtered from those spans solely because of content. This is not a promise that generated fields reproduce every source line: security redaction, extraction/admission limits, intentional condensation and verification withhold rules still apply.
- The same source key scores topic coverage, facts, three definitions (terminology), three measurement sentences, two table rows, two causal explanations and one ordered procedure. Heading order/organization and readability were checked through source hierarchy, structured section order and actual file validation; exact duplicate paragraphs were counted separately. The retained review prompt and offline cloze HTML test are proxies for exam utility, not a learner-outcome measurement. The fake provider cannot establish natural explanation depth, readability preferences, semantic fidelity outside the key, or exam effectiveness with live models. No quality claim is based on output length.
- `python -m gamas_bot --check` exited 2 with an empty configuration (required settings diagnosed; note generation unavailable), and exited 0 with isolated synthetic settings (dependencies/media worker passed). Neither check contacted a provider. Rollout timeout defaults remain 900 s total / 25 s first byte for notes, subject to existing failover and slot limits. Cross-process quota and stale-job recovery have regression coverage; full section checkpoints are supported by the orchestration API, not automatically persisted for all bot jobs.

## Before/after performance (offline)

Same **51,831-character** synthetic source and scripted transport on `e2148f4`
(pre-overhaul), `5af2483` (Phase 6 merge), and the release candidate. Each row is
three separate fresh Python 3.11.2 processes on the same x86-64, two-CPU sandbox;
rows show medians, including process high-water RSS. Temporary SQLite quota,
model routing, JSON parsing, generation, verification and validated PDF/Word
rendering are exercised. The stub uses 4 ms of artificial provider delay and
zero retry backoff. Tokens are consistently estimated at 1.5 characters/token
from serialized requests and visible response text, **not actual provider
tokens or prices**. Latency excludes chat, STT, and optional HTML; audio and HTML
were exercised separately in the functional suite.

| Workload | Revision | Requests (write / synth / repair) | Est. input / output tokens | p50 job / batch wall (s) | Peak RSS (MiB) |
| --- | --- | ---: | ---: | ---: | ---: |
| Default, 1 job | Original | 9 (9 / 0 / 0) | 124,679 / 3,328 | 0.370 / 0.370 | 146.6 |
| | Phase 6 | 9 (9 / 0 / 0) | 124,519 / 1,345 | 0.726 / 0.726 | 151.9 |
| | Candidate | 9 (9 / 0 / 0) | 124,519 / 1,345 | 0.760 / 0.761 | 151.4 |
| Synthesis, 1 job | Original | 10 (9 / 1 / 0) | 129,063 / 4,030 | 0.375 / 0.376 | 146.2 |
| | Phase 6 | 10 (9 / 1 / 0) | 129,211 / 1,401 | 0.739 / 0.740 | 152.3 |
| | Candidate | 10 (9 / 1 / 0) | 128,899 / 1,401 | 0.821 / 0.822 | 152.0 |
| Fault + synthesis, 3 concurrent jobs | Original | 39 (33 / 3 / 3) | 483,096 / 11,196 | 1.621 / 1.669 | 168.2 |
| | Phase 6 | 39 (33 / 3 / 3) | 468,679 / 4,581 | 1.749 / 1.758 | 182.4 |
| | Candidate | 39 (33 / 3 / 3) | 460,988 / 4,581 | 2.062 / 2.075 | 176.9 |

- Healthy runs: no failed requests, retries, or repairs. The fault runs injected six AvalAI HTTP 503s per batch (6/39 attempts, 15.4% overall). Candidate AvalAI incurred 6 failures on 28–34 requests (17.6–21.4%); OpenRouter had none. Each batch made three same-provider retries, overflowed/fell back to OpenRouter, and made one unsuccessful bounded repair per job after the stub deliberately withheld a measurement. All artifacts rendered. Observed concurrent total peaked at 3 wire requests; per-route peaks were AvalAI 3 and OpenRouter 1. Planning and semantic verification requests were zero for these scripted routes/settings.
- Healthy quality key: all three revisions retained **3/3 topics, 9/9 statements, 3/3 definitions, 3/3 measurement sentences and 2/2 table rows**, including the explanation/procedure and review prompt. Phase 6 marked the healthy table unverified; the candidate passed. The original has no comparable report. The fault group retained **8/9 statements and 2/3 measurement sentences**; neither report-bearing revision called that complete. Exact duplicate paragraphs went from 0 on the original to 4 healthy / 5 fault on both newer revisions. Repeated material across fitted sections remains a readability trade-off, not a quality win.
- **No local speedup was demonstrated.** Candidate job median was 0.760 s versus 0.370 s pre-overhaul (0.726 s at Phase 6) without synthesis, and 2.062 s versus 1.621 s for the fault batch. Request counts were unchanged; smaller scripted replies reflect the changed JSON protocol, not a claim of production savings. Verify actual cost and latency on a controlled pilot, particularly if turning on optional synthesis.

Reproduce with `python -m scripts.booklet_regression_bench --repeats 250`, then
add `--compile`, then `--compile --fault --jobs 3`. To run marked revisions,
extract each `git archive <revision>` to a separate temporary directory (no branch
switch), set `PYTHONPATH=.` there, and invoke the same absolute script path using
the same virtualenv. The benchmark never opens network connections or reads a
user source.

## Deployment and rollback — operator runbook, not executed

1. Review and approve the exact release; stage it with a separate service account/session/config/data directory. Keep paid planner and synthesis off initially. Baseline `done`/`partial`/failed rates, retry/timeouts, queue waits, per-stage usage, latency percentiles and RSS; compare with like-for-like source sizes and formats. Set acceptance limits from staging measurements, not the synthetic loop below.
2. During a maintenance window, stop and verify the single instance is stopped. Disable the scheduled watchdog first on cPanel. Store a restricted backup of the deployed revision, `.env`, and data (including the SQLite database/session and any sidecars). For a systemd install, the following are examples to run in the existing checkout with operator-approved access:

   ```bash
   cd /opt/gamas-bot
   sudo systemctl stop gamas-bot
   # Confirm stopped and take the private backup before proceeding.
   git pull --ff-only             # approved release from the configured deployment branch
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m gamas_bot --check
   sudo systemctl start gamas-bot
   sudo systemctl status gamas-bot
   sudo journalctl -u gamas-bot -n 80 --no-pager
   ```

   For cPanel follow [the existing watchdog update procedure](../docs/CPANEL.md#6-update-safely); for Windows use [the launcher guide](../docs/WINDOWS.md). Run the preflight under the service’s effective configuration. Re-enable the watchdog only after health and logs are clean. Never point two instances at one Telegram session or one production database.
3. After explicit live-test approval, smoke-test a short and a long representative source, successful and partial retrieval, note-provider failover, and one ledger settlement. First enable optional synthesis (then planner, separately) only if the measured cost/quality warrants it. A rise in completion misreporting, double debit, loss of source/range coverage, authorization failures, or sustained budget/latency regression is a stop condition. Compare aggregate logs; do not copy secrets or user sources to the report.
4. **Rollback:** disable optional planner/compile for a cost-only regression. For correctness or lifecycle regressions, stop/drain the candidate, restore the pinned previous release and compatible config/dependencies, preflight, and restart a single instance. This change has no schema migration, so prefer keeping the current compatible database to preserve intervening credits and submissions. If another migration crossed the deployment window, roll code and compatible data back together following the existing backup procedure, reconciling intervening ledger activity before any restore.

**Remaining limitations:** Scanned PDFs need text extraction outside this pipeline; unsupported formats/admission caps still apply. Explicit budget/failure states are possible (including the 200 paid-map cap); bounded repair cannot guarantee completeness or semantic equivalence. Extractive review HTML requires sufficient verified material. Real provider tokenization/cost, production-sized latency/RSS, arbitrary unusual documents, and human quality still need staging validation.
