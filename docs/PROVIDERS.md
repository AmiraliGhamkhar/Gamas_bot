# Note-generation providers

Note providers are optional for transcription. Without one, audio delivers only
the transcript; document notes and Office use deterministic fallbacks. AI quizzes
and flashcards need a working note route.

**Two kinds of information are mixed on this page.** "Shipped default" is a
project-wide capability of the code and applies to every deployment.
"Verified 2026-11-07" describes real API tests at **one specific deployment**
with one key per provider; it is a recommendation, not a guarantee for another
account, model revision or quota tier. Metadata availability never proves that
text generation works.

| Route | Key | Shipped model default | Tested deployment override (2026-11-07) |
| --- | --- | --- | --- |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` | `gemini-3.5-flash-lite` |
| `nara` | `NARA_API_KEY` | `agnes-2.5-flash` | not in the active chain |
| `openrouter` | `OPENROUTER_API_KEY` | `google/gemma-4-31b-it:free` | `nvidia/nemotron-3.5-super-vl:free` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-flash` | not configured |
| `avalai` | `AVALAI_API_KEY` | `deepseek-v4.1-flash` | not configured |
| `openai_compatible` | `NOTE_API_KEY` | `gpt-4o-mini` | not configured |
| `anthropic` | `NOTE_API_KEY` | Explicit `NOTE_MODEL` required | not configured |

Shipped defaults are repository defaults, not guarantees of current availability.
Confirm model access, context limits and pricing with your provider account.

## Setup — recommended tested profile

Values verified on 2026-11-07 for one deployment. Put credentials in `.env`
(never in this repository), keep the unused legacy slot unset, and restart the
bot afterwards: environment settings are read only at process startup.

```dotenv
NOTE_PROVIDERS=gemini,openrouter
NOTE_STRUCTURED_OUTPUT=0
NOTE_REASONING_EFFORT=none
NOTE_MAX_OUTPUT_TOKENS=32768
NOTE_GEMINI_THINKING_BUDGET=-1
GEMINI_MODEL=gemini-3.5-flash-lite
OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free
```

`NOTE_PROVIDERS` is the failover policy: exactly `gemini`, then `openrouter`.
Add keys only for routes you use; keyless routes are skipped and no empty
credential is sent. Override models and URLs with settings from
[`.env.example`](../.env.example). See [Configuration](CONFIGURATION.md) for the
full variable reference and [cPanel rollout](DEPLOY_CPANEL.md) for the safe
procedure. These values are **not** shipped defaults: `DEFAULT_NOTE_PROVIDERS`
stays `gemini,nara,openrouter,deepseek,avalai`, `NOTE_STRUCTURED_OUTPUT` stays on
and `NOTE_MAX_OUTPUT_TOKENS` stays 16384 for every other deployment.

## Verified 2026-11-07 (deployment-specific observations)

### Gemini — generation verified with structured output disabled

- Provider `gemini`, model `gemini-3.5-flash-lite`, API base
  `https://generativelanguage.googleapis.com/v1beta`.
- **Metadata (not generation):** model lookup and API-key authentication
  succeeded; the model reports an output token limit of 65,536.
- **Generation:** with `NOTE_STRUCTURED_OUTPUT=0` (plus
  `NOTE_GEMINI_THINKING_BUDGET=-1`), a 745-Persian-character test input produced
  a valid parsed note titled `فیزیولوژی کلیه` with 4 sections in about 4.6 s.
- **Earlier failure:** a structured-output-enabled request returned HTTP 400
  `INVALID_ARGUMENT`; the same model and key worked with structured output off.
  The exact rejected field was **not isolated** because the failing request
  payload was not captured. Candidates in the code's structured payload are
  `generationConfig.responseSchema`, `generationConfig.thinkingConfig`
  (`thinkingBudget: 0` was the shipped default at the time) and, less likely,
  `responseMimeType`. Do not claim a root cause until that payload is examined.
- The client now applies one bounded Gemini-only compatibility retry for a
  400 `INVALID_ARGUMENT` when the request actually contains `responseSchema`:
  it removes only that field, keeps the JSON mime type, and still validates the
  answer through the strict parser and note-domain validation. It does not
  apply to auth/quota/permission errors, to unrelated 400s, or when no schema
  was sent. The tested deployment avoids the issue entirely with
  `NOTE_STRUCTURED_OUTPUT=0`.

### OpenRouter — generation verified with structured output and reasoning off

- Provider `openrouter`, model `nvidia/nemotron-3.5-super-vl:free`, API base
  `https://openrouter.ai/api/v1`.
- **Metadata (not generation):** model endpoint lookup succeeded (Nvidia),
  context length 1,000,000, metadata completion cap 131,072, free-tier daily
  request limit 50.
- **Earlier application request:** sent 16,384 output tokens, returned 16,384
  output tokens and was classified as output-truncated — an incomplete answer
  at the shipped `NOTE_MAX_OUTPUT_TOKENS=16384` default. A truncated completion
  is never accepted as a complete study note.
- **Generation:** a later test with `NOTE_STRUCTURED_OUTPUT=0` **and**
  `NOTE_REASONING_EFFORT=none` succeeded in about 19 s on 745 Persian
  characters (title `فیزیولوژی کلیه`, 4 sections). Both settings were changed
  together, so neither independently caused the success.
- The deployment target is `NOTE_MAX_OUTPUT_TOKENS=32768`. The earlier
  16,384-token request matches the shipped default exactly; the code has no
  other note-budget override and no database setting for notes, so a process
  started before `.env` was changed (or a `.env` that was never reloaded) is a
  plausible explanation. **Not conclusively established** — inspect the
  outgoing payload in logs before asserting a cause.

### NaraRouter — generation NOT verified; integration preserved

- Provider `nara`, model `laguna-s-2.1`, API base
  `https://router.bynara.id/v1`.
- **Metadata (not generation):** authentication succeeded and the account was
  entitled to 44 models, including the configured one.
- **Generation:** three attempts failed with HTTP 502 and
  `The model rejected this request. It may not support the input you sent ... or a parameter is invalid. [BAD_REQUEST]`.
  A second test with `NOTE_STRUCTURED_OUTPUT=0` and
  `NOTE_REASONING_EFFORT=none` also returned 502.
- **Conclusion:** entitlement is not a generation guarantee. This
  model/configuration is not validated for production generation and is not in
  the recommended active chain. A 502 is not proof that a model is permanently
  unsupported; preserve the provider error detail and any `request_id` when
  diagnosing. NaraRouter support and its `NARA_*` configurability remain in the
  codebase — nothing was deleted.

## Routing, precedence and legacy settings

`config.from_env()` builds one frozen `Settings` object at startup.
`note_route_order()` returns the declared `NOTE_PROVIDERS` slugs (deduplicated,
unknown/removed slugs warned and skipped), then appends the v2-era single slot
only if `NOTE_PROVIDER` + `NOTE_API_KEY`/`NOTE_BASE_URL` is actually configured
and is not already in the chain. `note_routes()` keeps only eligible routes: a
hosted provider needs its own credential and a model name.

- A credential belongs to exactly one provider (`GEMINI_API_KEY`,
  `OPENROUTER_API_KEY`, `NARA_API_KEY`, …); one key is never sent to another
  provider. Missing keys produce no `Authorization` header and no route.
- A legacy `NOTE_BASE_URL` pointing at a documented host is classified as that
  provider (e.g. `router.bynara.id` → `nara`), so it keeps its place instead of
  becoming a duplicate leg. Google's `/v1beta/openai` stays
  `openai_compatible`; its native `/v1beta` is `gemini`.
- **Leave `NOTE_API_KEY` and `NOTE_BASE_URL` unset/blank** for the tested
  profile. A configured legacy base URL is an explicit extra OpenAI-compatible
  route and is appended; a keyless local gateway is intentionally still allowed
  for backward compatibility.
- Confirm the effective order without exposing credentials:
  `python -m scripts.probe_note_providers --show-config` (offline) or the
  "Effective note routing" block of `python -m gamas_bot --check`.

The ordered chain uses available slots; a full primary overflows immediately.
Primary/fallback limits default to 4/4 and all-full waits to 300 seconds. Auth,
quota and rate limits can cool down a route and trigger failover. Transient
retries are bounded, request-parameter compatibility adjustments are bounded,
and responses are capped at 16 MiB. `NOTE_HEDGE_AFTER_SECONDS=60` can start one
extra route while a slow request is pending; `0` disables that extra quota use.

For the tested deployment, Gemini is tried first; an eligible recoverable
Gemini failure moves to OpenRouter; a permanent 4xx is not re-sent unchanged;
Nara is never attempted because it is not listed. Provider errors keep their
safe diagnostics (including request IDs) and are logged with metadata only —
never keys, full request bodies or transcript content.

## Structured-output compatibility (general capability)

`NOTE_STRUCTURED_OUTPUT=1` (shipped default) sends a provider-shaped schema:
Gemini `generationConfig.responseSchema` in its OpenAPI 3.0 dialect (upper-case
types, `required` kept, `additionalProperties` stripped), or an OpenAI
`response_format: json_schema` envelope. `NOTE_STRUCTURED_OUTPUT=0` sends
**no** `responseSchema` and no `response_format`, even when the caller passes a
schema; Gemini still sets `responseMimeType: application/json` and the system
prompt still requires strict JSON. `NOTE_JSON_MODE` is a separate opt-in that
can add `json_object` on documented OpenAI-compatible routes.

Disabling structured output does **not** relax validation: `complete_json` uses
`extract_json_object`/`_parse_json_object` (strict JSON, non-standard constants
rejected), and `notes.StructuredNotes.from_payload` refuses empty or unusable
booklets. The JSON-reminder retry and the failover chain still apply. Rejected
parameters are downgraded by a **bounded** ladder (`json_schema` →
`json_object` → plain prompt, schema/thinking/MIME fields dropped one at a
time), not by an unbounded retry loop. Groq and Cerebras were removed from the
integration; a legacy `NOTE_PROVIDERS` value naming them is skipped with a
startup warning. Size budgets for the smallest configured model. Keep grounding
enabled: [Note quality](NOTE_QUALITY.md).

Per-provider request profiles (`gamas_bot/llm_profiles.py`) normalize wire
schemas and parameters: OpenRouter `:free` routes prefer `json_object` so
endpoint filtering does not 404; DeepSeek receives documented JSON Output but
never an undocumented strict `json_schema`; AvalAI receives a documented
`json_schema` envelope with a `json_object` downgrade; and Nara omits
undocumented `response_format` and `stream_options` fields unless opted in.

## Reasoning and thinking configuration (general capability)

`NOTE_REASONING_EFFORT` accepts `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max` (`ultra` is an alias of `max`); empty means the provider default
and an unknown value is dropped rather than forwarded. `none` is a real value:
it is sent as `reasoning_effort: "none"` on routes whose capability profile
documents the field (`nara`, `openrouter`, `deepseek`). Gemini, Anthropic and a
generic `openai_compatible` gateway never receive it.

Gemini uses `NOTE_GEMINI_THINKING_BUDGET` instead: `0` sends
`thinkingConfig.thinkingBudget: 0`, a positive value caps thinking tokens, and
`-1` omits `thinkingConfig` entirely. The tested deployment uses `-1` with
structured output off. The successful OpenRouter test used
`reasoning_effort=none` **together with** structured output disabled; disabling
reasoning alone is not claimed to fix anything, and it did not fix Nara's 502.

## Output-token budget (general capability)

`NOTE_MAX_OUTPUT_TOKENS` is read by `from_env()`, passed into each request
builder, and clamped only by a provider profile's documented cap (all currently
registered profiles have no lower cap). The final value is sent as `max_tokens`
for OpenAI-compatible routes and `maxOutputTokens` for Gemini. No database or
admin setting overrides note budgets; `LEARNING_MAX_OUTPUT_TOKENS` is a separate
per-call budget for learning content only. The bot reads `.env` once at startup,
so **restart after any change** — an already-running process keeps the old
value. Do not raise the budget above 32,768 without a demonstrated need.

Logs distinguish the two numbers: `LLM request sending ... requested_output_tokens=<n>`
is the effective value on the wire (also shown by `--show-config`), while
`note provider usage ... output_tokens=<n>` is the provider's actual completion
usage. `finish_reason=length`/`max_tokens` (or Gemini `MAX_TOKENS`, Anthropic
`max_tokens`) is rejected as an incomplete answer rather than delivered as a
partial note; the message names `NOTE_MAX_OUTPUT_TOKENS`. With the 32,768
profile, `python -m gamas_bot --check` can emit an advisory `WARN` that a slow
20 tokens/s gateway would need ~1638 s against the shipped 900 s request
deadline. It is a conservative estimate, not a prediction: the verified
OpenRouter generation took about 19 s. Measure before changing
`NOTE_REQUEST_TIMEOUT_SECONDS`.

## How to test a provider independently

All commands are operator tools; the bot never runs them and the automated test
suite never calls a live API.

```bash
python -m scripts.probe_note_providers --show-config   # offline: routing + non-secret settings
python -m gamas_bot --check                           # offline configuration inspection
python -m scripts.probe_note_providers                 # metadata only, no generation quota
python -m scripts.probe_note_providers --generate short_lecture.txt  # REAL requests, consumes quota
```

On Windows use `.venv\Scripts\python.exe` instead of `python`. `--show-config`
prints the effective provider order, structured-output/reasoning/thinking
settings, the configured output budget and each route's effective *outgoing*
budget, with no credentials (URLs are stripped of userinfo and query strings).
The default probe asks only metadata questions — authentication, model
entitlement and published limits. **Only `--generate` verifies generation**;
use a short, non-sensitive transcript, expect one request per route plus the
pipeline's bounded retries, and review the parsed title/section count. A
metadata PASS with a generation FAIL is a valid result (as Nara's test showed).
Safe request IDs and error details are preserved for support tickets; keys are
redacted.

## AvalAI (general capability, not part of the tested deployment)

[AvalAI](https://docs.avalai.ir/en/api-reference/chat) is an OpenAI-compatible
gateway that serves several vendors' models under its own ids, so it is one route
in the chain rather than a replacement for `deepseek`:

```dotenv
AVALAI_API_KEY=...
AVALAI_BASE_URL=https://api.avalai.ir/v1
AVALAI_MODEL=deepseek-v4.1-flash
```

Its DeepSeek entry documents 1,000,000 input / 393,216 output tokens, JSON output,
`json_schema` structured output and **implicit** prompt caching
([provider page](https://docs.avalai.ir/en/providers/deepseek)). Caching is why the
note prompt is one static string: a cache hit requires an exact prefix match from
the start of the request, so per-job material never enters the system prompt. The
gateway bills cached input at $0.003/1M against $0.15/1M uncached, so the saving is
only real if the prefix is byte-identical — which is asserted by
`tests/test_notes.py::PromptTests`. Rate limits depend on the account tier
([tiers](https://docs.avalai.ir/en/guides/rate-limits)); on a low tier, lower
`NOTE_CONCURRENCY` and `NOTE_CHUNK_CHARS` so one booklet does not exceed the
per-minute budget. `AVALAI_BASE_URL` also repairs itself: a bare
`https://api.avalai.ir` gains the documented `/v1` prefix.

`NOTE_CONTEXT_TOKENS` stays at the shipped default (131072) even though this
model documents a 1,000,000-token input: it sizes *every* request, including the
ones served by whichever smaller-window route in `NOTE_PROVIDERS` ends up
answering, so it can only be as large as the tightest route in the order. Raise
it only when every route serves at least that much. Other shipped defaults are
`NOTE_STRUCTURED_OUTPUT=1`, `NOTE_COMPLETENESS_FILL=1`,
`NOTE_QUALITY_GATE=repair` and an empty `NOTE_REASONING_EFFORT`. They are
general defaults, **not** the 2026-11-07 deployment profile above: that profile
disables structured output, sets reasoning to `none` and lists only two routes.

For one route in depth — the AvalAI chain member — use the dedicated smoke test.
It reports the configured route, asks `GET /models` whether the model is
entitled, and (only with `--generate`/`--snippet`) sends one real note request,
printing the served model, elapsed time, token usage and the cached-input count,
which is how the static-prompt cache is confirmed:

```bash
python -m scripts.smoke_avalai                                # metadata only
python -m scripts.smoke_avalai --snippet                      # one small request
python -m scripts.smoke_avalai --generate lecture.txt --full  # whole pipeline
```

`--full` runs chunking, the editorial compile and the coverage fill on the same
text and renders the DOCX, so the compile prompt and the gates are exercised
before a rollout. Both probes write nothing to the database and exit non-zero on
any failure. Generation stages consume provider quota.
