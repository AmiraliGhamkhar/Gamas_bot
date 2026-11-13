# Configuration

Copy [`.env.example`](../.env.example) to `.env`; never commit credentials.
Process variables override the file. Restart after edits: settings load at
startup. Use `python -m gamas_bot --check` for offline configuration diagnostics.
See [Providers](PROVIDERS.md) for routing and [Operations](OPERATIONS.md) for
paths, retention and host sizing.

## Text-generation credentials

Only **Gemini**, **AvalAI** and **OpenRouter** are routable. Each needs its own
key (`GEMINI_API_KEY`, `AVALAI_API_KEY`, `OPENROUTER_API_KEY`); a missing key
skips that route. The models shipped with the code are Gemini
`gemini-3.5-flash-lite`, AvalAI `deepseek-v4.1-flash` and OpenRouter
`nvidia/nemotron-3.5-super-vl:free`. Override them with `GEMINI_MODEL`,
`AVALAI_MODEL` or `OPENROUTER_MODEL`; the two gateway bases are configurable too.
These defaults do not establish live availability or account entitlement.

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
| `NOTE_COMPILE` / `NOTE_PROGRESSIVE_BOOKLET` | false / false |
| `NOTE_HEDGE_AFTER_SECONDS` | 0 (legacy knob; dispatch never hedges even if nonzero) |

Zero quota values mean no configured application cap, **not** unlimited
provider capacity. Set Gemini limits from the actual AI Studio project before
deployment. The effective concurrent limit is the smaller of the provider and
primary/fallback-role caps. `LLM_TOKEN_ESTIMATION_CHARS_PER_TOKEN=1.5` controls
conservative input estimation; the full requested output budget is reserved
as well. Do not inflate operating budgets to match reference model windows.
Compilation is optional and off by default; bounded map/merge, split/repair and
grounding checks are retained.

## Sensitivity and validation

`NOTE_DATA_SENSITIVITY` decides who may see a booklet's source. `auto` (the
default) classifies each submission once, before any provider call, and
**fails closed to `UNKNOWN`**: a source naming a patient/file number, national
ID, mobile number or e-mail is treated as unknown rather than assumed safe.
`standard_educational`, `internal`, `sensitive` and `phi_or_pii` are explicit
operator declarations for material that was reviewed; an unusable value falls
back to `auto` instead of failing startup. The resolved label is
passed to booklet generation, repair, coverage fill and compilation, so one
booklet never mixes policies, and it is logged once per request. Restart after
changing it — settings are read at startup.

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
