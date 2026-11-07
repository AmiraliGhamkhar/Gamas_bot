# Configuration

Copy [`.env.example`](../.env.example) to `.env` once, then edit it. Never replace
`.env` during updates. Process environment overrides the file; relative paths
resolve from the project directory. **The bot reads `.env` once at startup into a
frozen settings object — nothing is reloaded while it runs, so restart after
every change.** Provider settings live only in the environment; there is no v3
database or admin setting for AI providers and no migration for them.

## Required values

| Setting | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | API credentials from my.telegram.org |
| `ADMIN_IDS` | Comma-separated numeric Telegram user IDs, not usernames |
| STT key | At least one of `SPEECHMATICS_API_KEY`, `DEEPGRAM_API_KEY`, `STT_OPENAI_API_KEY` |

Use real values, not placeholders. Keep secrets out of Git, logs and support messages.

## Optional features

| Feature | Configuration |
| --- | --- |
| Lecture booklets | Add a [note-provider key](PROVIDERS.md); audio otherwise delivers transcript only |
| Channel membership | Default `SUBSCRIPTION_REQUIRED=true`, channel `@gamas_AI`; make the bot a channel admin. [Guide](TELEGRAM_SUBSCRIPTION.md) |
| Manual purchases | Set `PAYMENT_CARD_NUMBER`, `PAYMENT_CARD_HOLDER`, `PAYMENT_BANK_NAME` |
| Free Office | `OFFICE_ENABLED=true`; deterministic fallback works without note keys |
| Scans/photos | Default `OCR_ENABLED=false`; [install the OCR extra](OCR.md) before enabling |
| File logs | Set `LOG_FILE=data/logs/bot.log`; foreground otherwise uses stdout |

## Note-provider configuration

Full provider behaviour and the 2026-11-07 test results are in
[Note-generation providers](PROVIDERS.md). This is the variable reference.

| Setting | Shipped default | Meaning |
| --- | --- | --- |
| `NOTE_PROVIDERS` | `gemini,nara,openrouter,deepseek,avalai` | Ordered failover chain; unknown/removed slugs are warned and skipped, duplicates removed, uncredentialed routes never attempted. |
| `NOTE_PROVIDER`, `NOTE_API_KEY`, `NOTE_BASE_URL`, `NOTE_MODEL` | empty (v2-era single slot) | Backward-compatible legacy slot. Only appended when a key or base URL is configured, and never duplicated; a documented host is classified as that provider. Leave these unset for the tested profile so no extra `openai_compatible` route appears. |
| Per-provider keys | empty | `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `NARA_API_KEY`, `DEEPSEEK_API_KEY`, `AVALAI_API_KEY`. Each key serves only its own provider; never commit or print them. |
| Per-provider models / bases | see `.env.example` | `GEMINI_MODEL`, `OPENROUTER_MODEL` (`*_BASE_URL`, `NARA_MODEL`, …). Deployment overrides, not global defaults. |
| `NOTE_STRUCTURED_OUTPUT` | `1` | Sends `responseSchema` (Gemini) / `response_format: json_schema` (documented OpenAI-compatible routes). `0` omits both; Gemini keeps `responseMimeType: application/json` and every answer still goes through strict JSON parsing and note-domain validation. |
| `NOTE_REASONING_EFFORT` | empty (provider default) | `none\|minimal\|low\|medium\|high\|xhigh\|max`; `none` is a real value sent as `reasoning_effort: "none"` on routes that document the field (`nara`, `openrouter`, `deepseek`). Unknown values are dropped. Never sent to Gemini, Anthropic or generic gateways. |
| `NOTE_GEMINI_THINKING_BUDGET` | `0` | `0` sends `thinkingConfig.thinkingBudget: 0`; positive caps thinking tokens; `-1` omits `thinkingConfig`. Gemini-only. |
| `NOTE_MAX_OUTPUT_TOKENS` | `16384` | Output budget sent as `max_tokens` / `maxOutputTokens` after any provider-specific cap (none currently registered). The tested deployment uses `32768`; do not raise it further without evidence. |
| `NOTE_REQUEST_TIMEOUT_SECONDS` | `900` | Wall-clock deadline of one note request. At 32,768 output tokens the offline check may emit a conservative advisory `WARN` (20 tokens/s estimate); measure before changing it. |
| `NOTE_JSON_MODE` | `false` | Separate opt-in `json_object` mode on documented OpenAI-compatible routes. |
| `NOTE_CONTEXT_TOKENS`, `NOTE_CHUNK_CHARS`, `NOTE_CONCURRENCY` | `131072`, `64000`, `3` | Input sizing for the tightest configured route, not the output budget. |

**Provider precedence.** `NOTE_PROVIDERS` is the policy. `note_route_order()`
uses that order and appends an explicitly configured legacy `NOTE_PROVIDER`
slot only once; `note_routes()` then drops routes without their own credential
(and without a model). A credential is never shared between providers. Print
the effective, credential-free order with
`python -m scripts.probe_note_providers --show-config` or the "Effective note
routing" block of `python -m gamas_bot --check`.

**Recommended tested deployment profile (2026-11-07, deployment-specific, not a
shipped default):**

```dotenv
NOTE_PROVIDERS=gemini,openrouter
NOTE_STRUCTURED_OUTPUT=0
NOTE_REASONING_EFFORT=none
NOTE_MAX_OUTPUT_TOKENS=32768
NOTE_GEMINI_THINKING_BUDGET=-1
GEMINI_MODEL=gemini-3.5-flash-lite
OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free
```

Add `GEMINI_API_KEY` and `OPENROUTER_API_KEY` in `.env`. NaraRouter remains
configurable but is excluded: its `laguna-s-2.1` generation test failed with
HTTP 502. The successful OpenRouter test used disabled structured output **and**
`none` reasoning together; neither is claimed to be the independent cause.
Gemini's earlier HTTP 400 `INVALID_ARGUMENT` with structured output enabled is
not root-caused (the failing payload was not captured); the client applies one
bounded Gemini-only schema-dropping retry for that error class.

**Restart lifecycle.** After editing any of these variables, stop the running
bot and start it again with the existing process-management commands (see
[Operations](OPERATIONS.md)); a running process keeps its old settings. The
earlier real application request used 16,384 output tokens while a later
inspection showed 32,768 — that matches the shipped default versus the new
setting and a process started before the change. No other code or database
layer overrides note budgets, but the outgoing request (not just `.env`) must be
checked in logs to confirm which value a given process actually sent.

## Safe starting limits

Keep shipped defaults unless measurements justify changes.

| Settings | Defaults |
| --- | --- |
| `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS`, `MAX_ACTIVE_JOBS_PER_USER` | 3 / 8 / 2 |
| `MAX_CONCURRENT_DOWNLOADS`, `MEDIA_WORKER_CONCURRENCY`, `MAX_CONCURRENT_UPLOADS` | 2 / 1 / 2 |
| `MAX_FILE_SIZE`, `MAX_MEDIA_DURATION_SECONDS` | 134217728 bytes (128 MiB) / 7200 seconds |
| `DOC_MAX_BYTES`, `TEMP_MIN_FREE_BYTES` | 20971520 bytes (20 MiB) / 67108864 bytes (64 MiB) |
| `SPEECHMATICS_MAX_IN_FLIGHT`, `DEEPGRAM_MAX_IN_FLIGHT`, `STT_OPENAI_MAX_IN_FLIGHT` | 2 / 4 / 2 |
| `STT_PROVIDER_TIMEOUT_SECONDS`, `STT_SLOT_WAIT_TIMEOUT_SECONDS` | 1800 / 600 seconds |
| `LLM_PRIMARY_MAX_IN_FLIGHT`, `LLM_FALLBACK_MAX_IN_FLIGHT` | 4 / 4 slots |
| `LLM_SLOT_WAIT_TIMEOUT_SECONDS` | 300 seconds |
| `NOTE_CONCURRENCY` | 3 requests (lower it on an AvalAI low-tier account) |
| `NOTE_HEDGE_AFTER_SECONDS` | 60 seconds (0 disables hedging) |
| `NOTE_MAX_OUTPUT_TOKENS`, `NOTE_STRUCTURED_OUTPUT`, `NOTE_REASONING_EFFORT`, `NOTE_GEMINI_THINKING_BUDGET` | 16384 / 1 / empty / 0 (see the deployment profile above) |
| `NOTE_STRICT_GROUNDING`, `NOTE_QUALITY_GATE`, `NOTE_COMPLETENESS_FILL` | true / repair / true |
| `OFFICE_MAX_CHARS`, `OFFICE_MAX_INPUT_BYTES`, `OFFICE_MAX_SLIDES` | 100000 / 1000000 / 30 |
| `OFFICE_SESSION_TTL_MINUTES` | 30 minutes |
| `OFFICE_JOB_CONCURRENCY`, `OFFICE_MIN_INTERVAL_SECONDS` | 2 builds / 10 seconds |
| `LIBRARY_MAX_ITEMS` | 50 references/user; active-quiz sources may be retained longer |
| `RECEIPT_RETENTION_DAYS`, `TRANSCRIPT_RETENTION_DAYS` | 90 / 0 (keep transcript text forever) |

Per-provider keys and models — including `AVALAI_API_KEY`, `AVALAI_BASE_URL` and
`AVALAI_MODEL` — are listed in [Note-generation providers](PROVIDERS.md) and in the
template. The template lists additional model, timeout, tariff and presentation
settings.
`NOTE_MODE` is a legacy no-op; audio booklets use full mode. Compact document
notes are separate. See [Billing](BILLING.md) for learning prices.

## Private paths

Defaults: `DB_PATH=data/gamas_v3.db`, `TELEGRAM_SESSION=data/gamas`,
`TEMP_DIR=data/tmp`, `RECEIPT_DIR=data/receipts`. The session file is normally
`data/gamas.session`. Keep all paths outside web roots.

## Validate

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

**cPanel Terminal** (virtualenv active):

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
```

Fix `FAIL`; review `WARN`. `--check` also prints the effective note routing
without credentials. The offline check does not prove live keys, model access,
reachability or generation; preflight also probes the host/network and creates
temporary checks. For non-secret routing/token inspection use
`python -m scripts.probe_note_providers --show-config` (offline); metadata-only
provider probes and the opt-in, quota-consuming `--generate` probe are described
in [Note-generation providers](PROVIDERS.md).

### Support ticket admission

`SUPPORT_MAX_OPEN_TICKETS` defaults to 3. `SUPPORT_TICKET_COOLDOWN_SECONDS`
defaults to 600; set it to 0 to disable the creation cooldown. Both limits
are enforced atomically when a ticket is created.

`DOC_UNPACKED_MAX_BYTES` limits the cumulative expanded Office archive size (default 104857600 bytes).

Manual flashcard decks are limited by `FLASHCARD_MAX_CARDS_PER_DECK` (default 200).

`LEARNING_GLOBAL_CONCURRENCY` (default 2) bounds concurrent generation phases; `LEARNING_MAX_OUTPUT_TOKENS` (default 4096) limits each learning completion.
