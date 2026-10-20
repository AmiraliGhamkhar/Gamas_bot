# Note-generation providers

Note generation is optional. Without a configured note provider, the bot still delivers the raw transcript. Provider keys are stored in `.env` or the process environment, not in the database.

## Choose providers

Supported routes are `nara`, `openrouter`, `openai_compatible`, `gemini` and `anthropic`. Configure their own keys (`NARA_API_KEY`, `OPENROUTER_API_KEY`, `NOTE_API_KEY` or `GEMINI_API_KEY`) and set `NOTE_PROVIDERS` to the desired order. A provider is skipped when its own credential is missing. Do not assume a model or free tier is permanent; check the provider's current account terms.

The legacy single-provider settings (`NOTE_PROVIDER`, `NOTE_BASE_URL`, `NOTE_MODEL`, `NOTE_API_KEY`) remain supported. Anthropic requires an explicit `NOTE_MODEL`. See `.env.example` for defaults and provider URLs.

## Failover and limits

Transient network, timeout and rate-limit errors get bounded retries, then the next eligible provider is tried. Oversized input is split within configured limits; an answer cut off at the output-token limit is not resent unchanged. Tune `NOTE_CONTEXT_TOKENS`, `NOTE_MAX_OUTPUT_TOKENS` and `NOTE_REQUEST_TIMEOUT_SECONDS` for the smallest/slowest model in the configured chain.

Provider output is checked by the evidence gate before delivery. It is still third-party generated content; see [Note quality](NOTE_QUALITY.md).

## Check configuration

```powershell
python -m gamas_bot --check
```

On cPanel, use the same command with the bot's virtualenv Python. `python -m scripts.cpanel_preflight` additionally checks provider-host network access on the server. Do not use a live generation probe unless you intend to consume provider quota.
