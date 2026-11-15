# Architecture and coverage matrix — Second pass

**Date:** 2026-11-15 UTC

**Branch:** `arena/357a91cc-gamas-bot`

**Evidence convention:** all matrix outcomes use `[VERIFIED]`, `[INFERRED]`, or `[UNVERIFIED]`; command evidence is retained under `AUDIT/results/`.

## Runtime and entry points

- **[VERIFIED]** The package supports Python `>=3.11` at `pyproject.toml:18-28`; CI declares 3.11, 3.12, and 3.13 at `.github/workflows/tests.yml:12-29`. The audit sandbox ran only Python 3.11.2; see `AUDIT/results/a2-environment.log`.
- **[VERIFIED]** Telegram callback ingress dispatches through `GamasBot._dispatch_callback`, which applies private-chat, ban, and administrator-prefix gates before routing. See `gamas_bot/bot.py:1523-1563` and `AUDIT/results/d6-config-launcher-characterization.log`.
- **[VERIFIED]** The optional Passenger entry point only exposes `/` and `/health`, maps state to minimal JSON, and catches launcher exceptions rather than returning a traceback. See `passenger_wsgi.py:20-55` and `AUDIT/results/d6-config-launcher-characterization.log`.
- **[VERIFIED]** The instance guard creates private parent/file modes and holds an OS advisory lock, protecting the operational one-bot assumption. See `gamas_bot/instance_lock.py:37-115` and `AUDIT/results/d6-config-launcher-characterization.log`.

## Review coverage matrix

| Trust boundary / hotspot | Source reviewed | Characterization or scan evidence | Disposition |
|---|---|---|---|
| Telegram callbacks and administration | `gamas_bot/bot.py:1523-1563`, `gamas_bot/bot_admin.py:1748-1845` | Forged/non-admin hub cases at `tests/test_admin_hubs.py:123-144`; `AUDIT/results/d6-config-launcher-characterization.log` | **[VERIFIED]** central private/ban/admin gates and per-route validation observed. |
| User documents and document-derived callbacks | `gamas_bot/bot_documents.py:1544-1599`, `gamas_bot/bot_documents.py:1848-1910` | Foreign-user case at `tests/test_document_ingestion.py:489-518`; `AUDIT/results/d3-document-media-characterization.log` | **[VERIFIED]** document metadata lookups are user scoped before actions. |
| Telegram file transfer and temporary disk | `gamas_bot/telegram_transfer.py:58-134`, `gamas_bot/bot_jobs.py:808-837` | `tests/test_download_reservations.py:1-134`; `b2-*`, `b3-*` logs | **[VERIFIED]** F-002 reservation, stream cap, and cleanup regressions pass. |
| Job admission/fairness/cancellation | `gamas_bot/jobs.py:165-330`, `gamas_bot/capacity.py:75-178` | `AUDIT/results/d5-stt-capacity-job-characterization.log` | **[VERIFIED]** bounded queue, round-robin user selection, and capacity timeouts exercised. |
| Media subprocess boundary | `gamas_bot/media.py:102-180`, `gamas_bot/media_worker.py:1-12` | `AUDIT/results/d3-document-media-characterization.log`, `c2-targeted-grep.log` | **[VERIFIED]** argument-vector subprocess, deadline, cancellation, process-group cleanup observed. |
| Files, Office ZIP, PDF, image input | `gamas_bot/doc_extract.py:80-128`, `gamas_bot/doc_extract.py:290-315`, `gamas_bot/presentations.py:240-435` | `AUDIT/results/d3-document-media-characterization.log` | **[VERIFIED]** image pixels, text/expansion, ZIP traversal/member and XML controls exercised. |
| Compression-ratio policy | `gamas_bot/doc_extract.py:290-315`, `gamas_bot/presentations.py:401-425` | `AUDIT/results/d3-zip-compression-ratio-before.log` | **[INFERRED]** absolute limits exist; no ratio threshold is currently a confirmed defect. Human decision remains open due compatibility trade-off. |
| STT / provider responses | `gamas_bot/stt.py:269-358`, `gamas_bot/stt.py:1291-1349`, `gamas_bot/http_util.py:15-105` | `AUDIT/results/d5-stt-capacity-job-characterization.log` | **[VERIFIED]** response/read/time/cooldown/failover controls are covered offline. |
| LLM prompts and output acceptance | `gamas_bot/_prompt_block.py:450-485`, `gamas_bot/llm.py:1986-2185`, `gamas_bot/notes_evidence.py:1-120` | `tests/test_risky_module_characterization.py:27-37`; `AUDIT/results/d4-llm-render-characterization.log` | **[VERIFIED]** source stays in user-message construction; parsing/acceptance suite passed. |
| DOCX/PDF/HTML rendering | `gamas_bot/docx_export.py:261-302`, `gamas_bot/notes_pdf.py:120-150`, `gamas_bot/review_sheet_html.py:163-185` | `AUDIT/results/d4-llm-render-characterization.log`, `f3-pdf-docx-realistic-render-benchmark.log` | **[VERIFIED]** XML/HTML defenses and PDF raster/DOCX structure QA exercised. |
| SQLite/billing/payment/referral | `gamas_bot/db_core.py:249-364`, `gamas_bot/db_billing.py:259-374`, `gamas_bot/db_payments.py:313-403`, `gamas_bot/db_referrals.py:64-199` | `AUDIT/results/d7-db-learning-characterization.log`, `c1-bandit-production-triage-candidates.log` | **[VERIFIED]** transaction, ownership, status-transition and parameterization controls reviewed. |
| Learning/free-use support | `gamas_bot/db_users.py:440-500`, `gamas_bot/db_support.py:27-58`, `gamas_bot/db_support.py:180-198`, `gamas_bot/bot_learning.py:1-120` | `AUDIT/results/d7-db-learning-characterization.log` | **[VERIFIED]** fixed whitelist identifiers and user-bound parameters observed; targeted tests passed. |
| Config, logging, lock, cPanel launcher | `gamas_bot/config.py:1280-1687`, `gamas_bot/logging_config.py:1-160`, `gamas_bot/launcher.py:253-279`, `passenger_wsgi.py:20-55` | `AUDIT/results/d6-config-launcher-characterization.log` | **[VERIFIED]** offline configuration/launcher suite passed. |
| Dependency and secrets supply chain | `pyproject.toml:18-80`, `requirements.txt:1-21`, `gamas_bot/db_referrals.py:19-25` | `c4-pip-audit-deduplicated.log`, `c3-detect-secrets-clean-source-summary.log`, `c3-git-history-detect-secrets-clean-summary.log` | **[VERIFIED]** no known dependency advisories; current candidates are disposed non-secrets. |
| Python version / live-host parity | `pyproject.toml:18-28`, `.github/workflows/tests.yml:12-29` | `AUDIT/results/a2-environment.log` | **[UNVERIFIED]** native 3.12/3.13 and live cPanel were unavailable. |

## Data/control flow checkpoints

1. **Ingress:** a private Telegram event reaches the callback/message dispatch layer; callback routing applies ban/admin checks first (`gamas_bot/bot.py:1523-1563`).
2. **Admission:** media jobs enter bounded queue/capacity policies; downloads reserve disk before a transfer (`gamas_bot/jobs.py:165-330`, `gamas_bot/bot_jobs.py:808-837`).
3. **Untrusted binary handling:** Telegram bytes are bounded, then document/media parsing applies type, size, archive and worker-process controls (`gamas_bot/telegram_transfer.py:215-320`, `gamas_bot/doc_extract.py:290-315`, `gamas_bot/media.py:102-180`).
4. **Remote-provider handling:** STT/LLM/OCR clients use bounded reads/timeouts and structured error processing (`gamas_bot/stt.py:269-358`, `gamas_bot/http_util.py:15-105`).
5. **Business state:** user-scoped reads plus serialized transactions protect holds, payments, referrals, learning charges and support state (`gamas_bot/db_billing.py:259-374`, `gamas_bot/db_payments.py:313-403`).
6. **Artifact delivery:** untrusted text is normalized/escaped for DOCX/PDF/HTML and outputs are QA-inspected in the offline fixture benchmark (`gamas_bot/docx_export.py:261-302`, `gamas_bot/review_sheet_html.py:322-351`, `AUDIT/results/f3-pdf-docx-realistic-render-benchmark.log`).

## Static-scan interpretation

- **[VERIFIED]** Bandit’s 40 production B608 matches are all dynamic identifier/placeholder composition candidates, not a demonstrated tainted-value SQL path. Fixed support filters provide a representative whitelist at `gamas_bot/db_support.py:51-58`; user values are bound at `gamas_bot/db_support.py:183-198`. See `AUDIT/results/c1-bandit-production-triage-candidates.log`.
- **[VERIFIED]** The manual dangerous-API inventory did not match code execution, unsafe serialization, archive extraction, insecure temporary-file construction, `shell=True`, or direct requests/httpx calls in scope. See `AUDIT/results/c2-targeted-grep.log`.
- **[UNVERIFIED]** The local offline Semgrep rules are deliberately narrower than a maintained remote policy pack. See `AUDIT/results/c2-semgrep-status.log`.

## Benchmark context

- **[VERIFIED]** PyAV ran a one-minute synthetic media matrix with PyAV 18.1.0 under Python 3.11.2; see `AUDIT/results/f1-pyav-benchmark.log`.
- **[VERIFIED]** The DOCX/PDF benchmark used three offline fixtures—short Persian, medical-long, mixed RTL/LTR—and passed DOCX structure and PDF raster QA; see `AUDIT/results/f3-pdf-docx-realistic-render-benchmark.log` and `tests/test_booklet_rendering.py:1-60`.
- **[INFERRED]** Timings are hardware-local and are unsuitable as production latency/SLA promises.
