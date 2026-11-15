# Phase 0 — Architecture reconnaissance

**Scope/date:** 2026-11-15 UTC. This document records static reconnaissance of the checked-out `arena/c4b817d1-gamas-bot` branch. The request asked for an `audit/<date>` branch, but the Arena session policy fixes the working branch; no branch was created or changed.

## Evidence commands

```bash
git status --short --branch
find . -maxdepth 2 -type f -not -path './.git/*' -printf '%p\n' | sort
git log --all --numstat --format='' | awk 'NF==3 {a[$3]+=$1+$2} END {for (p in a) print a[p],p}' | sort -nr
python - <<'PY'  # LOC map over gamas_bot/**/*.py
...
PY
grep -RInE '(events\.NewMessage|events\.CallbackQuery|download_media|Path\(|ZipFile|create_subprocess|sqlite3\.connect)' gamas_bot --include='*.py'
```

The working tree was clean and on `arena/c4b817d1-gamas-bot`. `git rev-list --all --count` returned `1`, so repository-history churn is not meaningful beyond the initial 101,723-line import. LOC is therefore used as the hotspot signal.

## Package/runtime reconnaissance

- **Package manager:** pip/setuptools. `pyproject.toml` is the declared source of runtime ranges; `requirements.txt` is generated from it (`pyproject.toml:1-14`, `requirements.txt:1-21`). There is no `uv.lock`, `poetry.lock`, `Pipfile.lock`, or other resolver lockfile.
- **Supported Python:** `requires-python = ">=3.11"` (`pyproject.toml:20`) and CI tests 3.11, 3.12, and 3.13 (`.github/workflows/tests.yml:12-15`).
- **cPanel interpreter configured by repository instructions:** `python3.11` (`README.md:30-31`, `docs/CPANEL.md:16-17`). This is [INFERRED] deployment intent, not proof of the customer's live interpreter; the latter is [UNVERIFIED] because no cPanel host is accessible. No `runtime.txt` exists.
- **Entrypoints:** `python -m gamas_bot` / console script `gamas-bot` (`pyproject.toml:62-63`), watchdog `scripts/ensure_running.py`, optional Passenger WSGI endpoint (`passenger_wsgi.py`).

## Module map

| Area | Main modules | Responsibility / flow position |
|---|---|---|
| Startup and lifecycle | `__main__.py`, `config.py`, `launcher.py`, `instance_lock.py`, `logging_config.py` | Loads config, checks dependencies, locks a Telegram session, starts/joins the bot, and gives cPanel cron a throttled single-instance watchdog. |
| Telegram ingress / UI | `bot.py`, `bot_ui.py`, `bot_jobs.py`, `bot_documents.py`, `bot_learning.py`, `bot_wallet.py`, `bot_support.py`, `bot_admin.py` | Registers Telethon `CallbackQuery` and `NewMessage` handlers (`bot.py:544-555`), dispatches uploads, callbacks, controls, billing, support and admin flows. |
| Capacity and job scheduling | `jobs.py`, `capacity.py`, `intake.py`, `progress.py`, `telegram_transfer.py`, `fileio.py` | Bounded admission, per-user round-robin job queue, provider/stage slots, bounded streams and Telegram transfers. |
| Media and STT | `media.py`, `media_worker.py`, `stt.py`, `http_util.py`, `diarization_eval.py` | Isolated PyAV worker process media work, media metadata/normalisation, Speechmatics → Deepgram → OpenAI-compatible STT and bounded HTTP. |
| LLM and booklets | `llm.py`, `llm_profiles.py`, `llm_quota.py`, `notes.py`, `notes_evidence.py`, `_prompt_block.py`, `transcript.py`, `docx_export.py`, `notes_pdf.py`, `review_sheet*.py` | Routes approved LLM providers, produces/validates structured notes, evidence/acceptance checks, then renders RTL DOCX/PDF/HTML artifacts. |
| Documents / Office | `doc_extract.py`, `presentations.py`, `ocr.py`, `office*.py`, `pdf_export.py`, `document/*` | Extracts documents/slides, optional remote OCR, Office export and PDF/DOCX structure/RTL QA. |
| SQLite / business data | `db.py`, `db_core.py`, `db_*.py`, `billing.py`, `subscription.py`, `learning.py`, `referrals.py` | `aiosqlite` database facade and repositories, migrations, atomic billing/holds, user, library, learning, support and reporting state. |

The repository's own architecture table corroborates the map (`docs/DEVELOPMENT.md:9-28`).

## Per-user request flow

1. Telethon receives a message or callback in `GamasBot` (`bot.py:544-555`); message metadata is classified as text/document/presentation/media around `bot.py:1711-1756`.
2. Media/presentation jobs go through admission and the bounded round-robin `JobQueue`; `submit` rejects full queues rather than growing unbounded (`jobs.py:203-235`), and workers own each job through terminal cleanup (`jobs.py:278-370`).
3. Media is downloaded with limits, inspected/extracted/transcoded through a subprocess worker, then submitted to configured STT in priority/failover order. The default policy is Speechmatics, Deepgram, then OpenAI-compatible (`config.py:203-207`).
4. The transcript is persisted/delivered. If configured, LLM mapping/repair/acceptance generates grounded structured notes, which are rendered to RTL DOCX/PDF and review HTML before persisted/delivered artifacts are offered.
5. Documents and Office flows use the same Telegram boundary and SQLite ownership model but have direct bounded extraction/build paths rather than STT.

## Trust boundaries

| Boundary | Untrusted data | Initial code locations | Required review focus |
|---|---|---|---|
| Telegram MTProto | Message text, caption, callback data, sender identity, filenames, media bytes | `bot.py:544-555`, `intake.py`, `bot_jobs.py`, `bot_documents.py` | authorization/ownership, duplicate callbacks/messages, size/type checks, state transitions, user-visible errors. |
| Files / archives | Media codec metadata, PDF/DOCX/PPT/PPTX/ZIP entries, images, filenames | `telegram_transfer.py:215-287`, `doc_extract.py`, `presentations.py:401-425`, `artifact_validation.py:29-38` | traversal, archive/member/ratio limits, decompression memory, parser CPU/memory, temp cleanup. |
| Remote providers | STT/LLM/OCR responses and provider error payloads | `stt.py`, `llm.py`, `ocr.py`, `http_util.py` | strict response parsing, error-key redaction, timeout/retry/circuit cost controls, provider output injection. |
| SQLite persistence | Every user/protocol field and persisted binary artifact | `db_core.py`, `db_*.py` | parameterized SQL, ownership predicates, WAL/busy behavior, atomic billing/submission changes, DB growth. |
| Filesystem / cron / environment | `.env`, paths, lock/session/log files and watchdog args | `config.py`, `launcher.py`, `instance_lock.py`, `logging_config.py` | secret exposure, permission/containment, duplicate process/session, log rotation and restart recovery. |
| Document rendering | LLM transcript/output text and asset bytes | `notes.py`, `docx_export.py`, `notes_pdf.py`, `review_sheet_html.py` | prompt injection, HTML/XML safety, RTL logical order, output validation and provider-name leakage. |

## Top risk hotspots to deep-review

1. `gamas_bot/notes.py` — 4,755 LOC, prompt construction, parsing, repair and final acceptance; largest production module.
2. `gamas_bot/docx_export.py` — 3,156 LOC, untrusted structured content to OOXML and Persian rendering.
3. `gamas_bot/llm.py` — 2,952 LOC, external LLM routing, retries/timeouts and output parsing.
4. `gamas_bot/bot_learning.py` — 2,384 LOC, long-lived callback/state/credit workflow.
5. `gamas_bot/config.py` — 2,032 LOC, hard constraints and all credential/path/limit parsing.
6. `gamas_bot/bot_documents.py` — 1,940 LOC, user-file ingestion and artifact delivery.
7. `gamas_bot/bot_jobs.py` — 1,901 LOC, core media/STT/booklet job lifecycle.
8. `gamas_bot/bot.py` / `bot_admin.py` — 1,890 / 1,845 LOC, global untrusted ingress and privileged commands.
9. `gamas_bot/stt.py` — 1,589 LOC, quota/error fallback and large audio uploads.
10. `gamas_bot/presentations.py` — 1,561 LOC, PPTX ZIP parsing and media extraction.

All paths entered together in a single imported commit (`git rev-list --all --count` = `1`), therefore “high churn” cannot be distinguished from file size. Direct test import counts identify several modules with no direct test import (for example `_prompt_block.py`, `bot_admin.py`, `bot_documents.py`, `db_billing.py`, `launcher.py`, `telegram_transfer.py`); this is an [INFERRED] testability signal only, not proof that behavior is untested because high-level tests exercise some through `bot.py`.

## Recon limitations

No virtual environment or audit tools were preinstalled. Phase 1 creates an isolated `.venv` and installs declared runtime/dev/audit tools without contacting Telegram/STT/LLM production endpoints.
