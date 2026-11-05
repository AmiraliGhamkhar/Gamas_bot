# Note-generation providers

Note providers are optional for transcription. Without one, audio delivers only
the transcript; document notes and Office use deterministic fallbacks. AI quizzes
and flashcards need a working note route.

| Route | Key | Shipped model default |
| --- | --- | --- |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` |
| `nara` | `NARA_API_KEY` | `agnes-2.5-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `google/gemma-4-31b-it:free` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-flash` |
| `avalai` | `AVALAI_API_KEY` | `deepseek-v4.1-flash` |
| `openai_compatible` | `NOTE_API_KEY` | `gpt-4o-mini` |
| `anthropic` | `NOTE_API_KEY` | Explicit `NOTE_MODEL` required |

These are repository defaults, not guarantees of current availability. Confirm
model access, context limits and pricing with your provider account.

## Setup

```dotenv
NOTE_PROVIDERS=gemini,nara,openrouter,deepseek,avalai
NOTE_CONCURRENCY=3
NOTE_STRICT_GROUNDING=true
```

Add keys only for routes you use; keyless routes are skipped. Override models and
URLs with settings from [`.env.example`](../.env.example). The legacy single-route
slot uses `NOTE_PROVIDER`, `NOTE_BASE_URL`, `NOTE_MODEL`, `NOTE_API_KEY`.

## Routing and costs

The ordered chain uses available slots; a full primary overflows immediately.
Primary/fallback limits default to 4/4 and all-full wait to 300 seconds. Auth,
quota and rate limits can cool down a route and trigger failover. Transient retries
are bounded; responses are capped at 16 MiB.

`NOTE_HEDGE_AFTER_SECONDS=60` can start one extra route while a slow request is
pending; `0` disables that extra quota use. Per-provider request profiles
(`gamas_bot/llm_profiles.py`) normalize wire schemas and parameters automatically:
Gemini `responseSchema` keeps `required` and strips `additionalProperties`;
OpenRouter `:free` routes use `json_object` so endpoint filtering does not 404;
DeepSeek receives documented JSON Output (`json_object`) but never an
undocumented strict `json_schema`; AvalAI receives a documented `json_schema`
envelope with the `json_object` downgrade as its fallback; and Nara omits
undocumented `response_format` and `stream_options` fields unless explicitly
opted in. Groq and Cerebras were removed from the integration; a
legacy `NOTE_PROVIDERS` value naming them is skipped with a startup warning. Any
rejected parameter downgrades (`json_schema` → `json_object` → plain prompt) and
retries once. Size budgets for the smallest configured model.
Keep grounding enabled: [Note quality](NOTE_QUALITY.md).

## AvalAI

[AvalAI](https://docs.avalai.ir/en/api-reference/chat) is an OpenAI-compatible
gateway that serves several vendors' models under its own ids, so it is one route
in the chain rather than a replacement for `deepseek`:

```dotenv
AVALAI_API_KEY=...
AVALAI_BASE_URL=https://api.avalai.ir/v1
AVALAI_MODEL=deepseek-v4.1-flash
```

Its DeepSeek entry documents 1,000,000 input / 393,216 output tokens, JSON output,
`json_schema` structured output and **implicit** prompt caching
([provider page](https://docs.avalai.ir/en/providers/deepseek)). Caching is why the
note prompt is one static string: a cache hit requires an exact prefix match from
the start of the request, so per-job material never enters the system prompt. The
gateway bills cached input at $0.003/1M against $0.15/1M uncached, so the saving is
only real if the prefix is byte-identical — which is asserted by
`tests/test_notes.py::PromptTests`. Rate limits depend on the account tier
([tiers](https://docs.avalai.ir/en/guides/rate-limits)); on a low tier, lower
`NOTE_CONCURRENCY` and `NOTE_CHUNK_CHARS` so one booklet does not exceed the
per-minute budget. `AVALAI_BASE_URL` also repairs itself: a bare
`https://api.avalai.ir` gains the documented `/v1` prefix.

`NOTE_CONTEXT_TOKENS` stays at the shipped default (131072) even though this
model documents a 1,000,000-token input: it sizes *every* request, including the
ones served by whichever smaller-window route in `NOTE_PROVIDERS` ends up
answering, so it can only be as large as the tightest route in the order. Raise
it only when every route serves at least that much. The other shipped
defaults already match this route — `NOTE_STRUCTURED_OUTPUT=1`,
`NOTE_COMPLETENESS_FILL=1`, `NOTE_QUALITY_GATE=repair`, and an empty
`NOTE_REASONING_EFFORT` (the note task is extraction, so hidden reasoning is pure
latency).

## Verify

Run the [offline check](CONFIGURATION.md#validate). Optional live metadata probe
(network access, no generation):

- PowerShell: `.\.venv\Scripts\python.exe -m scripts.probe_note_providers`
- cPanel: `python -m scripts.probe_note_providers` (virtualenv active).

Adding `--generate lecture.txt` uploads text and consumes quota. Use a non-sensitive
test transcript and review the output manually.

For one route in depth — the AvalAI chain member — use the dedicated smoke test.
It reports the configured route, asks `GET /models` whether the model is
entitled, and (only with `--generate`/`--snippet`) sends one real note request,
printing the served model, elapsed time, token usage and the cached-input count,
which is how the static-prompt cache is confirmed:

```bash
python -m scripts.smoke_avalai                                # metadata only
python -m scripts.smoke_avalai --snippet                      # one small request
python -m scripts.smoke_avalai --generate lecture.txt --full  # whole pipeline
```

`--full` runs chunking, the editorial compile and the coverage fill on the same
text and renders the DOCX, so the compile prompt and the gates are exercised
before a rollout. Both probes are operator tools: the bot never runs them, they
write nothing to the database, and they exit non-zero on any failure.
