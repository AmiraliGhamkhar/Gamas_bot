# Speech-to-text providers (v3)

This page describes the STT layer exactly as it exists in `gamas_bot/stt.py`.
There is no provider registry, route table or quota platform in v3: the
`STT_PROVIDERS` list **is** the failover policy, and every setting below is read
once in `gamas_bot/config.py` (see [CONFIGURATION.md](CONFIGURATION.md)).

## Providers

| Provider | Slug | Credentials | Model | Language sent |
| --- | --- | --- | --- | --- |
| Speechmatics Batch | `speechmatics` | `SPEECHMATICS_API_KEY` (Bearer) | `SPEECHMATICS_MODEL` (default `enhanced`) | `STT_LANGUAGE` (default `fa`) |
| Deepgram pre-recorded | `deepgram` | `DEEPGRAM_API_KEY` (`Authorization: Token …`) | `DEEPGRAM_MODEL` (default `nova-3`) | `STT_LANGUAGE`, or `detect_language=true` for `auto` |
| OpenAI-compatible | `openai_compatible` | `STT_OPENAI_API_KEY` + `STT_OPENAI_BASE_URL` | `STT_OPENAI_MODEL` (default `whisper-1`) | `STT_LANGUAGE`, omitted for `auto`/`multi` |

Endpoints (a full endpoint URL is also accepted, e.g.
`DEEPGRAM_BASE_URL=https://api.deepgram.com/v1/listen`):

* Speechmatics — `POST {SPEECHMATICS_BASE_URL}/jobs` (multipart: `config` JSON +
  `data_file`), then `GET /jobs/{id}`, then `GET /jobs/{id}/transcript?format=json-v2`.
* Deepgram — `POST {DEEPGRAM_BASE_URL}/listen` with the audio as a binary body and
  `Content-Type` taken from the file extension; `model`, `language`,
  `smart_format=true` and `punctuate=true` travel as query parameters.
* OpenAI-compatible — `POST {STT_OPENAI_BASE_URL}/audio/transcriptions`
  (multipart: `model`, `language`, `response_format=verbose_json`, `file`). A
  gateway that rejects `verbose_json` with an explicit message is retried once
  with `response_format=json`; any other non-200 is reported as-is.

## Failover policy

1. The order is `STT_PROVIDERS` verbatim (default
   `speechmatics,deepgram,openai_compatible`). Duplicates and unknown slugs are
   dropped at startup with a warning.
2. A provider is attempted only when it has credentials, is not in cooldown, and
   its direct-upload ceiling covers the file. Skipping a provider is logged and
   never silent.
3. Per provider the request is retried at most `MAX_TRANSIENT_RETRIES = 2` times
   (so three attempts) for transient failures: HTTP 408/425/429/5xx, network
   errors and timeouts. `Retry-After` is honoured, clamped to
   `MAX_RETRY_AFTER_SECONDS = 60 s`; otherwise the delay is `2^n` seconds capped
   at 15 s.
4. Permanent failures are never retried: any 4xx other than 408/425/429
   (rejected request — bad model, language or media), 401/403 (credentials) and
   unusable payloads move straight to the next provider.
5. Cooldowns are in-memory and per process: 15 minutes after an authentication
   failure, 60 seconds after an exhausted transient budget. If every configured
   provider is cooling down, the job fails with a message that names each
   provider and the remaining seconds — it never fails silently.
6. The first provider that returns a usable transcript wins; the transcript
   records the engine that actually ran. A payload with no letter or digit at
   all (for example punctuation only for silence) is **not** a transcript: it
   fails over, because delivering and billing an empty `.txt` would report a
   failure as success. Nothing is ever fabricated: when every provider fails,
   `transcribe()` raises and the job fails with the collected reasons, the
   reserved credit is released and no file is delivered.

## Language and model compatibility

Persian is the product's default (`STT_LANGUAGE=fa`), so these pairings matter:

* **Deepgram** — Persian (`fa`) is a Nova-3 language; Nova-2 has no Persian pack
  and rejects `language=fa`. `DEEPGRAM_MODEL` therefore defaults to `nova-3`
  (`nova-3-general` is equivalent). `auto`/`detect` is sent as
  `detect_language=true` — omitting `language` would *not* detect anything,
  Deepgram defaults to English. Note that Deepgram's detection list and its
  `multi` set (English, Spanish, French, German, Hindi, Russian, Portuguese,
  Japanese, Italian, Dutch) do not include Persian, so for Persian audio use
  `STT_LANGUAGE=fa`.
* **Speechmatics** — `enhanced`, `standard`, `melia-1` and `oak-1` are the batch
  models. `melia-1`/`oak-1` are batch-only multilingual models: the job config
  must send `language=multi`, `auto` is rejected, and language packs/translation
  are unavailable. `enhanced`/`standard` accept `fa` directly and are the only
  tiers that support `language=auto` (batch).
* **OpenAI-compatible** — `language` is an ISO-639-1 code (`fa`) and is omitted
  for `auto`/`detect`/`multi`, because Whisper detects the language itself and
  `auto` is not a valid code. `whisper-1` supports `response_format=verbose_json`
  (segment timings); the newer `gpt-4o-transcribe` family does not, which is why
  that one retry exists.

`--check` reports a Persian deployment whose `DEEPGRAM_MODEL` cannot transcribe
`fa`; it is advisory (another provider in the chain may still serve the job) and
is also written to the log at startup.

## Provider limits

| Limit | Speechmatics | Deepgram | OpenAI-compatible |
| --- | --- | --- | --- |
| Direct upload | **< 1 GB** per `POST /jobs` | 2 GB per request | `STT_OPENAI_MAX_UPLOAD_BYTES` (default 25 MB) |
| Larger input | only via a public `fetch_data` URL (not implemented) | — | — |
| Processing | jobs poll for `STT_JOB_TIMEOUT` (default 21600 s) | requests over ~10 min of *processing* can return 504 | request timeout is `STT_JOB_TIMEOUT` |
| Retention | 7 days, then 404/`expired` | none (transcript returned inline) | provider-dependent |

* The ceilings are enforced **before** any upload, measured in decimal bytes
  (`SPEECHMATICS_MAX_UPLOAD = 1_000_000_000`, `DEEPGRAM_MAX_UPLOAD = 2_000_000_000`).
  A file over one provider's ceiling is routed to the next one.
* Speechmatics also documents a two-hour recorded-length limit for batch jobs;
  the bot does not pre-screen duration, so an over-long file is rejected by the
  provider and fails over.
* Nothing is chunked. Splitting audio would cost word context at every boundary,
  which is worse than failing over.

## Logging and diagnosis

Every job carries its submission reference (`GMS-#####`) in the log via
`job_id`. The STT layer adds:

```
STT failover chain providers=speechmatics,deepgram,openai_compatible bytes=… language=fa
STT attempt provider=deepgram bytes=… model=nova-3 language=fa
STT provider speechmatics transient failure (attempt 1/3); retrying in 2.0s: …
STT done provider=deepgram bytes=… elapsed_seconds=…
STT provider deepgram rejected the request after 0.4s: Deepgram درخواست را رد کرد (HTTP 400): …
STT exhausted every eligible provider (2 attempted/skipped): …
```

Never logged: API keys, `Authorization` headers, audio bytes or transcript text.
Provider error bodies are reduced to a bounded, single-line message with the key
redacted.

To diagnose a failure, search the log for the tracking reference:

| Symptom in the log / chat | Class | Action |
| --- | --- | --- |
| `هیچ سرویس تبدیل گفتاری پیکربندی نشده است` | configuration | set a key for a provider that is in `STT_PROVIDERS` |
| `اعتبارنامه را نپذیرفت (HTTP 401/403)` | credentials | replace the key; the provider is skipped for 15 minutes |
| `محدودیت نرخ درخواست را اعلام کرد (HTTP 429)` | rate limit | transient; the job retries, then fails over |
| `درخواست را رد کرد (HTTP 4xx)` | request/model/language | read the provider detail in the message; check model/language pairing |
| `موتماً در دسترس نیست` / `پس از چند تلاش در دسترس نبود` | network/server | transient; check outbound TCP 443 and DNS |
| `سقف حجم …MB دارد` | file too large | the file is routed to the next provider; configure one with a larger ceiling |
| `در حال استراحت (…s)` | cooldown | wait, or fix the credential; the message names the provider |
| `پاسخ … معتبری برنگرداند` / `متن قابل استفاده‌ای تولید نکرد` | unusable response | provider outage or an incompatible gateway; the next provider runs |

`python -m gamas_bot --check` verifies configuration without contacting Telegram
or any provider, and `python -m gamas_bot.media_worker check` verifies the media
worker's media dependencies.

## Deliberate non-features

* No chunking, no callbacks, no diarization, no keyterm/vocabulary hints.
* Speechmatics word timings are used (json-v2 `results` with `attaches_to`
  punctuation); Speechmatics confidence is not parsed, so
  `RawTranscript.confidence` is `None` for that engine.
* Deepgram confidence and `punctuated_word` timings are used to build the
  paragraph structure in `gamas_bot/transcript.py`.

## Tests

`tests/test_stt.py` covers request shapes per provider, language mapping,
Persian model compatibility, HTTP error classification, bounded retries,
`Retry-After` clamping, cooldowns, ordered failover, upload ceilings and the
malformed/empty-response paths. Every test uses a scripted fake HTTP session —
no test in CI makes a paid call or needs a key.

## Sources

* Deepgram models and languages: https://developers.deepgram.com/docs/models-languages-overview
* Deepgram pre-recorded audio (limits, auth): https://developers.deepgram.com/docs/pre-recorded-audio
* Deepgram language detection: https://developers.deepgram.com/docs/language-detection
* Speechmatics models: https://docs.speechmatics.com/speech-to-text/models
* Speechmatics languages: https://docs.speechmatics.com/speech-to-text/languages
* Speechmatics batch limits: https://docs.speechmatics.com/speech-to-text/batch/limits
* OpenAI transcription API: https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
