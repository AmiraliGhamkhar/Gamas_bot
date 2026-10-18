# Speech-to-text providers

The STT layer is `gamas_bot/stt.py`. There is no registry, route table or quota
platform: the `STT_PROVIDERS` list **is** the failover policy.

## Providers

| Provider | Slug | Credential | Model | Language sent |
| --- | --- | --- | --- | --- |
| Speechmatics Batch | `speechmatics` | `SPEECHMATICS_API_KEY` (Bearer) | `SPEECHMATICS_MODEL` (`enhanced`) | `STT_LANGUAGE` (`fa`) |
| Deepgram pre-recorded | `deepgram` | `DEEPGRAM_API_KEY` (`Authorization: Token …`) | `DEEPGRAM_MODEL` (`nova-3`) | `STT_LANGUAGE`, or `detect_language=true` for `auto` |
| OpenAI-compatible | `openai_compatible` | `STT_OPENAI_API_KEY` + `STT_OPENAI_BASE_URL` | `STT_OPENAI_MODEL` (`whisper-1`) | `STT_LANGUAGE`, omitted for `auto`/`multi` |

Endpoints (a full endpoint URL is also accepted, e.g.
`DEEPGRAM_BASE_URL=https://api.deepgram.com/v1/listen`):

* **Speechmatics** — `POST {SPEECHMATICS_BASE_URL}/jobs` (multipart: `config` JSON
  + `data_file`), then `GET /jobs/{id}`, then
  `GET /jobs/{id}/transcript?format=json-v2`.
* **Deepgram** — `POST {DEEPGRAM_BASE_URL}/listen`, audio as a binary body;
  `model`, `language`, `smart_format=true` and `punctuate=true` as query
  parameters.
* **OpenAI-compatible** — `POST {STT_OPENAI_BASE_URL}/audio/transcriptions`
  (multipart: `model`, `language`, `response_format=verbose_json`, `file`). A
  gateway that rejects `verbose_json` is retried once with
  `response_format=json`.

## Failover

1. The order is `STT_PROVIDERS` verbatim (default
   `speechmatics,deepgram,openai_compatible`). Unknown slugs are dropped at
   startup with a warning.
2. A provider is attempted only when it has credentials, is not in cooldown, and
   its upload ceiling covers the file. Skipping is logged, never silent.
3. Transient failures (408/425/429/5xx, network, timeout) are retried twice per
   provider — three attempts. `Retry-After` is honoured, clamped to 60 s;
   otherwise the delay is `2^n` capped at 15 s.
4. Permanent failures are never retried: any other 4xx, 401/403, and unusable
   payloads move straight to the next provider.
5. Cooldowns are in-memory per process: 15 minutes after an authentication
   failure, 60 seconds after an exhausted retry budget. If every provider is
   cooling down the job fails with a message naming each one.
6. A payload with no letter or digit at all is **not** a transcript: it fails
   over, because delivering and billing an empty `.txt` would report a failure as
   success. When every provider fails, the job fails with the collected reasons,
   the reserved credit is released and no file is delivered.

## Persian and model compatibility

Persian is the default (`STT_LANGUAGE=fa`), so these pairings matter:

* **Deepgram** — `fa` is a Nova-3 language. Nova-2 has no Persian pack and
  rejects `language=fa` with HTTP 400, which is why `DEEPGRAM_MODEL` defaults to
  `nova-3`. `auto` is sent as `detect_language=true`; omitting `language` would
  not detect anything, Deepgram defaults to English. Persian is in neither
  Deepgram's detection list nor its `multi` set.
* **Speechmatics** — `enhanced`, `standard`, `melia-1`, `oak-1`. The last two are
  batch-only multilingual: the config always sends `language=multi`, whatever
  `STT_LANGUAGE` says (`auto` included — it is mapped to `multi`, not rejected),
  and language packs are unavailable. `enhanced`/`standard` accept `fa` directly
  and are the only tiers supporting `language=auto`; on those tiers `auto`
  becomes `fa`.
* **OpenAI-compatible** — `language` is an ISO-639-1 code and is omitted for
  `auto`/`detect`/`multi`. `whisper-1` supports `verbose_json` (segment timings);
  the `gpt-4o-transcribe` family does not, hence the one retry.

`--check` warns when a Persian deployment's `DEEPGRAM_MODEL` cannot transcribe
`fa`. It is advisory — another provider in the chain may still serve the job.

## Limits

| Limit | Speechmatics | Deepgram | OpenAI-compatible |
| --- | --- | --- | --- |
| Direct upload | **< 1 GB** per `POST /jobs` | 2 GB | `STT_OPENAI_MAX_UPLOAD_BYTES` (25 MB) |
| Processing | polls for `STT_JOB_TIMEOUT` (21600 s) | ~10 min of processing can return 504 | request timeout is `STT_JOB_TIMEOUT` |
| Retention | 7 days, then 404 | inline response | provider-dependent |

Ceilings are enforced **before** upload, in decimal bytes. A file over one
provider's ceiling is routed to the next. Nothing is chunked — splitting audio
costs word context at every boundary, which is worse than failing over.

## Diagnosis

Every job carries its submission reference (`GMS-#####`) in the log as `job_id`.
Never logged: API keys, `Authorization` headers, audio bytes or transcript text.

| Symptom | Class | Action |
| --- | --- | --- |
| `هیچ سرویس تبدیل گفتاری پیکربندی نشده است` | configuration | set a key for a provider listed in `STT_PROVIDERS` |
| `اعتبارنامه را نپذیرفت (HTTP 401/403)` | credentials | replace the key; the provider cools down 15 minutes |
| `محدودیت نرخ درخواست را اعلام کرد (HTTP 429)` | rate limit | transient; retried, then fails over |
| `درخواست را رد کرد (HTTP 4xx)` | request/model/language | check the model/language pairing |
| `سقف حجم …MB دارد` | file too large | routed to the next provider |
| `در حال استراحت (…s)` | cooldown | wait, or fix the credential |
| `متن قابل استفاده‌ای تولید نکرد` | unusable response | provider outage or incompatible gateway |

```bash
python -m gamas_bot --check              # offline configuration check
python -m gamas_bot.media_worker check   # media dependencies
```

## Deliberate non-features

No chunking, no callbacks, no diarization, no keyterm hints. Speechmatics
confidence is not parsed, so `RawTranscript.confidence` is `None` for that
engine. Deepgram confidence and word timings drive the paragraph structure in
`gamas_bot/transcript.py`.

## Tests

`tests/test_stt.py` covers request shapes, language mapping, Persian model
compatibility, error classification, bounded retries, `Retry-After` clamping,
cooldowns, ordered failover, upload ceilings and the empty-response path. Every
test uses a scripted fake HTTP session — no CI test makes a paid call.

## Sources

* Deepgram [models](https://developers.deepgram.com/docs/models-languages-overview)
  · [pre-recorded audio](https://developers.deepgram.com/docs/pre-recorded-audio)
  · [language detection](https://developers.deepgram.com/docs/language-detection)
* Speechmatics [models](https://docs.speechmatics.com/speech-to-text/models)
  · [languages](https://docs.speechmatics.com/speech-to-text/languages)
  · [batch limits](https://docs.speechmatics.com/speech-to-text/batch/limits)
* [OpenAI transcription API](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
