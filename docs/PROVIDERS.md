# Note-generation providers

Note generation is optional. Without a configured note provider, the bot still delivers the raw transcript. Provider keys are stored in `.env` or the process environment, not in the database.

## Choose providers

Supported routes are `nara`, `openrouter`, `openai_compatible`, `gemini` and `anthropic`. Configure their own keys (`NARA_API_KEY`, `OPENROUTER_API_KEY`, `NOTE_API_KEY` or `GEMINI_API_KEY`) and set `NOTE_PROVIDERS` to the desired order. A provider is skipped when its own credential is missing. Do not assume a model or free tier is permanent; check the provider's current account terms.

The legacy single-provider settings (`NOTE_PROVIDER`, `NOTE_BASE_URL`, `NOTE_MODEL`, `NOTE_API_KEY`) remain supported. Anthropic requires an explicit `NOTE_MODEL`. See `.env.example` for defaults and provider URLs.

## Failover and limits

Transient network, timeout and rate-limit errors get bounded retries, then the next eligible provider is tried. Oversized input is split within configured limits; an answer cut off at the output-token limit is not resent unchanged. Tune `NOTE_CONTEXT_TOKENS`, `NOTE_MAX_OUTPUT_TOKENS` and `NOTE_REQUEST_TIMEOUT_SECONDS` for the smallest/slowest model in the configured chain.

Two optional, default-off guards can shorten the wait on a hung or slow provider; neither changes what is asked or how answers are validated:

- **TTFB guard (`NOTE_TTFB_TIMEOUT_SECONDS`, default `0` = off).** On OpenAI-style routes only (`nara`, `openrouter`, `openai_compatible`), the request is streamed and the time to the first streamed byte is measured (gateway comment lines count as liveness). If no byte arrives within the threshold, the provider is abandoned and the chain fails over in seconds instead of waiting out the full request timeout, with no same-provider retry when a successor exists. The streamed deltas are reassembled and parsed by exactly the same response parser, in-body error checks and usage logging as a non-streaming answer, so a `length` finish still raises the output-budget error and is never resent unchanged. Gemini and Anthropic routes keep the non-streaming path.
- **Hedged racing (`NOTE_HEDGE_AFTER_SECONDS`, default `0` = off).** With at least two eligible providers, the first provider starts alone; only if it has not finished after the threshold does the second provider start in parallel. The first successful answer wins and the loser is cancelled. A provider that fails *before* the threshold behaves exactly as in the sequential chain (no race is started). A hedge adds at most one extra in-flight POST per pipeline call, so it can consume extra provider quota — enable it only when the tail latency is worth that cost.

Provider output is checked by the evidence gate before delivery. It is still third-party generated content; see [Note quality](NOTE_QUALITY.md).

## Check configuration

```powershell
python -m gamas_bot --check
```

On cPanel, use the same command with the bot's virtualenv Python. `python -m scripts.cpanel_preflight` additionally checks provider-host network access on the server. Do not use a live generation probe unless you intend to consume provider quota.
