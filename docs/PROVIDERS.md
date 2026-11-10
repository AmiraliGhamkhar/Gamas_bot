# Providers

Two independent chains: **speech-to-text** (required) and **note generation**
(optional). Keys go in `.env` only, and the process reads them once at startup.

## Speech-to-text

At least one key is required. `STT_PROVIDERS` is the failover order; a provider
without its own key is skipped.

| Provider | Keys | Persian notes |
| --- | --- | --- |
| Speechmatics | `SPEECHMATICS_API_KEY`, `SPEECHMATICS_MODEL=enhanced`, `SPEECHMATICS_MAX_IN_FLIGHT=2` | long-poll job API |
| Deepgram | `DEEPGRAM_API_KEY`, `DEEPGRAM_BASE_URL=https://api.deepgram.com/v1`, `DEEPGRAM_MODEL=nova-3`, `DEEPGRAM_MAX_IN_FLIGHT=4` | `nova-3` is required for `fa`; `nova-2` has no Persian pack |
| OpenAI-compatible | `STT_OPENAI_BASE_URL`, `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL=whisper-1`, `STT_OPENAI_MAX_UPLOAD_BYTES=25000000`, `STT_OPENAI_MAX_IN_FLIGHT=2` | any endpoint that accepts `audio/…` uploads |

```dotenv
STT_PROVIDERS=speechmatics,deepgram,openai_compatible
STT_LANGUAGE=fa
STT_FALLBACK_ENABLED=true
STT_PREPROCESS=true
STT_DIARIZE=true
STT_SPEAKER_SENSITIVITY=0.5
```

- `STT_LANGUAGE=fa` is the Persian-first default; switching language without a
  provider that supports it returns provider errors, not empty text.
- `STT_PREPROCESS=true` re-encodes every upload with a bounded PyAV filter chain
  (rumble, hiss, level, limiter). The charged duration is still the uploaded one.
  Set `false` only for A/B quality comparisons.
- `STT_DIARIZE=true` asks the engine who spoke; providers that cannot diarize
  ignore it. `DIARIZATION_ENABLED` is the legacy alias and wins when set.
  `STT_PRESERVE_STEREO=true` keeps two channels (doubles the upload size).
- Each provider has its own in-flight slot pool; a full primary overflows into
  available fallback capacity instead of waiting.
- Offline sanity check (no network): `python -m gamas_bot --check`.

## Note generation (optional)

Without a note provider, audio still delivers the **full transcript**; document
notes and Office exports use deterministic local fallbacks. AI quizzes and
flashcards need a working route.

| Route | Key | Shipped model |
| --- | --- | --- |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` |
| `nara` | `NARA_API_KEY` | `agnes-2.5-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `google/gemma-4-31b-it:free` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-flash` |
| `avalai` | `AVALAI_API_KEY` | `deepseek-v4.1-flash` |
| `openai_compatible` | `NOTE_API_KEY` + `NOTE_BASE_URL` | `gpt-4o-mini` (legacy slot) |

`NOTE_PROVIDERS` is the policy: unknown slugs are warned and skipped, duplicates
removed, keyless routes never attempted. A credential is never shared between
providers. Leave the legacy `NOTE_PROVIDER`/`NOTE_API_KEY`/`NOTE_BASE_URL` blank
unless you deliberately want that extra route — a set base URL *adds* an
`openai_compatible` leg.

### Tested profile

Verified with real generation at one deployment on 2026-11-07. It is a starting
point for your own probes, not a shipped default:

```dotenv
NOTE_PROVIDERS=gemini,openrouter
NOTE_STRUCTURED_OUTPUT=0
NOTE_REASONING_EFFORT=none
NOTE_MAX_OUTPUT_TOKENS=32768
NOTE_GEMINI_THINKING_BUDGET=-1
GEMINI_MODEL=gemini-3.5-flash-lite
OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
```

NaraRouter stays configurable but is excluded from this chain: `laguna-s-2.1`
failed three real generation attempts with HTTP 502 while its metadata check
passed. Entitlement is not a generation guarantee.

### Key settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `NOTE_MAX_OUTPUT_TOKENS` | `16384` | per-request output budget (`max_tokens`/`maxOutputTokens`); tested profile uses `32768` |
| `NOTE_STRUCTURED_OUTPUT` | `1` | send a JSON schema; `0` relies on the prompt, still strict-parsed and validated |
| `NOTE_REASONING_EFFORT` | empty | `none…max`, only on routes that document it |
| `NOTE_GEMINI_THINKING_BUDGET` | `0` | `-1` omits `thinkingConfig`, positive caps it |
| `NOTE_REQUEST_TIMEOUT_SECONDS` | `900` | one note request; at 32 768 tokens `--check` may warn |
| `NOTE_CONCURRENCY`, `NOTE_CHUNK_CHARS`, `NOTE_CONTEXT_TOKENS` | `3`, `64000`, `131072` | input sizing and parallelism |
| `NOTE_STRICT_GROUNDING`, `NOTE_QUALITY_GATE`, `NOTE_COMPLETENESS_FILL` | `true`, `repair`, `true` | grounding and repair gates |
| `NOTE_HEDGE_AFTER_SECONDS` | `60` | start one extra route on a slow request; `0` disables |

A truncated answer (`finish_reason=length`) is refused, never delivered as a
partial booklet: raise the budget with evidence, do not retry blindly.

## Probe a deployment (three stages, keep them separate)

```powershell
.\.venv\Scripts\python.exe -m scripts.probe_note_providers --show-config
.\.venv\Scripts\python.exe -m scripts.probe_note_providers
.\.venv\Scripts\python.exe -m scripts.probe_note_providers --generate .\short.txt
```

```bash
python -m scripts.probe_note_providers --show-config
python -m scripts.probe_note_providers
python -m scripts.probe_note_providers --generate short.txt
```

1. `--show-config` — offline: effective order, models and outgoing token budget.
2. no flag — metadata only (authentication, entitlement, published limits). No
   generation quota.
3. `--generate FILE` — **one real request per route, consumes quota.** Use a
   short, non-sensitive transcript; never wire this into CI or cron.

If a route fails stage 3, remove it from `NOTE_PROVIDERS`, restart, and keep its
key for later. Logs record `requested_output_tokens` (what was sent) and
`output_tokens` (what the provider billed) with no keys or transcript text.
