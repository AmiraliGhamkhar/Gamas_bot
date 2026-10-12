# Note-generation providers

`gamas_bot/llm.py` speaks three request styles behind one interface:
OpenAI-compatible chat completions (`nara`, `openrouter`, `openai_compatible`),
Gemini `generateContent`, and Anthropic `messages`.

| Provider | Base URL default | Credential | Model default |
| --- | --- | --- | --- |
| NaraRouter (`nara`) | `https://router.bynara.id/v1` | `NARA_API_KEY` | `agnes-2.5-flash` |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | `openrouter/free` |
| OpenAI-compatible | `https://api.openai.com/v1` (or `NOTE_BASE_URL`) | `NOTE_API_KEY` (optional for a local gateway) | `NOTE_MODEL` (`gpt-4o-mini`) |
| Gemini | `https://generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` (or `NOTE_API_KEY`) | `gemini-2.5-flash` |
| Anthropic | `https://api.anthropic.com/v1` | `NOTE_API_KEY` | none — set `NOTE_MODEL` |

The defaults are the models the providers publish as free
([NaraRouter plans](https://router.bynara.id/api/plans),
[OpenRouter free router](https://openrouter.ai/docs/cookbook/get-started/free-models-router-playground)).
Changing `NARA_MODEL` or `OPENROUTER_MODEL` is an explicit operator decision; the
bot never relabels a model as free.

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

There is no in-bot provider panel and no background probing: a provider is
exercised only by a real job.

```bash
python -m gamas_bot --check          # offline: which providers are eligible
python -m scripts.cpanel_preflight   # outbound reachability of the note hosts
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
