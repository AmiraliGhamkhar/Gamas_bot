# Note-generation providers

`gamas_bot/llm.py` speaks three request styles behind one interface:
OpenAI-compatible chat completions (`nara`, `openrouter`, `openai_compatible`),
Gemini `generateContent`, and Anthropic `messages`.

| Provider | Base URL default | Credential | Model default |
| --- | --- | --- | --- |
| NaraRouter (`nara`) | `https://router.bynara.id/v1` | `NARA_API_KEY` | `agnes-2.5-flash` |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | `google/gemma-4-31b-it:free` |
| OpenAI-compatible | `https://api.openai.com/v1` (or `NOTE_BASE_URL`) | `NOTE_API_KEY` (optional for a local gateway) | `NOTE_MODEL` (`gpt-4o-mini`) |
| Gemini | `https://generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` (or `NOTE_API_KEY`) | `gemini-2.5-flash` |
| Anthropic | `https://api.anthropic.com/v1` | `NOTE_API_KEY` | none — set `NOTE_MODEL` |

The defaults are free models published by the providers
([NaraRouter plans](https://router.bynara.id/api/plans), the OpenRouter model
catalogue). Changing `NARA_MODEL` or `OPENROUTER_MODEL` is an explicit operator
decision; the bot never relabels a model as free.

## The default pair is window-matched

One global `NOTE_CONTEXT_TOKENS` sizes requests for whichever provider
answers, so the two defaults were chosen to serve the *same* window
(verified against the providers' published data on 2026-10-16):

| Default model | Served context | Completion cap | Why this one |
| --- | --- | --- | --- |
| `agnes-2.5-flash` (Nara) | 512K official reference; Nara's gateway cap is unpublished | 65.5K | The general instruct model on Nara's Free plan (7M tokens/day, 15 req/min) |
| `google/gemma-4-31b-it:free` (OpenRouter) | 262,144 (endpoint) | 32,768 | Pinned endpoint, `response_format`-capable, pre-trained on 140+ languages |

That is why the OpenRouter default is a pinned model and not the
`openrouter/free` router: the router selects a model per request, so the
window that will serve a request is unknown at sizing time (its pool currently
spans 66K–1M windows, with completion caps as low as 8,192). The operational
assumption is `NOTE_CONTEXT_TOKENS=131072` — half the matched served window —
with `NOTE_CHUNK_CHARS=64000` and `NOTE_MAX_OUTPUT_TOKENS=16384` (within both
completion caps). Re-verify the endpoints before changing any of the three:
Nara's authenticated `/v1/models`, OpenRouter's
`/api/v1/models/<model>/endpoints`, and `GET /api/v1/key` for the free-tier
daily request counter (20/min; 50/day, or 1,000/day after $10 of lifetime
credit purchases). Free rosters rotate; a model that leaves the free tier
fails as a provider error and the chain falls back, but the sizing assumption
must be revisited at the same time.

## Failover

* **`NOTE_PROVIDERS` is the policy** (default `nara,openrouter`). Providers are
  attempted in that order and only when they have their own credential.
* **Bounded retries per provider** for transient failures (408/425/429/5xx,
  network, timeout) with backoff and `Retry-After`; three attempts, then the next
  provider. Every request has its own 300 s timeout.
* **Ordered failover** on authentication failure, rejection and unusable answers.
  The failure class of the first provider is preserved, so a context overflow
  still reaches the note pipeline as one.
* **`NOTE_JSON_MODE`** sends `response_format` only to providers that document it
  (OpenRouter and generic OpenAI-compatible gateways — not NaraRouter). Gemini
  always asks for its native JSON.
* **`NOTE_REASONING_EFFORT`** (unset by default) sends `reasoning_effort` on
  the Nara and OpenRouter routes only — the two gateways verified for the
  field. A generic OpenAI-compatible gateway (whose model may reject the
  parameter) and the Gemini/Anthropic styles never receive it. Each completed request logs its
  served model and real token usage (`note provider usage …`), which is the
  data to recalibrate the chunk budget's character-to-token estimate from.
* **The compile (reduce) budget is token-derived** (`NOTE_COMPILE_MAX_CHARS=0`
  means automatic): a merged booklet larger than the derived budget — bounded
  by an absolute ceiling in `notes.py` — is delivered as the deterministic
  merge instead of being re-sent as one oversized request.

## Size failures

Two failures look similar and have opposite fixes, so they are separate classes:

| Class | Trigger | What the pipeline does |
| --- | --- | --- |
| `LLMContextOverflowError` | HTTP **413**, or a 400 whose text names the context window | splits the transcript in half and retries both halves (bounded depth) |
| `LLMOutputBudgetError` | answer cut off by the output budget — `finish_reason: "length"`, Gemini `MAX_TOKENS`, Anthropic `max_tokens` | does **not** retry; raises with a pointer to `NOTE_MAX_OUTPUT_TOKENS` |

NaraRouter reserves 413 for an oversized body ("The request body or input is too
large") and documents that reasoning models spend thinking tokens out of the same
`max_tokens` budget — which is how an answer comes back empty. Neither is fixed by
re-sending the same request, and neither is retried as if it were a malformed
answer.

A third case is retried once: a usable HTTP 200 whose text is not valid JSON gets
one "valid JSON only" reminder per provider, then the chain moves on. When the
chain runs out, the caller degrades to the raw transcript the user already holds.

## Diagnostics

There is no in-bot provider panel and no background probing: inside the bot, a
provider is exercised only by a real job. The one explicit exception is the
operator-run probe below, which is never invoked by the bot itself.

```bash
python -m gamas_bot --check          # offline: which providers are eligible
python -m scripts.cpanel_preflight   # outbound reachability of the note hosts
python -m scripts.probe_note_providers            # read-only: entitlements, served caps, free-tier counters
python -m scripts.probe_note_providers --generate lecture.txt   # opt-in: one real generation per provider (consumes quota)
```

The job log names every provider attempted, skipped for missing credentials, or
left behind after a failure — with no key material in it.

A rejection is logged with the provider's error `type` and, where the provider
supplies one, its `request_id`. NaraRouter's envelope
(`{"error": {"type", "message", "request_id"}}`) carries that id precisely so a
failure can be matched against its logs, so a `[request_id=req_…]` suffix is the
value to quote in a support ticket.

## Safety

* A note failure never costs the transcript: the raw text is delivered and
  persisted before note generation starts.
* Provider output is untrusted. Backend names, request ids, HTTP diagnostics and
  echoed credentials are scrubbed before a student-facing note is saved, while
  medical, scientific and technical terminology is preserved.
* No key is logged or stored in the database.

## Sources

* NaraRouter API: <https://router.bynara.id/docs>
* NaraRouter model list: <https://router.bynara.id/api/plans>
* OpenRouter: <https://openrouter.ai/docs>
* Gemini: <https://ai.google.dev/api/generate-content>
* Anthropic Messages: <https://docs.anthropic.com/en/api/messages>
