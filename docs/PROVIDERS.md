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

`NOTE_API_PROVIDER` selects `gemini`, `anthropic`, `openai_compatible` or
`disabled`. The layer is provider-neutral: base URL, model, timeout, retries and
extra headers are configuration, not code.

* `NOTE_API_JSON_MODE` opts into `response_format={"type":"json_object"}` for
  gateways that implement it; the strict system prompt remains the default.
* `NOTE_MODE` (`full`/`standard`/`summary`) is the only intentional compression.
* `NOTE_REPAIR_ENABLED` adds a second pass **only** when the deterministic QA
  gate detects real loss; the repaired notes are accepted only when measurably
  better.
* `NOTE_GLOBAL_CONTEXT_ENABLED` adds the outline/compilation layer for
  multi-part lectures. Single-part lectures still cost one call per chunk.
* Authentication headers in `NOTE_API_EXTRA_HEADERS_JSON` are dropped with a
  warning: an operator-supplied header must never be able to override a
  provider credential.

## Provider health

`🩺 وضعیت سرویس‌ها` (or `/health`) checks every configured engine and shows a
masked key, HTTP status and latency. Checks are manual and cached: opening the
panel never hammers a provider and redrawing never calls the network again. No
key material is ever displayed beyond a masked tail.
