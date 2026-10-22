# Speech-to-text providers

`STT_PROVIDERS` is the ordered failover list. A provider is used only when its key is set, it is not cooling down and the file fits its upload limit.

| Provider | Slug | Key and model | Endpoint / Persian note |
| --- | --- | --- | --- |
| Speechmatics | `speechmatics` | `SPEECHMATICS_API_KEY`, `SPEECHMATICS_MODEL=enhanced` | `SPEECHMATICS_BASE_URL`; `enhanced` and `standard` support Persian. |
| Deepgram | `deepgram` | `DEEPGRAM_API_KEY`, `DEEPGRAM_MODEL=nova-3` | `DEEPGRAM_BASE_URL`; Nova-2 does not support Persian. |
| OpenAI-compatible | `openai_compatible` | `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL` | Set `STT_OPENAI_BASE_URL`; upload cap is `STT_OPENAI_MAX_UPLOAD_BYTES` (25 MB by default). The gateway must support audio transcription. |

The default language is `STT_LANGUAGE=fa`; the default provider order is `deepgram,speechmatics,openai_compatible`. Review exact URLs and limits in `.env.example`.

**Order is latency policy, not preference.** Only providers with a key are attempted, and the first one that qualifies serves the job — so an unconfigured slug is skipped, and listing a slow engine first makes it the default for every upload. The shipped order therefore leads with Deepgram (Nova-3, an independent benchmark measured ~351× real time against ~24× for Speechmatics `enhanced`) and keeps Speechmatics second: a deployment whose only key is `SPEECHMATICS_API_KEY` behaves exactly as before, and a Deepgram outage degrades to the previous path instead of failing. `python -m gamas_bot --check` prints an advisory warning when `STT_PROVIDERS` starts with `speechmatics` while `SPEECHMATICS_MODEL` is `enhanced` or `standard` and Deepgram is also configured — the bot still runs, but every job would wait for the slower engine.

## Failover behavior

The whole chain shares **one deadline**, `STT_JOB_TIMEOUT` (default six hours): each provider attempts with the time that is left, and a provider is not started on an exhausted budget. A submit is **never repeated** — retrying would re-upload the whole file, which for a long lecture means gigabytes per attempt — so a transient submit failure (rate limit, server error, dropped connection) fails over to the next provider in the chain instead. Only *idempotent reads* are retried: the Speechmatics status and transcript `GET`s are retried a bounded number of times (`MAX_TRANSIENT_RETRIES`), respecting a bounded `Retry-After`, inside the same deadline. Permanent request or authentication errors move to the next provider. Files are **not chunked**; if no provider accepts the file, the job fails and its pre-delivery credit hold is released. Empty/unusable transcripts are not treated as success.

`STT_TRANSFER_TIMEOUT_SECONDS` (default `600`) bounds one upload/download transfer, never the provider's processing time: a stalled socket fails the transfer and the chain moves on instead of holding a worker for the rest of the job budget. `MAX_STT_RESPONSE_BYTES` (default `64 MiB`) caps the response body decoded into memory, so a corrupted or hostile answer cannot exhaust the worker. Audio uploads use 1 MiB file-stream buffering, response bodies are read in 256 KiB chunks and parsed with `orjson`, and Speechmatics status polling ramps adaptively (`0.25x` → `0.5x` → `0.75x` → `1.0x` of `STT_POLL_INTERVAL`) so short clips finish faster without increasing steady-state poll load.

`STT_STATUS_LONG_POLL` (default `25` seconds, `0` disables) is sent as `wait` on the Speechmatics submit and status reads. The API holds the request open until the job changes state — and on `POST /jobs` it returns the finished transcript inline (`format=json-v2`), so a job that finishes inside the window costs **one request instead of a submit + status poll + transcript download**. If the window expires first the response says `created` and the normal polling path continues; if a `done` response arrives without the embedded document, the transcript endpoint is asked once. Melia-1 can return a two-hour lecture on the submit call, which is why the phase exists. When `LLM_PREWARM=true`, the first configured STT provider's host is pre-warmed alongside the first note provider's host when the shared HTTP session is created.

Audio is prepared *for the chain*, not for `MAX_FILE_SIZE`: the merged/extracted track is sized to the largest upload ceiling among the configured providers (Opus instead of WAV when the raw PCM would not fit), so a deployment whose only provider accepts 25 MB never produces a file that every provider must skip. A provider skipped for cooldown or upload size is reported to the user as temporarily unavailable, not as a transcription failure; the abandoned Speechmatics job is deleted so it cannot keep running (and billing) after the bot gave up on it.

Only the first audio track of a multi-track file or embedded clip is transcribed. A second track (a second microphone, a dubbed audio track) is ignored without a notice.

Run `python -m gamas_bot --check` to validate local settings. On cPanel, `python -m scripts.cpanel_preflight` also checks outbound access. Logs show provider, model and error class, not audio or transcript content. See [Troubleshooting](TROUBLESHOOTING.md) for common errors.
