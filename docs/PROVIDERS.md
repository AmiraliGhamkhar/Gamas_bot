# Providers

## Approved text-generation routes

For the results-screen and billing behavior after transcription, see
[Features](FEATURES.md#audio-and-video) and [Billing](BILLING.md#transcription-charges).

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
skip to the next configured route. Educational source content is not classified
to block or exclude a configured provider.

### Structured output and model capabilities

Per-model and per-provider wire contracts live in `gamas_bot/llm_profiles.py`
(`MODEL_PROFILES` and `PROVIDER_PROFILES`), so new models or providers can be
added via `register_model_profile`, `register_provider_profile`, and
`register_llm_dialect` without rewriting the transport loop.

- **Gemini (`gemini-3.5-flash-lite`)**: Native `generateContent` /
  `streamGenerateContent?alt=sse` with `x-goog-api-key` auth, `systemInstruction`,
  `generationConfig.maxOutputTokens` clamped to `65536`,
  `generationConfig.responseFormat.text` (`mimeType: "APPLICATION_JSON"` plus
  optional lowercase JSON Schema with unsupported keywords stripped), and
  `generationConfig.thinkingConfig.thinkingLevel` (`MINIMAL` default, `LOW`,
  `MEDIUM`, `HIGH`). Deprecated sampling parameters (`temperature`, `topP`,
  `topK`), `candidateCount`, and prefilled `model` turns are never sent.
- **AvalAI (`deepseek-v4.1-flash`)**: OpenAI-compatible
  `POST /v1/chat/completions` (`Authorization: Bearer ...`) with output clamped
  to `393216`, portable non-strict `response_format.json_schema` and a bounded
  fallback ladder (`json_schema` → `json_object` → prompt-only), implicit
  prompt-cache usage logging (`cached_tokens` + `avalai-request-id`), and
  `reasoning_content` exhaustion detection. Unverified `reasoning_effort` and
  `stream_options` fields are omitted per AvalAI's DeepSeek V4.1 Flash
  documentation.
- **OpenRouter (`nvidia/nemotron-3.5-super-vl:free`)**: OpenAI-compatible
  `POST /api/v1/chat/completions` with output clamped to `131072`,
  `stream_options.include_usage`, optional `provider.sort="throughput"`,
  `response_format: {"type": "json_object"}` on `:free` endpoints (preventing
  HTTP 404 endpoint filtering while still enforcing valid JSON), and
  OpenRouter's unified `reasoning` object when `NOTE_REASONING_EFFORT` is set
  (`{"enabled": false}` for `none`, or `{"effort": "<level>", "exclude": true}`
  for `minimal`/`low`/`medium`/`high`/`xhigh`/`max`). Response parsing inspects
  `reasoning`, `reasoning_content`, `reasoning_details`, and
  `completion_tokens_details.reasoning_tokens` to detect output-budget
  exhaustion.

Streaming, usage parsing (including `cached_tokens` and `reasoning_tokens`),
finish-reason/truncation checks, and bounded local JSON/domain validation remain
in place. A malformed or truncated answer is never silently accepted or
delivered as a partial booklet. Healthy requests are not hedged, even if the
legacy hedge setting is nonzero.

The checked-in reference profiles distinguish model context, input and output
caps from application budgets:

| Profile | Reference model | Reference context / input tokens | Reference output cap |
| --- | --- | --- | --- |
| Gemini | `gemini-3.5-flash-lite` | 1048576 / 1048576 | 65536 |
| AvalAI | `deepseek-v4.1-flash` | 1000000 / 1000000 | 393216 |
| OpenRouter | `nvidia/nemotron-3.5-super-vl:free` | 1000000 / 1000000 | 131072 |

These are repository metadata, **not** current provider/account guarantees.
The capacity registry matches the exact provider/model pair; unknown overrides
use configurable deployment ceilings rather than inheriting these limits. The
native Google route can use [`models.countTokens`](https://ai.google.dev/api/tokens)
with the sanitized generation request; failure or an unsupported endpoint falls
back to estimation. The counter is not sent to custom proxies.
Operating defaults remain **131072 context / 64000 chunk characters / 16384
output**, with `NOTE_COMPILE=false`; never substitute a reference context window
for the output budget. Verify current model documentation and entitlement
before changing a deployment. Offline tests do not verify live compatibility.

### Quota budgets and reliability

AvalAI defaults: **35 RPM, 750000 TPM, 3 in flight**. OpenRouter defaults:
**10 RPM, 15 RPD, 1 in flight**. Gemini defaults to **1 in flight** with
RPM/TPM/RPD unset (zero); configure those from the actual AI Studio project,
not an assumed free-tier guarantee. The local pool and the shared ledger
both use the tighter of the provider limit and the primary/fallback limit
(defaults 3/1), so an in-process job can wait for a real provider slot instead
of being admitted locally and immediately denied by the ledger.

The shared SQLite/WAL ledger uses `BEGIN IMMEDIATE` to reserve request/token
budgets and in-flight capacity atomically across processes sharing the same
database. Reservations include conservative input estimates and the full output
cap plus a bounded retry allowance against RPM/RPD. Reported total usage is
reconciled; absent usage on success retains the full token estimate. Failure
releases unused tokens and in-flight capacity; request allowances remain
counted conservatively. Conservative reservations can underutilize budget.
Bounded transport/compatibility retries still consume provider requests.
This ledger is separate from user credits: media keeps its existing single
settlement by transcription duration, regardless of note retries or fallback.

The SQLite circuit breaker opens after three consecutive transient/auth
failures and permits one recovery probe after cooldown. Invalid requests and
context/output-budget errors do not count as provider-health failures. Metrics
aggregate task/provider/model counts, outcomes, estimates, usage and latency
without storing prompts or user identifiers. Internal `llm request` records
cover each completion attempt and native token-count request: stage, provider,
model, retry count (within that provider), duration, error category, and actual
input/output counts when provided. Otherwise the client estimates from the
submitted request or received completion text; an unavailable output is logged
as unknown, not as usage equal to the reserved maximum. `BOOKLET_PLAN` identifies
optional LLM planning separately from `BOOKLET_GENERATION`, `BOOKLET_COMPILE`,
`BOOKLET_VERIFY`, and `BOOKLET_REPAIR`. Booklet renderers make no provider calls;
quiz and flashcard routes remain independent. Bounded booklet chunk/split/repair
and grounding/completeness checks are unchanged.

### Prompt handling

There is no document-content sensitivity classifier or route gate: the configured
providers may receive educational source text in the task-specific attempt order.
This is not de-identification or a consent guarantee. Before a request is built,
the client removes configured provider/Telegram credentials, values of
secret-named environment variables, common API/bearer/Telegram token patterns,
and explicitly labelled password/key/secret assignments from prompt text.
Names, identifiers, case narratives, email addresses and other ordinary source
content are otherwise passed through unchanged. Review provider terms and obtain
any consent your deployment requires; never use live credentials or real user
material in tests.

### Booklet acceptance

A rendered booklet must be **faithful and structurally valid**; only a booklet
that also meets the completeness checks is announced as complete. Long-source
metadata is generated per chunk and merged in source order, so the optional
`NOTE_COMPILE` pass is not required for a summary, key points or glossary. The
editorial pass receives the draft and a structural plan, not the raw source.
It returns a patch: complete ID permutations, source-preserving headings,
extractive summaries/comparisons, exact deduplication, or structural links.
Detected literal value variants are possible conflicts, not factual verdicts;
both variants remain. Large drafts are edited in whole-section topic groups,
then chapter and final outlines. Over-budget or invalid proposals never
truncate or replace the approved baseline. A failed map unit uses its local
source-preserving fallback when enabled; otherwise the booklet is withheld
rather than silently delivering only the successful sections.

| Outcome | Rule | Result |
| --- | --- | --- |
| Recovered/scrubbed | Unsupported signals or claims are removed locally; if that cannot leave a safe booklet, the default source-only fallback is audited instead | Safe booklet delivered as `partial`, with a `notes_recovered` or `notes_degraded` notice |
| Delivered, incomplete | The safe booklet has unresolved plan, structural, or evidence findings after bounded repair | Booklet delivered as `partial`; the notice names the gap (or says coverage is incomplete) |
| Structure unavailable | The source is unusable or no renderable structure can be recovered | Raw transcript retained; no booklet is rendered, and the results screen explains the failure |
| Delivered, complete | Mandatory structure is valid, no fabricated signal or unsupported claim remains, and no diagnostic completeness gap remains | `done` |

Synthesis acceptance checks actual educational items and their owning source
references rather than section or character counts. This protects generated
content, not a claim that the baseline covered every source fact. Source
omissions are measured separately by the evidence and completeness audit.

The deterministic report is available at `StructuredNotes.verification_report`
in memory, or via `gamas_bot.booklet_verification.verify_booklet` without any
provider. `passed` means no findings remain under the policy; the scores are
ratios of explicit topic/dimension/fact, structural, and evidence checks, not
arbitrary acceptance thresholds. With no plan, lexical/unit coverage is an
informational fallback score. Deterministic word overlap is an evidence screen,
not semantic entailment. Strict `repair` mode can ask the provider about
ambiguous high-risk, quantitative, definition or procedure claims using exact
local excerpts. A reply cannot mark unreviewed or unresolved claims as supported.
Only stage, counts and status are logged; the report carries IDs rather than
claim or source text. Stored booklet JSON and provider routing order are unchanged.

### Request diagnostics

A failed route logs `note provider failed; trying the next one provider=<slug>
error=<ExceptionClass> detail=<sanitized>`, where the detail carries the HTTP
status and the provider's own bounded message. When every configured route fails,
one `every configured note provider failed task=… categories=… detail=…` line
lists the whole chain. The message shown to the user names only the classes
(`AvalAI: LLMAuthError؛ Gemini: LLMResponseError`), because a provider may echo
credential-like text in its error body; the sanitized details stay in the log.
A log detail is reduced to printable ASCII, so HTTP status, error code and
request id survive while any echoed lecture text (or credential-shaped string)
cannot reach `bot.log`.
Truncated (`finish_reason=length`), empty and malformed answers raise
`LLMResponseError` and fall through to the next eligible route instead of
producing a partial booklet.

### Safe probes

```bash
python -m scripts.probe_note_providers --show-config
```

`--show-config` is offline: it shows the configured provider chain and effective
output budgets without printing credentials. Task priorities can reorder the
configured chain while keeping OpenRouter last. Without the flag, the script makes metadata
requests (models/entitlement/limits), **not** generation requests. Metadata PASS
does not establish that a model can generate usable notes.

`python -m scripts.probe_note_providers --generate SYNTHETIC_TRANSCRIPT.txt`
is a separate, explicit operator action that sends one map-style request per
configured route, with bounded retries, and **consumes quota**. Use synthetic
text only. Neither live metadata nor generation probes belong in
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
