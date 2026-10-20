# Speech-to-text providers

`STT_PROVIDERS` is the ordered failover list. A provider is used only when its key is set, it is not cooling down and the file fits its upload limit.

| Provider | Slug | Key and model | Endpoint / Persian note |
| --- | --- | --- | --- |
| Speechmatics | `speechmatics` | `SPEECHMATICS_API_KEY`, `SPEECHMATICS_MODEL=enhanced` | `SPEECHMATICS_BASE_URL`; `enhanced` and `standard` support Persian. |
| Deepgram | `deepgram` | `DEEPGRAM_API_KEY`, `DEEPGRAM_MODEL=nova-3` | `DEEPGRAM_BASE_URL`; Nova-2 does not support Persian. |
| OpenAI-compatible | `openai_compatible` | `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL` | Set `STT_OPENAI_BASE_URL`; upload cap is `STT_OPENAI_MAX_UPLOAD_BYTES` (25 MB by default). The gateway must support audio transcription. |

The default language is `STT_LANGUAGE=fa`; the default provider order is `speechmatics,deepgram,openai_compatible`. Review exact URLs and limits in `.env.example`.

## Failover behavior

Temporary network/rate-limit/server errors are retried a bounded number of times, then the next provider is tried. Permanent request or authentication errors move to the next provider. Files are **not chunked**; if no provider accepts the file, the job fails and its pre-delivery credit hold is released. Empty/unusable transcripts are not treated as success.

Run `python -m gamas_bot --check` to validate local settings. On cPanel, `python -m scripts.cpanel_preflight` also checks outbound access. Logs show provider, model and error class, not audio or transcript content. See [Troubleshooting](TROUBLESHOOTING.md) for common errors.
