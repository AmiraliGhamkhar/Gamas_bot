# Note-generation providers

Note providers are optional for transcription. Without one, audio delivers only
the transcript; document notes and Office use deterministic fallbacks. AI quizzes
and flashcards need a working note route.

| Route | Key | Shipped model default |
| --- | --- | --- |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` |
| `nara` | `NARA_API_KEY` | `agnes-2.5-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `google/gemma-4-31b-it:free` |
| `groq` | `GROQ_API_KEY` | `openai/gpt-oss-120b` |
| `cerebras` | `CEREBRAS_API_KEY` | `gpt-oss-120b` |
| `openai_compatible` | `NOTE_API_KEY` | `gpt-4o-mini` |
| `anthropic` | `NOTE_API_KEY` | Explicit `NOTE_MODEL` required |

These are repository defaults, not guarantees of current availability. Confirm
model access, context limits and pricing with your provider account.

## Setup

```dotenv
NOTE_PROVIDERS=gemini,nara,openrouter,groq,cerebras
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
Cerebras uses `strict: true` schemas (capping completion tokens at `40,960` and
setting `reasoning_format: "parsed"`); Groq switches to `json_object` while SSE
streaming is active (`stream: true` + `json_schema` is rejected by Groq) and uses
`max_completion_tokens` + `reasoning_format: "parsed"`; OpenRouter `:free` routes
use `json_object` so endpoint filtering does not 404; and Nara omits undocumented
`response_format` and `stream_options` fields unless explicitly opted in. Any
rejected parameter downgrades (`json_schema` → `json_object` → plain prompt) and
retries once. Size budgets for the smallest configured model.
Keep grounding enabled: [Note quality](NOTE_QUALITY.md).

## Verify

Run the [offline check](CONFIGURATION.md#validate). Optional live metadata probe
(network access, no generation):

- PowerShell: `.\.venv\Scripts\python.exe -m scripts.probe_note_providers`
- cPanel: `python -m scripts.probe_note_providers` (virtualenv active).

Adding `--generate lecture.txt` uploads text and consumes quota. Use a non-sensitive
test transcript and review the output manually.
