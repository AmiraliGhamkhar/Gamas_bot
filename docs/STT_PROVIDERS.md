# Speech-to-text providers

Configure at least one provider in `.env`. Keys are never stored in SQLite.

| Route | Key | Model / local slots |
| --- | --- | --- |
| `speechmatics` | `SPEECHMATICS_API_KEY` | `SPEECHMATICS_MODEL=enhanced`; 2 slots |
| `deepgram` | `DEEPGRAM_API_KEY` | `DEEPGRAM_MODEL=nova-3`; 4 slots |
| `openai_compatible` | `STT_OPENAI_API_KEY` | `STT_OPENAI_MODEL=whisper-1`; 2 slots |

```dotenv
STT_PROVIDERS=speechmatics,deepgram,openai_compatible
STT_FALLBACK_ENABLED=true
STT_LANGUAGE=fa
```

Keyless routes are skipped. Override endpoints with `SPEECHMATICS_BASE_URL`,
`DEEPGRAM_BASE_URL` or `STT_OPENAI_BASE_URL`; defaults and other settings are in
[`.env.example`](../.env.example). OpenAI-compatible uploads also obey
`STT_OPENAI_MAX_UPLOAD_BYTES` (25000000 by default). Persian Deepgram needs
Nova-3, not Nova-2.

## Routing and limits

- Use the first eligible route with capacity. Full primary slots overflow to
  available fallback capacity immediately; wait only when all routes are busy/cooling down.
- Default all-full wait: `STT_SLOT_WAIT_TIMEOUT_SECONDS=600`; attempt deadline:
  `STT_PROVIDER_TIMEOUT_SECONDS=1800`; chain deadline: `STT_JOB_TIMEOUT=21600` seconds.
- Auth, quota, rate-limit and transient errors can trigger failover. Permanently
  invalid media fails without wasting fallback uploads.
- Audio submission is not replayed on the same provider. Only idempotent reads retry.
- Transfers use `STT_TRANSFER_TIMEOUT_SECONDS=600`; response bodies are capped at
  `MAX_STT_RESPONSE_BYTES=67108864`. Files are not split into STT chunks.
- Only the first audio track is used. Empty transcripts fail. Unmeasurable duration
  is refused; holds release if the complete transcript cannot be delivered.

Run the [offline check](CONFIGURATION.md#validate). cPanel preflight also probes
connectivity but does not verify transcription quality or account entitlement.
Test short, non-sensitive audio before accepting real work.
