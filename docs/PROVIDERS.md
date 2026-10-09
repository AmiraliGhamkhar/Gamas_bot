# Providers

## Speech-to-text

`gamas_bot/stt.py` keeps a provider-neutral interface: one `transcribe()` call
receives a settings object and an audio path and returns a transcript plus the
engine that produced it. The engine name in the result is **the engine that
actually ran** — a fallback never reports itself as the primary.

| Provider | Notes |
| --- | --- |
| Speechmatics (batch) | default; `SPEECHMATICS_OPERATING_POINT` selects `enhanced`, `standard`, `melia-1` or `oak-1`; custom dictionary via `SPEECHMATICS_ADDITIONAL_VOCAB` |
| Deepgram | `DEEPGRAM_MODEL` (default `nova-3`) |
| OpenAI-compatible | `STT_OPENAI_BASE_URL` + model, for OpenAI/Groq/self-hosted gateways |

Behaviour:

* **One session per job** with explicit connect/read timeouts, so a job reuses
  one connection for upload, polling and download.
* **Bounded retries** for transient failures (429/5xx/network) with exponential
  backoff bounded by `STT_RETRY_MAX_DELAY_SECONDS`, honouring `Retry-After`.
* **Fallback** to the next configured engine when the primary fails and
  `STT_FALLBACK_ENABLED` is true.
* **Size routing**: a file above a provider's direct-upload limit is routed to
  another configured engine. Audio is never chunked, because splitting would
  cost word context at every boundary.
* **Credential rotation**: encrypted keys are tried in order; a 401/403
  quarantines a key, a 429 puts it in cooldown.
* Transcript content and secrets are never logged.

Whole-file transcription only: no chunking in the STT layer.

## Note generation

Note generation runs on the **Gamas AI Provider Platform**
(`gamas_bot/ai/`, see [docs/AI_PROVIDERS.md](AI_PROVIDERS.md) for the full
reference): a 15-provider registry (`gemini`, `nara`, `groq`, `openrouter`,
`mistral`, `sambanova`, `zai`, `nvidia`, `cloudflare`, `huggingface`,
`alibaba`, `cohere`, `cerebras`, `openai_compatible`, `anthropic`) with
capability-aware JSON strategies, provider-level failover and credential
rotation. Free-tier is the default: `AI_FREE_ONLY=true` blocks non-free
providers; `AI_ALLOW_PAID_FALLBACK` (default `false`) can append paid legs
strictly **after** every free leg. Z.AI’s selected GLM models are listed Free
in its pricing table, but Terms §III.06 restrict specified education-related
decision-making and services requiring educational qualifications or
professional review. This is not a blanket ban on all educational use; Gamas
conservatively blocks student-note routes pending scope clarification, regardless
of model price or key billing attestation.

Backward compatibility is preserved at the configuration and pipeline
boundary:

* `NOTE_API_PROVIDER=openai_compatible` + `NOTE_API_BASE_URL=https://router.bynara.id/v1`
  (the production NaraRouter deployment) is still resolved as NaraRouter and
  remains seeded first; `NOTE_API_MODEL=agnes-3-flash` is preserved. Migration
  007 deliberately does **not** claim that model is free: the currently
  published Nara Free-plan list names `agnes-2.5-flash`, not `agnes-3-flash`.
  Existing environment keys stay configured and work for read-only checks, but
  generation requires copying the same secret into the encrypted vault and
  explicitly attesting its billing state.
* `NOTE_API_PROVIDER=gemini` keeps using the native `generateContent` adapter;
  only exact reviewed model capabilities enable a Gemini `responseSchema`.
* `NOTE_API_JSON_MODE=1` remains supported for the legacy generic
  OpenAI-compatible route. It does not override a provider/model’s verified
  strategy or leak JSON-object mode to NaraRouter.
* `AI_ROUTING_ENABLED=false` bypasses the new router and retains the legacy
  single-provider code path, subject to that path’s existing deployment settings.

The single-provider legacy knobs stay authoritative in legacy mode:
`NOTE_API_BASE_URL`, `NOTE_API_MODEL`, `NOTE_API_TIMEOUT_SECONDS`,
`NOTE_API_RETRIES`, `NOTE_API_MAX_OUTPUT_TOKENS`,
`NOTE_API_EXTRA_HEADERS_JSON` (auth headers are still stripped with a warning).
When routing is enabled, timeout/retry/output values remain upper bounds on
provider-specific profiles; they may tighten, but never inflate, a provider
budget. The full billing-attestation and model-eligibility rules are in
[AI_PROVIDERS.md](AI_PROVIDERS.md).

* `NOTE_MODE` (`full`/`standard`/`summary`) is the only intentional compression.
* `NOTE_REPAIR_ENABLED` adds a second pass **only** when the deterministic QA
  gate detects real loss; the repaired notes are accepted only when measurably
  better. In routing mode the primary provider's profile decides
  outline/repair/compile enablement.
* `NOTE_GLOBAL_CONTEXT_ENABLED` adds the outline/compilation layer for
  multi-part lectures. Single-part lectures still cost one call per chunk.
* Chunk sizing is token-aware: the active provider's profile budget
  (see `gamas_bot/ai/profiles.py`) converts to a character budget via
  `ai/tokens.py`; `note_chunk_chars()` caps it at the legacy ceiling when no
  router context is bound.

## Provider health

`🩺 وضعیت سرویس‌ها` (or `/health`) checks every configured engine and shows a
masked key, HTTP status and latency. Checks are manual and cached: opening the
panel never hammers a provider and redrawing never calls the network again. No
key material is ever displayed beyond a masked tail.
