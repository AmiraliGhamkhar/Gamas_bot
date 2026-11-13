# Providers

## Approved text-generation routes

Only **Gemini**, **AvalAI** and **OpenRouter** are routable. Shared
OpenAI-compatible HTTP/schema code serves AvalAI and OpenRouter; it does not
register a fourth provider. Speech-to-text is a separate pipeline below.

| Provider | Key | Default model | Default API base |
| --- | --- | --- | --- |
| Gemini | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` | `https://generativelanguage.googleapis.com/v1beta` |
| AvalAI | `AVALAI_API_KEY` | `deepseek-v4.1-flash` | `https://api.avalai.ir/v1` |
| OpenRouter | `OPENROUTER_API_KEY` | `nvidia/nemotron-3.5-super-vl:free` | `https://openrouter.ai/api/v1` |

Default availability: `NOTE_PROVIDERS=avalai,gemini,openrouter`. Missing keys
skip a route. Removed/unknown slugs and old dedicated credentials are ignored
with safe warnings from either `.env` or the process environment; they cannot
activate providers or brick startup. See [Configuration](CONFIGURATION.md) for
approved legacy-slot compatibility and how to disable a route fully.

### Task priorities

| Task | Eligible attempt order |
| --- | --- |
| Booklet generation, repair, compile; 20-question and difficult quizzes | AvalAI → Gemini → OpenRouter |
| Key points, compact notes, smaller quizzes, flashcards, Office tables/slides/documents | Gemini → AvalAI → OpenRouter |
| Unspecified/custom task | Configured approved primaries, then OpenRouter |

Task policy takes priority over list ordering and preferences. OpenRouter is
always a last fallback, never promoted by `prefer_provider` or
`NOTE_COMPILE_PROVIDER`. Capacity/quota exhaustion and provider failure may
skip to the next **eligible** route; they never bypass sensitivity policy.
Ordinary unclassified calls exclude OpenRouter even when it has a key.

### Structured output and model capabilities

Gemini uses its native schema dialect (upper-case types, preserved `required`,
unsupported keywords removed). AvalAI uses a non-strict `json_schema` envelope
with a bounded JSON-object/no-format compatibility ladder. OpenRouter Free
starts with `json_object` to avoid filtering out endpoints that do not advertise
schema support; other OpenRouter models can use a schema envelope. Streaming,
usage parsing, finish-reason/truncation checks and bounded local JSON/domain
validation remain in place. A malformed or truncated answer is never silently
accepted or delivered as a partial booklet. Healthy requests are not hedged,
even if the legacy hedge setting is nonzero.

The checked-in reference profiles distinguish model context, input and output
caps from application budgets:

| Profile | Reference context / input tokens | Reference output cap |
| --- | --- | --- |
| Gemini | 1048576 / 1048576 | 65536 |
| AvalAI | 1000000 / 1000000 | 393216 |
| OpenRouter | 1000000 / 1000000 | 131072 |

These are repository metadata, **not** current provider/account guarantees.
Operating defaults remain **131072 context / 64000 chunk characters / 16384
output**, with `NOTE_COMPILE=false`; never substitute a reference context window
for the output budget. Verify current model documentation and entitlement
before changing a deployment. Offline tests do not verify live compatibility.

### Quota budgets and reliability

AvalAI defaults: **35 RPM, 750000 TPM, 3 in flight**. OpenRouter defaults:
**10 RPM, 15 RPD, 1 in flight**. Gemini defaults to **1 in flight** with
RPM/TPM/RPD unset (zero); configure those from the actual AI Studio project,
not an assumed free-tier guarantee. Local primary/fallback caps (3/1) also
apply, so a fallback may have fewer effective slots than its provider cap.

The shared SQLite/WAL ledger uses `BEGIN IMMEDIATE` to reserve request/token
budgets and in-flight capacity atomically across processes sharing the same
database. Reservations include conservative input estimates and the full output
cap plus a bounded retry allowance against RPM/RPD. Reported total usage is
reconciled; absent usage on success retains the full token estimate. Failure
releases unused tokens and in-flight capacity; request allowances remain
counted conservatively. Conservative reservations can underutilize budget.
Bounded transport/compatibility retries still consume provider requests.

The SQLite circuit breaker opens after three consecutive transient/auth
failures and permits one recovery probe after cooldown. Invalid requests and
context/output-budget errors do not count as provider-health failures. Metrics
aggregate task/provider/model counts, outcomes, estimates, usage and latency
without storing prompts or user identifiers. Bounded booklet chunk/split/repair
and grounding/completeness checks are unchanged.

### Sensitivity policy

Both `complete()` and `complete_json()` accept explicit `data_sensitivity`.

- `UNKNOWN` (including unrecognized labels): Gemini/AvalAI may be eligible;
  **OpenRouter never is**, even with its sensitive-data opt-in.
- `STANDARD_EDUCATIONAL`: an explicit caller assertion permitting the approved
  routes; only use when source review/job metadata warrants it.
- `INTERNAL`, `SENSITIVE`, `PHI_OR_PII`: no provider is eligible without its
  individual approval flag. All three flags default to false; AvalAI is not
  presumed approved for patient or personal data.

Task selection is not automatic de-identification/classification. Never use
real patient data in fixtures or smoke tests. See [Security](SECURITY.md).

The booklet pipeline classifies its source once (`NOTE_DATA_SENSITIVITY`,
`auto` by default) and reuses that one label for generation, repair, coverage
fill and compilation; `auto` fails closed to `UNKNOWN`. Every request therefore
logs one policy line, and no stage can widen the policy for a later stage:

```
LLM route policy task=BOOKLET_GENERATION sensitivity=UNKNOWN routes=avalai:eligible, gemini:eligible, openrouter:blocked_by_unknown
```

`eligible`, `blocked_by_unknown` and `blocked_by_sensitivity` explain exactly
why a configured provider was or was not attempted, without transcript text or
credentials.

### Request diagnostics

A failed route logs `note provider failed; trying the next one provider=<slug>
error=<ExceptionClass> detail=<sanitized>`, where the detail carries the HTTP
status and the provider's own bounded message. When every eligible route fails,
one `every configured note provider failed task=… categories=… detail=…` line
lists the whole chain. The message shown to the user names only the classes
(`AvalAI: LLMAuthError؛ Gemini: LLMResponseError`), because a provider may echo
credential-like text in its error body; the sanitized details stay in the log.
Truncated (`finish_reason=length`), empty and malformed answers raise
`LLMResponseError` and fall through to the next eligible route instead of
producing a partial booklet.

### Safe probes

```bash
python -m scripts.probe_note_providers --show-config
```

`--show-config` is offline: it shows eligible configuration and effective output
budgets without printing credentials. Actual task/sensitivity policy can narrow
or reorder that configuration. Without the flag, the script makes metadata
requests (models/entitlement/limits), **not** generation requests. Metadata PASS
does not establish that a model can generate usable notes.

`python -m scripts.probe_note_providers --generate SYNTHETIC_TRANSCRIPT.txt`
is a separate, explicit operator action that sends one map-style request per
configured route, with bounded retries, and **consumes quota**. Use non-sensitive
synthetic text only. Neither live metadata nor generation probes belong in
routine CI. Never paste keys, private text or provider bodies into a report.

## Speech-to-text providers

`STT_PROVIDERS` defaults to `speechmatics,deepgram,openai_compatible`:
Speechmatics primary, Deepgram non-blocking overflow, then an explicitly
configured OpenAI-compatible **transcription** endpoint. `STT_LANGUAGE=fa`
selects Persian. This STT transport is not a generic LLM route.

| Setting | Default | Notes |
| --- | --- | --- |
| `SPEECHMATICS_API_KEY` | empty | required for Speechmatics |
| `SPEECHMATICS_MODEL` | `enhanced` | batch API |
| `DEEPGRAM_API_KEY` | empty | required for Deepgram |
| `DEEPGRAM_MODEL` | `nova-3` | Persian-capable model |
| `DEEPGRAM_BASE_URL` | `https://api.deepgram.com/v1` | configurable base |
| `STT_OPENAI_BASE_URL` / `STT_OPENAI_API_KEY` | empty / empty | separately configured gateway |
| `STT_OPENAI_MODEL` | `whisper-1` | model on that gateway |
| `STT_OPENAI_MAX_UPLOAD_BYTES` | 25000000 | decimal upload ceiling; compatible gateways may differ |
