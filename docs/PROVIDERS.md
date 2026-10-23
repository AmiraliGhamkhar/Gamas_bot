# Note-generation providers

Note generation is optional. Without a configured provider the bot delivers the raw transcript.
Keys live in `.env` or the process environment, never in the database.

## Routes

Set `NOTE_PROVIDERS` to the desired order. A route is skipped when its own key is missing.

| Slug | Key | Default model |
| --- | --- | --- |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` |
| `nara` | `NARA_API_KEY` | `agnes-2.5-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `google/gemma-4-31b-it:free` |
| `groq` | `GROQ_API_KEY` | `openai/gpt-oss-120b` |
| `cerebras` | `CEREBRAS_API_KEY` | `gpt-oss-120b` |
| `openai_compatible` | `NOTE_API_KEY` | `gpt-4o-mini` |
| `anthropic` | `NOTE_API_KEY` | requires an explicit `NOTE_MODEL` |

Base URLs and all defaults are in `.env.example`. The shipped order is
`gemini,nara,openrouter,groq,cerebras`; the order **is** the failover policy.

The legacy single-provider slot (`NOTE_PROVIDER`, `NOTE_BASE_URL`, `NOTE_MODEL`, `NOTE_API_KEY`) is
still honored and is classified by host, so pointing it at a documented provider keeps that
provider's place in the chain.

## Recommended Persian setup

```dotenv
NOTE_PROVIDERS=gemini,nara,openrouter,groq,cerebras
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash-lite
NOTE_GEMINI_THINKING_BUDGET=0
NOTE_CONCURRENCY=3
NOTE_REQUEST_TIMEOUT_SECONDS=900
NOTE_STRICT_GROUNDING=true
```

`NOTE_GEMINI_THINKING_BUDGET=0` disables Gemini thinking (extraction needs no hidden reasoning);
a positive value caps it and `-1` sends no thinking field.

## Failover and guards

The first configured route with a credential and a free local slot is selected. If the primary is
saturated, an available configured secondary is selected immediately; a bounded wait applies only
when every candidate route is full. `LLM_PRIMARY_MAX_IN_FLIGHT`, `LLM_FALLBACK_MAX_IN_FLIGHT`, and
`LLM_SLOT_WAIT_TIMEOUT_SECONDS` (default `4`, `4`, and `30` seconds) control those local limits.

- HTTP 429 and recognized provider-capacity limits cool down the route briefly and fail over
  immediately without sleeping or replaying that completion. Authentication/quota errors also
  cool down and move to the next eligible route.
- Transient network/server errors receive at most two retries on that route. A normal request
  timeout fails over rather than repeating a potentially costly request; the last route can use its
  bounded retry path. An output-truncated answer is not resent unchanged.
- **First-byte guard** (`NOTE_TTFB_TIMEOUT_SECONDS`, default 25, `0` off): on OpenAI-style and
  Gemini routes the request is streamed; a route that sends no byte in time is abandoned and the
  chain fails over instead of waiting out the full deadline.
- **Hedged racing** (`NOTE_HEDGE_AFTER_SECONDS`, default 60, `0` off): if the first route has not
  finished after the delay, the second starts in parallel. At most one extra request is active; it
  can spend extra quota. Both tasks are cancelled/joined and leases released if the caller is
  cancelled.
- Non-streaming JSON and SSE response bodies are incrementally bounded at 16 MiB.
- **Structured output** (`NOTE_STRUCTURED_OUTPUT=1`): the request carries a response schema. A
  provider that rejects the field is retried once without it.

Tune `NOTE_CONTEXT_TOKENS`, `NOTE_MAX_OUTPUT_TOKENS` and `NOTE_REQUEST_TIMEOUT_SECONDS` for the
smallest/slowest model in the chain. Provider output is still third-party content: `--check`
validates settings, and the evidence gate checks the booklet before delivery (see
[Note quality](NOTE_QUALITY.md)).

```powershell
.\.venv\Scripts\python.exe -m gamas_bot --check
```

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
```
