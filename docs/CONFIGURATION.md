# Configuration

Copy [`.env.example`](../.env.example) to `.env`; never commit credentials.
Process variables override the file. Restart after edits: settings load at
startup. Use `python -m gamas_bot --check` for offline configuration diagnostics.
See [Providers](PROVIDERS.md) for routing and [Operations](OPERATIONS.md) for
paths, retention and host sizing. The default database is `data/gamas_v3.db`;
set `DB_PATH` if an intentionally different database is required, and include
that path in backups.

## Public URL and the Mini App

Two optional values connect the bot to the Mini App; both default to "not
deployed", where the bot is a pure file interface and no button points at a
dead URL.

| Setting | Meaning |
| --- | --- |
| `WEBAPP_URL` | Bare origin of this deployment (for example `https://gamas.example`). The bot appends `/app` itself and strips a trailing slash; empty means no Mini App button. |
| `BOT_USERNAME` | Bot username without the `@` (default `Gamas_bot`). The Mini App builds its invite, share, support and credit-purchase deep links from it. |

The Mini App is served by the same Passenger application at `/app`, so its
JSON calls are relative (`/api/v1/*`) and never carry a host or a credential.
Deployment steps are in [Mini App deployment](DEPLOY_MINIAPP.md).

## Subscription plans

`gamas_bot.billing.PAID_PLAN_SPECS` is the canonical six-plan catalog. Each
plan has explicit `PLAN_<hours>_ORIGINAL_PRICE_TOMAN` and
`PLAN_<hours>_PRICE_TOMAN` settings plus hours and 30-day validity; the latter
is the final price charged and frozen in the payment snapshot. Defaults are
listed in [Billing](BILLING.md). Prices are not calculated from the credit unit
or plan duration. No 50-hour plan code or configuration keys are defined;
that tier stays unavailable until both prices are confirmed. On full startup
sync, obsolete standard plans are marked inactive while custom plans, payments,
snapshots and balances are preserved; partial sync never retires unrelated
plans.

## Text-generation credentials

Only **Gemini**, **AvalAI** and **OpenRouter** are routable. Each needs its own
key (`GEMINI_API_KEY`, `AVALAI_API_KEY`, `OPENROUTER_API_KEY`); a missing key
skips that route. The models shipped with the code are Gemini
`gemini-3.5-flash-lite`, AvalAI `deepseek-v4.1-flash` and OpenRouter
`nvidia/nemotron-3.5-super-vl:free`. Override them with `GEMINI_MODEL`,
`AVALAI_MODEL` or `OPENROUTER_MODEL`; the two gateway bases are configurable too.
These defaults do not establish live availability or account entitlement.

The native Gemini 3.5 Flash-Lite GenerateContent request uses the documented
`contents`, `systemInstruction`, `generationConfig.maxOutputTokens` (clamped
to 65,536), `generationConfig.responseFormat.text` JSON MIME type/schema, and
`generationConfig.thinkingConfig.thinkingLevel` fields. The request omits
deprecated sampling parameters (`temperature`, `topP`, `topK`), `candidateCount`,
and prefilled `model` turns; local JSON and domain validation remain
authoritative if a provider rejects optional structured-output fields.
`NOTE_GEMINI_THINKING_LEVEL` accepts `minimal`, `low`, `medium` or `high`.
The legacy `NOTE_GEMINI_THINKING_BUDGET` variable is ignored with a names-only
deprecation warning if left in an old environment.

AvalAI (`deepseek-v4.1-flash`) sends OpenAI-compatible `/v1/chat/completions`
requests with `response_format` (`json_schema` with automatic downgrade to
`json_object` and prompt-only fallback), output clamping at 393,216 tokens,
and omits unverified reasoning-effort fields and `stream_options`.

OpenRouter (`nvidia/nemotron-3.5-super-vl:free`) sends OpenAI-compatible
`/api/v1/chat/completions` requests with output clamping at 131,072 tokens,
`json_object` on `:free` endpoints (avoiding 404 endpoint filtering),
`stream_options.include_usage`, optional `provider.sort="throughput"`, and
OpenRouter's unified `reasoning` object when `NOTE_REASONING_EFFORT` is set
(`{"enabled": false}` for `none`, or `{"effort": "<level>", "exclude": true}`).

`NOTE_PROVIDERS=avalai,gemini,openrouter` controls available providers, not an
escape from task policy: known tasks have fixed priorities and OpenRouter is
always last. `prefer_provider`/`NOTE_COMPILE_PROVIDER` cannot override them.
The v2 `NOTE_PROVIDER`/`NOTE_API_KEY`/`NOTE_BASE_URL`/`NOTE_MODEL` slot remains
compatible only as an approved provider. A recognized provider host is
classified as that provider, never duplicated; an explicit valid legacy slot
can append a provider omitted from `NOTE_PROVIDERS`. Clear that slot as well
when disabling a provider. Arbitrary generic text providers are not supported.
Removed credentials and unsupported slots are ignored from **both** `.env`
and the process environment, with names-only warnings rather than startup
failure. Their keys are never lent to an approved provider.

## Conservative operational defaults

| Setting | Default |
| --- | --- |
| `NOTE_CONTEXT_TOKENS` / `NOTE_CHUNK_CHARS` / `NOTE_PACK_TARGET_CHARS` / `NOTE_MAX_OUTPUT_TOKENS` | 262144 / 64000 / 20000 / 65536 |
| `NOTE_REQUEST_TIMEOUT_SECONDS` / `NOTE_TTFB_TIMEOUT_SECONDS` | 1200 / 60 |
| `NOTE_CONCURRENCY` | 3 |
| `LLM_PRIMARY_MAX_IN_FLIGHT` / `LLM_FALLBACK_MAX_IN_FLIGHT` | 3 / 1 |
| `AVALAI_RPM_LIMIT` / `AVALAI_TPM_LIMIT` / `AVALAI_MAX_IN_FLIGHT` | 35 / 750000 / 3 |
| `OPENROUTER_RPM_LIMIT` / `OPENROUTER_RPD_LIMIT` / `OPENROUTER_MAX_IN_FLIGHT` | 10 / 15 / 1 |
| `GEMINI_RPM_LIMIT` / `GEMINI_TPM_LIMIT` / `GEMINI_RPD_LIMIT` / `GEMINI_MAX_IN_FLIGHT` | 0 / 0 / 0 / 1 |
| `NOTE_STRUCTURED_OUTPUT` / `NOTE_STRICT_GROUNDING` / `NOTE_QUALITY_GATE` | 1 / false / repair |
| `NOTE_PDF_ENGINE` | typst |
| `NOTE_MAX_REPAIR_ATTEMPTS` / `NOTE_ENABLE_DETERMINISTIC_FALLBACK` | 2 / true |
| `NOTE_COMPILE` / `NOTE_PROGRESSIVE_BOOKLET` | false / false |
| `NOTE_LLM_PLANNER` | false |
| `NOTE_UNKNOWN_MODEL_CONTEXT_TOKENS` / `NOTE_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS` | 32768 / 4096 |
| `NOTE_CAPACITY_SAFETY_TOKENS` | 512 |
| `NOTE_COMPILE_MAX_OUTPUT_TOKENS` / `NOTE_REPAIR_MAX_OUTPUT_TOKENS` | 0 / 0 (inherit the generation ceiling) |
| `NOTE_HEDGE_AFTER_SECONDS` | 0 (legacy knob; dispatch never hedges even if nonzero) |

Zero quota values mean no configured application cap, **not** unlimited
provider capacity. Set Gemini limits from the actual AI Studio project before
deployment. The effective concurrent limit is the smaller of the provider and
primary/fallback-role caps. `LLM_TOKEN_ESTIMATION_CHARS_PER_TOKEN=1.5` controls
conservative input estimation; the full requested output budget is reserved
as well. Do not inflate operating budgets to match reference model windows.
The booklet map uses plan topics as its primary units. It subdivides only
oversized units, preserving whole blocks and sentence boundaries where
possible. `NOTE_CHUNK_CHARS` is still an operator transport ceiling, not a
chunking target. Input allowances subtract the system prompt, request framing,
wire schema, model/task output reserve, and safety margin. The native Gemini
route checks its prompt with Google's `countTokens`; private proxies and other
routes use the conservative configurable estimate. Unknown model IDs use the
configurable ceilings above, not the provider default model's advertised size.
`NOTE_COMPILE=true` opts into synthesis after deterministic merge and a local
preflight. The editorial request is only sent when deterministic checks find a
coverage gap, duplicate, conflicting merge, bad title or plan-order problem;
otherwise the merged draft stays untouched. It edits the merged draft, not the
raw source. Final verification and the bounded repair pass run on the retained
draft after synthesis.
`NOTE_COMPILE_MAX_CHARS=0` derives the synthesis request ceiling from the routed
context/output budgets; a positive ceiling can only lower it. Large drafts use
whole-section topic groups, chapter outlines and a final complete ID manifest.
The editor allows at most 24 calls per booklet (provider failover is separately
bounded). An unfit section or outline stays intact and an attempted but
incomplete synthesis is recorded as recovered. By default (`NOTE_STRICT_GROUNDING=false`),
the bot operates in direct scientific booklet mode — preserving academic Persian
rewriting, labeled non-factual clarification, English scientific terms, and
source-supported tables, emphasis, and Q&A. Section writers do not send the raw
text of other topics. The direct-mode writer rejects unsupported measurements
and checks scoped measurements and source references before delivery. Strict
mode (`NOTE_STRICT_GROUNDING=true`) also gates unsupported prose/terminology.
With `NOTE_QUALITY_GATE=repair`, strict mode may make at most two small
`BOOKLET_VERIFY` requests for unresolved claim-to-source comparisons. `warn`
turns off both semantic verification calls and node repairs, not deterministic
checks. Ambiguities left unresolved remain findings, not approvals.

`NOTE_MAX_REPAIR_ATTEMPTS` caps repairs across the booklet (default 2, hard
maximum 3); a defective node gets at most one request even when it has multiple
issues. `NOTE_COMPLETENESS_FILL=true` opts direct mode into the same capped
coverage repairs; it is not a separate quota or full-booklet fill pass. Rejected
repairs leave verified material intact, and omitted facts are reported as a safe
partial result. With `NOTE_ENABLE_DETERMINISTIC_FALLBACK=true`, unusable nodes can
recover from their local source. `NOTE_PDF_ENGINE` chooses the primary graphical
PDF renderer (`typst` by default, with automatic ReportLab fallback).


Booklet topic planning is deterministic by default. `NOTE_LLM_PLANNER=true`
allows local outline refinement only on contextual multi-chunk booklet runs
with at least 250,000 source characters and no more than one fifth covered by
heading/transition-backed topics. At most three weak windows qualify for a
request, with 2–32 whole source blocks and at most 6,000 serialized input
characters per window. The normal booklet provider chain and quota accounting
apply. Titles must quote their assigned blocks verbatim; incomplete or invalid
partitions leave the deterministic window intact. This is not an input size cap
or permission to send an entire document for planning.

## Interactive HTML review sheet

The review sheet adds **no environment variables**. It reuses the note-provider
settings (`NOTE_*`, the provider chain and the allowlists), and its one planning
call follows the same timeouts and provider failover. Its 30-minute duration is
a default of the renderer and planner API, not a deployment setting.

## Learning validation

Generated quizzes require a nonempty source and valid `medium`/`hard`
difficulty by default, matching `QUIZ_SCHEMA` and the strict learning caller.
Persian/English aliases are normalized; wrong types, empty/missing tags and
unrecognized difficulty labels are rejected. Explicit `require_source=False`/`require_difficulty=False` are legacy-reader
options, not production generation defaults; they do not relax question/option
validation. Finite numeric locators remain compatible; NaN/Infinity do not.
