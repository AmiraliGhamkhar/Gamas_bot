# Configuration

Copy [`.env.example`](../.env.example) to `.env`; never commit credentials.
Process variables override the file. Restart after edits: settings load at
startup. Use `python -m gamas_bot --check` for offline configuration diagnostics.
See [Providers](PROVIDERS.md) for routing and [Operations](OPERATIONS.md) for
paths, retention and host sizing.

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
`contents`, `systemInstruction`, `generationConfig.maxOutputTokens`,
`generationConfig.responseFormat.text` JSON MIME type/schema, and
`generationConfig.thinkingConfig.thinkingLevel` fields. The request omits
sampling parameters (`temperature`, `topP`, `topK`) and `candidateCount`; local
JSON and domain validation remain authoritative if a provider rejects optional
structured-output fields. `NOTE_GEMINI_THINKING_LEVEL` accepts `minimal`, `low`,
`medium` or `high` for Gemini 3.x. The numeric
`NOTE_GEMINI_THINKING_BUDGET` remains only for older Gemini models.

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
| `NOTE_CONTEXT_TOKENS` / `NOTE_CHUNK_CHARS` / `NOTE_MAX_OUTPUT_TOKENS` | 131072 / 64000 / 16384 |
| `NOTE_REQUEST_TIMEOUT_SECONDS` / `NOTE_TTFB_TIMEOUT_SECONDS` | 900 / 25 |
| `NOTE_CONCURRENCY` | 3 |
| `LLM_PRIMARY_MAX_IN_FLIGHT` / `LLM_FALLBACK_MAX_IN_FLIGHT` | 3 / 1 |
| `AVALAI_RPM_LIMIT` / `AVALAI_TPM_LIMIT` / `AVALAI_MAX_IN_FLIGHT` | 35 / 750000 / 3 |
| `OPENROUTER_RPM_LIMIT` / `OPENROUTER_RPD_LIMIT` / `OPENROUTER_MAX_IN_FLIGHT` | 10 / 15 / 1 |
| `GEMINI_RPM_LIMIT` / `GEMINI_TPM_LIMIT` / `GEMINI_RPD_LIMIT` / `GEMINI_MAX_IN_FLIGHT` | 0 / 0 / 0 / 1 |
| `NOTE_STRUCTURED_OUTPUT` / `NOTE_STRICT_GROUNDING` / `NOTE_QUALITY_GATE` | 1 / true / repair |
| `NOTE_DATA_SENSITIVITY` | auto |
| `NOTE_MAX_REPAIR_ATTEMPTS` / `NOTE_ENABLE_DETERMINISTIC_FALLBACK` | 2 / true |
| `NOTE_COMPILE` / `NOTE_PROGRESSIVE_BOOKLET` | false / false |
| `NOTE_HEDGE_AFTER_SECONDS` | 0 (legacy knob; dispatch never hedges even if nonzero) |

Zero quota values mean no configured application cap, **not** unlimited
provider capacity. Set Gemini limits from the actual AI Studio project before
deployment. The effective concurrent limit is the smaller of the provider and
primary/fallback-role caps. `LLM_TOKEN_ESTIMATION_CHARS_PER_TOKEN=1.5` controls
conservative input estimation; the full requested output budget is reserved
as well. Do not inflate operating budgets to match reference model windows.
Compilation is optional and off by default; bounded map/merge, split/repair and
grounding checks are retained. `NOTE_MAX_REPAIR_ATTEMPTS` caps the ordered
claim-faithfulness, coverage, and structural repair passes (they stop early).
When `NOTE_ENABLE_DETERMINISTIC_FALLBACK=true`, an exhausted eligible provider
chain yields a local source-preserving booklet rather than discarding successful
chunks. `NOTE_COMPLETENESS_FILL` remains strictly source-grounded and may run
while `NOTE_STRICT_GROUNDING=true`.

## Sensitivity and validation

`NOTE_DATA_SENSITIVITY` decides who may see a booklet's source. `auto` (the
default) classifies each submission once, before any provider call, and is an
identifier heuristic rather than de-identification: a source naming a
patient/file number, national ID, mobile number or e-mail, or reading like a
case narrative (`شرح حال`, `بستری شد`, `case report`), is treated as `UNKNOWN`
rather than assumed safe, and a named person after an honorific becomes
`SENSITIVE`, which no provider may receive without its approval flag.
`standard_educational`, `internal`, `sensitive` and `phi_or_pii` are explicit
operator declarations for material that was reviewed; an unusable value falls
back to `auto` instead of failing startup. The resolved label is
passed to booklet generation, repair, coverage fill and **every** compile
request (single, grouped and the second-level merge), so one booklet never mixes
policies, and it is logged once per request. Restart after changing it —
settings are read at startup.

Unclassified calls default to `UNKNOWN` and cannot use OpenRouter. `INTERNAL`,
`SENSITIVE` and `PHI_OR_PII` require the corresponding
`AVALAI_ALLOW_SENSITIVE_DATA`, `GEMINI_FREE_ALLOW_SENSITIVE_DATA` or
`OPENROUTER_FREE_ALLOW_SENSITIVE_DATA` setting (all false by default). An
OpenRouter opt-in still does not admit `UNKNOWN`. Only mark reviewed, synthetic
or otherwise justified material `STANDARD_EDUCATIONAL`; the task name alone
never supplies that assertion.

Generated quizzes require a nonempty source and valid `medium`/`hard`
difficulty by default, matching `QUIZ_SCHEMA` and the strict learning caller.
Persian/English aliases are normalized; wrong types, empty/missing tags and
unrecognized difficulty labels are rejected. Flashcards require a source too.
Explicit `require_source=False`/`require_difficulty=False` are legacy-reader
options, not production generation defaults; they do not relax question/option
validation. Finite numeric locators remain compatible; NaN/Infinity do not.
