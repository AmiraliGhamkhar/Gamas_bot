# LLM providers

Text generation is restricted to the approved allowlist: **Gemini**, **AvalAI**, and **OpenRouter**. Speechmatics and Deepgram are separate STT integrations and are unaffected.

## Configuration

| Provider | Key | Default model | Default API |
|---|---|---|---|
| Gemini | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` | Google Generative Language API |
| AvalAI | `AVALAI_API_KEY` | `deepseek-v4.1-flash` | `https://api.avalai.ir/v1` |
| OpenRouter | `OPENROUTER_API_KEY` | `nvidia/nemotron-3.5-super-vl:free` | `https://openrouter.ai/api/v1` |

`NOTE_PROVIDERS` can narrow the available allowlisted providers, but legacy or unknown providers are never admitted. Task policy takes priority over its ordering. Defaults: booklet generation/repair/compile -> AvalAI, Gemini, OpenRouter; key points, compact notes, small quizzes, flashcards, tables, slides and office documents -> Gemini, AvalAI, OpenRouter; 20-question and difficult quizzes -> AvalAI, Gemini, OpenRouter. OpenRouter is last-resort and requires its own credential and daily budget.

## Structured output and reliability

All JSON responses are parsed and validated locally; provider-specific request dialects are isolated in the router/profile adapter. Retries are bounded and provider failures fall through to eligible routes. Set `NOTE_HEDGE_AFTER_SECONDS=0` (the default) to prevent duplicate parallel requests. Model capabilities and quotas change; model identifiers and limits are configuration, not guarantees. Live provider compatibility has not been verified by ordinary tests.

## Quota budgets

AvalAI application defaults are 35 RPM, 750,000 TPM, and 3 in flight. OpenRouter defaults are 10 RPM, 15 RPD, and 1 in flight. Gemini account quotas vary and must be set from the actual AI Studio project; do not infer guaranteed free-tier limits. `LLM_PRIMARY_MAX_IN_FLIGHT` and `LLM_FALLBACK_MAX_IN_FLIGHT` constrain local routing. The shared SQLite/WAL ledger reserves provider RPM, RPD, estimated TPM (input estimate plus full output cap), and in-flight capacity under `BEGIN IMMEDIATE`, so separate processes sharing the same configured database coordinate reservations. Provider-reported total usage is reconciled when available; otherwise successful requests retain the full token reservation for the rolling minute. Failed requests release unused token reservations, while the bounded retry allowance is conservatively reserved against RPM/RPD. The existing bounded in-process metrics collector aggregates task/provider/model request counts, outcomes, estimates, actual usage, and latency without storing prompts or user identifiers. Conservative reservations can underutilize budget.

A SQLite circuit breaker opens for a provider after three consecutive transient/auth failures and allows one probe after the cooldown. Invalid requests and context/output budget errors do not count as provider-health failures.

## Sensitive data

`complete_json()` accepts `data_sensitivity` (`STANDARD_EDUCATIONAL`, `INTERNAL`, `SENSITIVE`, or `PHI_OR_PII`). Unclassified calls default to `UNKNOWN`, which excludes OpenRouter Free. For non-standard/sensitive classifications, every provider is excluded unless its explicit `*_ALLOW_SENSITIVE_DATA` setting is enabled. All three settings default to false. Standard educational classification is a caller assertion and must only be used when the job metadata/source review supports it. AvalAI is not presumed approved. Never put keys in URLs or logs.
