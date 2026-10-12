# Providers

## Speech-to-text

`gamas_bot/stt.py` keeps a provider-neutral interface: one `transcribe()` call
receives a settings object and an audio path and returns a transcript plus the
engine that produced it. The engine name in the result is **the engine that
actually ran** — a fallback never reports itself as the primary.

| Provider | Notes |
| --- | --- |
| Speechmatics (batch) | `SPEECHMATICS_MODEL` selects `enhanced` (default), `standard`, `melia-1` or `oak-1` |
| Deepgram (pre-recorded) | `DEEPGRAM_MODEL` (default `nova-3`; Persian needs Nova-3) |
| OpenAI-compatible | `STT_OPENAI_BASE_URL` + `STT_OPENAI_MODEL`, for OpenAI/self-hosted gateways |

There are exactly these three engines; the v2 provider platform (native Groq,
Gemini Transcribe, AssemblyAI, Gladia, Google Cloud, Watson, Azure, AWS, Soniox,
ElevenLabs adapters, routes, quotas and the quality gate) was removed in v3. The
current behaviour, settings and limits are documented in
[STT_PROVIDERS.md](STT_PROVIDERS.md).

Behaviour:

* **Provider order is the failover policy**: `STT_PROVIDERS` is tried in order,
  and only providers with credentials are attempted.
* **Bounded retries** for transient failures (408/425/429/5xx, network, timeout)
  with exponential backoff, honouring `Retry-After` up to
  `MAX_RETRY_AFTER_SECONDS` (60 s). Permanent rejections are never retried.
* **Cooldowns**: 15 minutes after a 401/403, 60 seconds after an exhausted
  transient budget; the log and the error message name every skipped provider.
* **Size routing**: a file above a provider's direct-upload ceiling is routed to
  the next configured engine. Audio is never chunked, because splitting would
  cost word context at every boundary.
* Transcript content and secrets are never logged.

Whole-file transcription only: no chunking in the STT layer.

## Note generation

`gamas_bot/llm.py` speaks **four** request styles behind one interface:
OpenAI-compatible chat completions (`nara`, `openrouter`, `openai_compatible`),
Gemini `generateContent`, and Anthropic `messages`. The v2 provider platform
(15-provider registry, encrypted vault, quotas, routes, attestations,
`gamas_bot/ai/`) was removed in v3; what exists now is a short ordered chain:

| Provider | Base URL (default) | Credential | Model default |
| --- | --- | --- | --- |
| Nara (NaraRouter) | `https://router.bynara.id/v1` | `NARA_API_KEY` | `agnes-2.5-flash` (its published free-plan list) |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | `openrouter/free` (its documented free router) |
| OpenAI-compatible | `NOTE_BASE_URL` (or `https://api.openai.com/v1`) | `NOTE_API_KEY` (optional for a local gateway) | `NOTE_MODEL` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` (or `NOTE_API_KEY` when selected) | `gemini-2.5-flash` |
| Anthropic | `https://api.anthropic.com/v1` | `NOTE_API_KEY` | none — set `NOTE_MODEL` |

Behaviour:

* **`NOTE_PROVIDERS` is the failover policy** (default `nara,openrouter`). Each
  provider is attempted in order and only when it has its own credential; a
  provider with no key is skipped, never assumed.
* **Bounded retries per provider** for transient failures (408/425/429/5xx,
  network, timeout) with backoff and `Retry-After`; a second malformed answer
  ends that provider's turn. A permanent 4xx is never re-sent to the same
  provider.
* **Ordered failover** on authentication, rejection, unusable answers and
  context overflow. The failure class of the first provider is preserved, so a
  context overflow still reaches the note pipeline as one.
* **Context-aware sizing**: `NOTE_CONTEXT_TOKENS` minus the output budget, the
  system prompt and the contextual material becomes the chunk budget, and the
  document is split at paragraph/sentence boundaries. A request the model still
  rejects as oversized is halved and retried (bounded depth), never re-sent
  unchanged.
* **Structured output stays conservative**: `NOTE_JSON_MODE` is only sent to
  providers that document `response_format` (OpenRouter and generic
  OpenAI-compatible gateways — not NaraRouter). Gemini always asks for its
  native JSON.
* **Backend metadata is scrubbed** from student-facing notes (provider
  self-references, key/id-shaped tokens, diagnostic clusters, meta phrases)
  while medical, scientific and technical terms are preserved.
* A note failure never costs the transcript: the raw text is delivered (and
  persisted) before note generation starts.

No key is ever logged or stored in the database, and no note request is made
before the user's upload has been transcribed and delivered.

## Provider diagnostics

There is no in-bot provider-health panel in v3 and no background probing: a
provider is exercised only by a real job, and a redrawing screen never performs
network calls. Diagnose with `python -m gamas_bot --check` (offline, reports
which note providers are eligible), `python -m scripts.cpanel_preflight`
(outbound reachability of the configured note and STT hosts) and the job log,
which names every provider that was attempted, skipped for missing credentials,
or left behind after a failure — with no key material in it.
