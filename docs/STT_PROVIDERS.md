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

## Audio preprocessing (mandatory by default)

Every upload is cleaned before it is sent to an engine: `STT_PREPROCESS=true`
(the default) makes the media worker run

| Stage | Why |
| --- | --- |
| `highpass=f=70:poles=2` | room rumble, desk thumps, air-conditioning below speech |
| `afftdn=nr=12:nf=-35:tn=1` | steady projector/fan hiss and tape noise |
| `dynaudnorm=f=250:g=11:p=0.85:m=4` | the lecturer near the mic and a student at the back |
| `alimiter=limit=0.95:level=false` | brick-wall guard, so normalisation cannot clip |

The chain runs inside the extraction call, after the mono/16 kHz resample, and is
**sample-exact**: the output has exactly as many samples as the input, so the
duration the user is charged for is the duration the engine heard. A stage the
local FFmpeg build does not provide is skipped (and reported) rather than failing
the job. Set `STT_PREPROCESS=false` only to compare raw recognition quality.

## Speaker diarization

`STT_DIARIZE=true` (default) asks the engine who spoke and the transcript is
split by conversation turns:

| Route | Request | Reading |
| --- | --- | --- |
| `speechmatics` | `transcription_config.diarization="speaker"` plus `speaker_diarization_config.speaker_sensitivity` | per-word `speaker` (`S1`, `S2`, `UU`) |
| `deepgram` | `diarize=true` | per-word `speaker` index |
| `openai_compatible` | pass-through | per-segment `speaker`, when the gateway provides one |

Labels are counted per file and never stored as an identity: providers renumber
speakers between calls, so `S1` in one lecture is not `S1` in the next.

The count shown to the user is deliberately conservative. A raw label must hold
at least 4% of attributed words (and 12 words) to be reported, the maximum is 12
speakers, and unknown (`UU`) words inherit the surrounding turn. A run that
resolves to a single voice is delivered **without** speaker labels — a heading on
every paragraph of a solo lecture is noise — and reported as «یک گوینده».

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


---

## Diarization routing (2.0)

`ProviderSpec.supports_diarization` marks Speechmatics and Deepgram as native
diarizers; an OpenAI-compatible gateway is pass-through (it may surface segment
labels, but Whisper itself does not diarize). When `STT_DIARIZE` (or its alias
`DIARIZATION_ENABLED`) is on, `order_specs_for_diarization()` stable-sorts the
*configured* chain so a diarizing engine is tried first. The capacity pool still
overflows immediately rather than waiting for a busy provider, and when no
diarizing provider is configured the order is unchanged and the transcript simply
carries no speaker labels.

* Speechmatics: `diarization=speaker` with `speaker_sensitivity` (0–1, default
  0.5). Documented at docs.speechmatics.com; adds 10–50 % latency.
* Deepgram: leave `DEEPGRAM_DIARIZE_MODEL` empty for the legacy `diarize=true`
  (v1). `latest` / `v1` / `v2` send the documented `diarize_model` parameter;
  `latest` maps to the v2 batch diarizer for pre-recorded audio. See
  developers.deepgram.com/docs/diarization.
* Word confidences are kept on `Word.confidence`; overlapping words are derived
  from timings only, never from a provider claim.

Labels are resolved conservatively (`MIN_SPEAKER_SHARE`, `MIN_SPEAKER_WORDS`,
`MAX_SPEAKERS`): a one-voice class is reported as one speaker, not as a panel.
