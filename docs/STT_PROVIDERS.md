# Speech-to-text providers

`STT_PROVIDERS` is the ordered failover list. A provider is used only when its key is set, it is not cooling down and the file fits its upload limit.

| Provider | Slug | Key and model | Endpoint / Persian note |
| --- | --- | --- | --- |
| Speechmatics | `speechmatics` | `SPEECHMATICS_API_KEY`, `SPEECHMATICS_MODEL=enhanced` | `SPEECHMATICS_BASE_URL`; `enhanced` and `standard` support Persian. |
| Deepgram | `deepgram` | `DEEPGRAM_API_KEY`, `DEEPGRAM_MODEL=nova-3` | `DEEPGRAM_BASE_URL`; Nova-2 does not support Persian. |
| OpenAI-compatible | `openai_compatible` | `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL` | Set `STT_OPENAI_BASE_URL`; upload cap is `STT_OPENAI_MAX_UPLOAD_BYTES` (25 MB by default). The gateway must support audio transcription. |

The default language is `STT_LANGUAGE=fa`; the default provider order is `speechmatics,deepgram,openai_compatible`. Review exact URLs and limits in `.env.example`.

## Failover behavior

The whole chain shares **one deadline**, `STT_JOB_TIMEOUT` (default six hours): each provider attempts with the time that is left, and a provider is not started on an exhausted budget. A submit is **never repeated** — retrying would re-upload the whole file, which for a long lecture means gigabytes per attempt — so a transient submit failure (rate limit, server error, dropped connection) fails over to the next provider in the chain instead. Only *idempotent reads* are retried: the Speechmatics status and transcript `GET`s are retried a bounded number of times (`MAX_TRANSIENT_RETRIES`), respecting a bounded `Retry-After`, inside the same deadline. Permanent request or authentication errors move to the next provider. Files are **not chunked**; if no provider accepts the file, the job fails and its pre-delivery credit hold is released. Empty/unusable transcripts are not treated as success.

`STT_TRANSFER_TIMEOUT_SECONDS` (default `600`) bounds one upload/download transfer, never the provider's processing time: a stalled socket fails the transfer and the chain moves on instead of holding a worker for the rest of the job budget. `MAX_STT_RESPONSE_BYTES` (default `64 MiB`) caps the response body decoded into memory, so a corrupted or hostile answer cannot exhaust the worker. Audio uploads use 1 MiB file-stream buffering, response bodies are read in 256 KiB chunks and parsed with `orjson`, and Speechmatics status polling ramps adaptively (`0.25x` → `0.5x` → `0.75x` → `1.0x` of `STT_POLL_INTERVAL`) so short clips finish faster without increasing steady-state poll load. When `LLM_PREWARM=true`, the first configured STT provider's host is pre-warmed alongside the first note provider's host when the shared HTTP session is created.

Audio is prepared *for the chain*, not for `MAX_FILE_SIZE`: the merged/extracted track is sized to the largest upload ceiling among the configured providers (Opus instead of WAV when the raw PCM would not fit), so a deployment whose only provider accepts 25 MB never produces a file that every provider must skip. A provider skipped for cooldown or upload size is reported to the user as temporarily unavailable, not as a transcription failure; the abandoned Speechmatics job is deleted so it cannot keep running (and billing) after the bot gave up on it.

Only the first audio track of a multi-track file or embedded clip is transcribed. A second track (a second microphone, a dubbed audio track) is ignored without a notice.

Run `python -m gamas_bot --check` to validate local settings. On cPanel, `python -m scripts.cpanel_preflight` also checks outbound access. Logs show provider, model and error class, not audio or transcript content. See [Troubleshooting](TROUBLESHOOTING.md) for common errors.
