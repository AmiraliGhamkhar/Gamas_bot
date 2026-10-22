# Speech-to-text providers

`STT_PROVIDERS` is the ordered failover list. Only providers with a key are attempted; the first
eligible one serves the job.

| Provider | Slug | Settings |
| --- | --- | --- |
| Deepgram | `deepgram` | `DEEPGRAM_API_KEY`, `DEEPGRAM_MODEL=nova-3`, `DEEPGRAM_BASE_URL` |
| Speechmatics | `speechmatics` | `SPEECHMATICS_API_KEY`, `SPEECHMATICS_MODEL=enhanced`, `SPEECHMATICS_BASE_URL` |
| OpenAI-compatible | `openai_compatible` | `STT_OPENAI_BASE_URL`, `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL`, `STT_OPENAI_MAX_UPLOAD_BYTES` |

Default language: `STT_LANGUAGE=fa`. Default order: `deepgram,speechmatics,openai_compatible`.
Deepgram needs Nova-3 for Persian; Nova-2 has no `fa` pack, so `--check` reports that pairing (advisory: another configured provider may still transcribe).

## Order is latency policy

The first provider with a key is the one that runs. A slow engine placed first becomes the default
for every upload, so `--check` prints an advisory warning when `speechmatics` leads with a slow
model while Deepgram is also configured. Only providers with a key are attempted, so the shipped
order still degrades safely for a Speechmatics-only deployment.

## Failover behavior

- The whole chain shares one deadline, `STT_JOB_TIMEOUT` (default 6 hours).
- A submit is **never repeated**: retrying would re-upload the whole file. A transient submit error
  fails over to the next provider instead. Only idempotent status/transcript reads are retried
  (`MAX_TRANSIENT_RETRIES`, bounded `Retry-After`).
- `STT_TRANSFER_TIMEOUT_SECONDS` (default 600) bounds one transfer, not provider processing time.
- `MAX_STT_RESPONSE_BYTES` (default 64 MiB) bounds the decoded response body.
- A provider skipped for cooldown or upload size is reported as temporarily unavailable, not as a
  failed transcription.
- Files are not chunked. If no provider accepts the file, the job fails and its hold is released.
  Empty transcripts are not treated as success.
- Only the first audio track is transcribed; other tracks are ignored.

## Long polling

`STT_STATUS_LONG_POLL` (default 25 s, `0` disables) is sent as `wait` on Speechmatics submit and
status requests. `POST /jobs` can return the finished transcript inline (`format=json-v2`), so a
job that finishes inside the window costs one request instead of submit + poll + download. If the
window expires first, normal polling continues.

## Upload sizing

Audio is prepared for the chain, not for `MAX_FILE_SIZE`: it is sized to the largest upload ceiling
among configured providers (Opus instead of WAV when needed), so a provider is not skipped for a
file that could have been compressed.

## Check

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
```

`--check` is offline. The preflight also tests outbound connectivity to each configured provider
host. Logs name the provider, model and error class — never audio or transcript content.
