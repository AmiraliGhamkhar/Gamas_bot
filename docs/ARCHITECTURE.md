# Architecture

One Telethon client, bounded asyncio work and SQLite WAL. PyAV media work uses
bounded child processes. No broker, web framework, external DB or system Office/media
binary is needed.

## Flows

```text
Audio/video → admission + hold → queue → media → STT → transcript delivered
            → optional grounded notes → RTL Word → Telegram-backed library
PDF/DOCX/PPTX → owner-scoped extraction → SQLite text cache → notes/exports
             → narration only on explicit action
Text/CSV/Markdown → private Office draft → bounded build → Word/Excel/PPT/PDF
Wallet → order + receipt → admin review → transactional credit
Studio edit → validated StructuredNotes → revision → PDF + DOCX artifacts
           → stored bytes served to Studio and Telegram (never a re-render)
```

## Code map

Modules are under `gamas_bot/`.

| Area | Modules |
| --- | --- |
| Startup/config | `__main__`, `config`, `launcher`, `instance_lock`, `logging_config` |
| Telegram workflows | `bot`, `bot_ui`, `bot_jobs`, `bot_documents`, `bot_learning`, `bot_wallet`, `bot_support`, `bot_admin` |
| Contracts/routing | `protocols`, `bot_deps`, `routing`, `conversation_state`, `intake` |
| Queue/transfers | `jobs`, `capacity`, `progress`, `telegram_transfer`, `telegram_text` |
| Media/providers | `media`, `media_worker`, `presentations`, `stt`, `llm`, `http_util` |
| Notes/documents | `transcript`, `notes`, `notes_evidence`, `doc_extract`, `ocr`, `office*`, `docx_export`, `notes_pdf` (RTL PDF), `pdf_export` |
| Note-provider layer | `config` (routes), `llm_profiles` (capabilities), `llm` (wire dialects, bounded retries/failover), `notes` (strict JSON + domain validation) |
| Mini App / Studio | `mini_app` (WSGI + initData auth), `mini_app_store` (per-request SQLite facade), `mini_app/` static bundle |
| Storage/billing | `db` facade, `db_*` facets, `billing`, `subscription`, `learning` |

## Note-provider layer

`config.from_env()` reads `.env` once at startup; `note_routes()` turns the
ordered `NOTE_PROVIDERS` chain into eligible routes, each with its own key, base
URL and model. `llm.py` builds provider-specific requests (Gemini native or
OpenAI-compatible), applies profile capabilities from `llm_profiles.py`, retries
only bounded transient/parameter-compatibility failures and fails over to the
next route. `notes.py` parses strict JSON and validates the booklet shape and
grounding before any export. Truncated completions are refused. Provider
settings are environment-only: the v3 database holds no AI-provider preferences,
so no migration or admin change is needed to switch routes. See
[Note-generation providers](PROVIDERS.md).

## Boundaries

- Serialize `BEGIN IMMEDIATE` writes; use the query-only connection for reporting.
  Reports must not mutate state or take the write lock.
- Atomic admission, bounded queues/stages and owner checks. Release one stage slot
  before waiting on another.
- Transcript delivery determines transcription billing; learning uses durable
  pending/finalized/refunded recovery. See [Billing](BILLING.md).
- SQLite holds text/metadata; Telegram holds generated files. Library pruning does
  not erase the separate document-text cache. The Studio's re-rendered booklet
  artifacts are stored per revision in SQLite BLOBs (migration 027/028); both
  Telegram buttons and Studio exports serve those exact bytes.
- Studio structured editing uses one canonical model end to end:
  `StructuredNotes → editor → validated StructuredNotes → PDF + DOCX`. There is
  no structured→plain-text→reconstruct path, editing never overwrites the raw
  transcript or the original pipeline output, and a failed render aborts the
  save so a revision never points at missing artifacts.
- Shutdown stops intake and joins/rejects work before closing SQLite. Startup
  recovery follows the singleton lock.
- `migrations/v3/` runs at startup. Add numbered migrations; never edit applied SQL.
  Test fresh databases, populated upgrades and replay safety.
- Preserve private drafts/files, bounded provider bodies, output validation and
  metadata-only logs. See [Security](SECURITY.md) and [AGENTS.md](../AGENTS.md).
