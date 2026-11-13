# Providers

The bot has two separate AI pipelines: **speech-to-text (STT)** for uploaded
audio and **LLM generation** for booklets, AI quizzes and AI flashcards. Keys
belong in the private `.env` (or the process environment); settings are read at
startup. STT and note-generation routes have independent credentials and
failover orders.

## What is the live server using?

The model table below records the **code defaults**. Separately, the operator
reports that the current deployment uses Nara `laguna-s-2.1`, OpenRouter
`nvidia/nemotron-3.5-super-vl:free`, and Gemini `gemini-3.5-flash-lite` (update
2026-11-13). This is user-provided information, not independently verified:
this checkout contains `.env.example` only, no live `.env`. The deployment's
failover order was not specified, and must be read from its actual `NOTE_PROVIDERS`.

| Operator-reported current route | Model ID | Setting |
| --- | --- | --- |
| NaraRouter | `laguna-s-2.1` | `NARA_MODEL` |
| OpenRouter | `nvidia/nemotron-3.5-super-vl:free` | `OPENROUTER_MODEL` |
| Gemini | `gemini-3.5-flash-lite` | `GEMINI_MODEL` |

On the target host, inspect the private `.env` and service-level environment
overrides. To print the effective note-generation chain without keys or network
calls, run `python -m scripts.probe_note_providers --show-config` from the
project virtualenv. It uses the same configuration as the bot. Never paste or
commit secret values.

## Speech-to-text

At least one STT key is required. The default `STT_PROVIDERS` order is
`speechmatics,deepgram,openai_compatible`. A provider without its own key is
skipped; set `STT_PROVIDERS` to choose a different order. Model IDs can also
be overridden independently.

| Provider | Default model | Configuration | Persian notes |
| --- | --- | --- | --- |
| Speechmatics | `enhanced` | `SPEECHMATICS_API_KEY`, `SPEECHMATICS_MODEL`, `SPEECHMATICS_MAX_IN_FLIGHT=2` | long-poll job API |
| Deepgram | `nova-3` | `DEEPGRAM_API_KEY`, `DEEPGRAM_BASE_URL=https://api.deepgram.com/v1`, `DEEPGRAM_MODEL`, `DEEPGRAM_MAX_IN_FLIGHT=4` | `nova-3` supports `fa`; `nova-2` does not |
| OpenAI-compatible | `whisper-1` | `STT_OPENAI_BASE_URL`, `STT_OPENAI_API_KEY`, `STT_OPENAI_MODEL`, `STT_OPENAI_MAX_UPLOAD_BYTES=25000000`, `STT_OPENAI_MAX_IN_FLIGHT=2` | endpoint must accept audio transcription uploads |

```dotenv
STT_PROVIDERS=speechmatics,deepgram,openai_compatible
STT_LANGUAGE=fa
STT_FALLBACK_ENABLED=true
STT_PREPROCESS=true
STT_DIARIZE=true
STT_SPEAKER_SENSITIVITY=0.5
```

- `STT_LANGUAGE=fa` is the Persian-first default; a selected model/provider
  must support the requested language.
- `STT_PREPROCESS=true` re-encodes every upload with a bounded PyAV filter chain
  (rumble, hiss, level, limiter). The charged duration is still the uploaded one.
- `STT_DIARIZE=true` asks the engine who spoke; providers that cannot diarize
  ignore it. `DIARIZATION_ENABLED` is the legacy alias and wins when set.
  `STT_PRESERVE_STEREO=true` keeps both channels (and doubles PCM upload size).
- Each provider has its own in-flight slot pool; configured fallback capacity
  can take work when a primary is saturated or returns a recoverable error.
- Offline sanity check (no network): `python -m gamas_bot --check`.

## Note generation (optional)

The default note chain is `gemini,nara,openrouter,deepseek,avalai`; the model
for each route defaults as follows. `NOTE_PROVIDERS` controls the order, while
`GEMINI_MODEL`, `NARA_MODEL`, `OPENROUTER_MODEL`, `DEEPSEEK_MODEL` and
`AVALAI_MODEL` override the corresponding IDs. Hosted routes without their own
credentials are skipped. The same note-generation client is used for booklet
creation and AI quiz/flashcard generation. Without a note route, audio still
delivers the full transcript and document/Office work uses deterministic local
fallbacks; AI quizzes and flashcards require a working note route.

| Route | Key | Shipped default model |
| --- | --- | --- |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` |
| `nara` | `NARA_API_KEY` | `agnes-2.5-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `google/gemma-4-31b-it:free` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-flash` |
| `avalai` | `AVALAI_API_KEY` | `deepseek-v4.1-flash` |

Optional legacy single-provider slots are not part of that default chain:

| Route | Configuration | Default model |
| --- | --- | --- |
| OpenAI-compatible | `NOTE_PROVIDER=openai_compatible`, `NOTE_API_KEY` and/or `NOTE_BASE_URL` | `gpt-4o-mini` |
| Anthropic | `NOTE_PROVIDER=anthropic`, `NOTE_API_KEY`, `NOTE_MODEL` | none; set `NOTE_MODEL` explicitly |

A configured legacy slot is appended to the declared chain if not already in it;
a `NOTE_BASE_URL` on a recognized provider host is classified as that provider.
A set base URL can therefore add a route. Credentials are never shared between
providers. Unknown slugs are warned and skipped, duplicates are removed, and
keyless hosted routes are never attempted.

### Earlier tested profile (2026-11-07)

This Gemini + OpenRouter configuration passed metadata and generation tests at
one deployment. It is historical evidence, not the shipped default and not a
verification of today's production settings:

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

NaraRouter was excluded from that earlier test profile. Three generation
attempts for `laguna-s-2.1` returned HTTP 502 even though metadata checks passed.
The operator now reports this model in current use; the earlier failures are
historical and do not establish its current availability. Entitlement is not a
generation guarantee.

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

### AvalAI API safety and cost reconciliation

The `avalai` route uses the OpenAI-compatible **Chat Completions** API at
`https://api.avalai.ir/v1` with `Authorization: Bearer <AVALAI_API_KEY>`. It
intentionally does not send note prompts to AvalAI's separate User API base
(`https://api.avalai.ir/user/v1`) or its Google-native `/v1beta` API. Keep the
key in `AVALAI_API_KEY`, never in `AVALAI_BASE_URL`; `--check` rejects the
wrong official API family and credential/query-bearing official URLs.

AvalAI returns `avalai-request-id` on responses. The LLM layer captures it for
both normal and streaming completions and writes the safe value beside each
successful usage record, for example:

```text
note provider usage provider=avalai request_id=... input_tokens=... output_tokens=...
```

Use that exact request id with AvalAI's [User API reference](https://docs.avalai.ir/en/api-reference/user)
to reconcile a completion's precise provider cost. Failure messages prefer the
same header when present, while keys and control characters are redacted. Model
availability changes independently of the bot release: run the metadata stage
before choosing or promoting an `AVALAI_MODEL`.

For an AvalAI-only check that never creates Telegram/SQLite state:

```bash
python -m scripts.smoke_avalai             # metadata only
python -m scripts.smoke_avalai --snippet   # one billable, small generation
```

The smoke command hides credentials, query strings and fragments when it prints
a base URL. Do not run its generation modes from CI or cron.

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
