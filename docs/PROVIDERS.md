# Note-generation providers

Note generation is optional. Without a configured note provider, the bot still delivers the raw transcript. Provider keys are stored in `.env` or the process environment, not in the database.

## Choose providers

Supported routes are `gemini`, `nara`, `openrouter`, `groq`, `cerebras`, `openai_compatible` and `anthropic`. Configure their own keys (`GEMINI_API_KEY`, `NARA_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, or the generic `NOTE_API_KEY`) and set `NOTE_PROVIDERS` to the desired order. A provider is skipped when its own credential is missing. Do not assume a model or free tier is permanent; check the provider's current account terms.

The default chain is `NOTE_PROVIDERS=gemini,nara,openrouter,groq,cerebras`: the fastest documented route first, then each further provider only if it is configured. The order *is* the failover policy.

The legacy single-provider settings (`NOTE_PROVIDER`, `NOTE_BASE_URL`, `NOTE_MODEL`, `NOTE_API_KEY`) remain supported. Anthropic requires an explicit `NOTE_MODEL`. See `.env.example` for defaults and provider URLs.

## Routes at a glance

Verified October 2026. Speeds and quotas are the providers' published free-tier figures and change over time — re-check before relying on them.

| Provider (slug) | Default model | Output speed | Free tier limits | Persian quality | Caveats |
|---|---|---|---|---|---|
| Google AI Studio (`gemini`) | `gemini-2.5-flash-lite` | ~330–570 tok/s | ~1,000 req/day, 250K TPM, 1M ctx | Excellent | thinking forced off via `thinkingBudget=0` |
| NaraRouter (`nara`) | `agnes-2.5-flash` | ~53 tok/s | 5M tokens/day | Good | very slow → kept as fallback |
| OpenRouter (`openrouter`) | `google/gemma-4-31b-it:free` | slow | 50 req/day | OK | tiny quota → last resort |
| Groq (`groq`) | `openai/gpt-oss-120b` | ~470–500 tok/s, TTFT ~0.17s | 30 RPM, 1K req/day, **8K TPM**, 200K tok/day | Good | if Groq is PRIMARY: `NOTE_CHUNK_CHARS=14000`, `NOTE_CONCURRENCY=2` |
| Cerebras (`cerebras`) | `gpt-oss-120b` | ~2,000–3,000 tok/s | **5 RPM**, 30K TPM, 1M tok/day | Good | 5 RPM clashes with concurrency → hedge/secondary only |

### Gemini thinking budget

`NOTE_GEMINI_THINKING_BUDGET` is sent as `generationConfig.thinkingConfig.thinkingBudget`:

- `0` (default) disables thinking. A note request is extraction and organisation, so hidden reasoning tokens buy nothing and cost latency.
- a positive value caps the reasoning tokens the model may spend.
- `-1` sends no `thinkingConfig` field at all (the pre-flag behaviour).

If a model rejects the field, the request is retried once without it — no model-name sniffing is involved.

### Recommended preset

```
NOTE_PROVIDERS=gemini,nara,openrouter,groq,cerebras
GEMINI_MODEL=gemini-2.5-flash-lite
NOTE_GEMINI_THINKING_BUDGET=0
NOTE_TTFB_TIMEOUT_SECONDS=25
NOTE_HEDGE_AFTER_SECONDS=60
NOTE_CONCURRENCY=6
NOTE_REQUEST_TIMEOUT_SECONDS=300
NOTE_REASONING_EFFORT=low
NOTE_STRICT_GROUNDING=true
```

Expected result: a full-lecture note run drops from ~8–12 minutes (Nara-only) to ~1–2 minutes (Gemini flash-lite primary plus hedges). `NOTE_REASONING_EFFORT=low` keeps gpt-oss from burning output tokens on hidden reasoning; it is sent only to `nara`, `openrouter`, `groq` and `cerebras`, never to the generic slot or to Gemini.

With the 300-second deadline above, `--check` raises the *advisory* "the note request will time out on a slow gateway" warning (it assumes a 20 tok/s gateway). That warning is expected with the fast preset — the fast routes emit the output budget far quicker — and it never blocks the bot from starting.

## Failover and limits

Transient network, timeout and rate-limit errors get bounded retries, then the next eligible provider is tried. Oversized input is split within configured limits; an answer cut off at the output-token limit is not resent unchanged. Tune `NOTE_CONTEXT_TOKENS`, `NOTE_MAX_OUTPUT_TOKENS` and `NOTE_REQUEST_TIMEOUT_SECONDS` for the smallest/slowest model in the configured chain.

Two optional, default-off guards can shorten the wait on a hung or slow provider; neither changes what is asked or how answers are validated:

- **TTFB guard (`NOTE_TTFB_TIMEOUT_SECONDS`, default `0` = off).** On OpenAI-style routes only (`nara`, `openrouter`, `groq`, `cerebras`, `openai_compatible`), the request is streamed and the time to the first streamed byte is measured (gateway comment lines count as liveness). If no byte arrives within the threshold, the provider is abandoned and the chain fails over in seconds instead of waiting out the full request timeout, with no same-provider retry when a successor exists. The streamed deltas are reassembled and parsed by exactly the same response parser, in-body error checks and usage logging as a non-streaming answer, so a `length` finish still raises the output-budget error and is never resent unchanged. Gemini and Anthropic routes keep the non-streaming path.
- **Hedged racing (`NOTE_HEDGE_AFTER_SECONDS`, default `0` = off).** With at least two eligible providers, the first provider starts alone; only if it has not finished after the threshold does the second provider start in parallel. The first successful answer wins and the loser is cancelled. A provider that fails *before* the threshold behaves exactly as in the sequential chain (no race is started). A hedge adds at most one extra in-flight POST per pipeline call, so it can consume extra provider quota — enable it only when the tail latency is worth that cost.

Provider output is checked by the evidence gate before delivery. It is still third-party generated content; see [Note quality](NOTE_QUALITY.md).

## Check configuration

```powershell
python -m gamas_bot --check
```

On cPanel, use the same command with the bot's virtualenv Python. `python -m scripts.cpanel_preflight` additionally checks provider-host network access on the server. Do not use a live generation probe unless you intend to consume provider quota.
