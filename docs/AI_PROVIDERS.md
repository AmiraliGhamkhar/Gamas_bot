# Gamas AI Provider Platform

This is the operational reference for the existing multi-provider note pipeline. It preserves the canonical structured-note schema and the current transcript/chunk/QA/repair/merge/DOCX/PPTX, billing, Telegram, encrypted-key, health, and deployment paths. Provider routing is an additional layer; it does not replace the note pipeline.

Provider plan and capability statements below were reviewed against provider-owned documentation on **2026-10-09**. Quotas and terms can change: the URLs in the registry are the source of truth. A `$0` price or a provider-wide “free” label is not enough to enable a model under `AI_FREE_ONLY`.

## Hard safety rules

1. Defaults are `AI_FREE_ONLY=true` and `AI_ALLOW_PAID_FALLBACK=false`. A paid route is never a fallback unless paid fallback is enabled, its route is deliberately marked paid, and the selected key is explicitly attested `paid`.
2. New and migrated credentials start with `billing_state=unknown`. Unknown keys cannot generate. Read-only catalog/health checks may use them. A key marked `free` is an administrator assertion that billing is disabled or a provider-side hard `$0` cap prevents paid overage. A key marked `paid` is explicit paid-use authorization. Clear the attestation to block generation again.
3. Key attestation does not establish that an individual model is free. FREE_ONLY requires an active, non-deprecated, text-capable model with compatible terms and no paid-billing gate. Known-paid is always blocked. An otherwise-unknown model is blocked unless it belongs to an account-verification provider and that deployment has an explicit no-overage account attestation plus a successful live account catalog for that exact model; the per-key no-overage attestation and all other gates still apply.
4. Restricted-use, trial-only, and region-restricted providers are not silently routed. NVIDIA hosted Developer endpoints, Cohere Trial API, and Cerebras trial credits remain blocked from production note routes. Z.AI’s Terms do not ban every educational use, but restrict specified education-related decisions and services requiring educational qualifications or professional review; Gamas conservatively blocks student-note generation pending scope clarification, despite selected models being listed Free.
5. Credential rotation is inside a provider leg; provider failover is the next layer. Retries, local RPM/TPM/daily caps, provider rate headers, and quota probes all participate in the decision.
6. Logs and student files never contain API keys, credentials/ciphertext, prompts, transcripts, raw model output, provider diagnostic bodies, or backend IDs. Provider-supplied error messages are bounded and secret-redacted before they can reach structured logs.

## Provider registry and current evidence

“Generation eligibility” is a provider-level gate; the model and per-key billing gates still apply.

| Provider | Registry class | FREE_ONLY status | Model / quota evidence and limitations |
|---|---|---|---|
| Google Gemini API / AI Studio (`gemini`) | Free plan | Reviewed model IDs can be considered; live catalog, live quota, and key no-overage attestation still apply | Reviewed seeds include Gemini 3.8 Flash and Gemini 3.5 Flash-Lite, retaining the Gemini 2.5 models as legacy choices. AI Studio free-tier limits are model-specific; linking billing can move requests to paid-tier pricing, so FREE_ONLY still requires the key's no-overage attestation. Gemini 3.x native schema and `thinkingConfig` are capability-gated. See [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [latest models](https://ai.google.dev/gemini-api/docs/generate-content/latest-model), and [GenerateContent](https://ai.google.dev/gemini-api/docs/generate-content). |
| NaraRouter (`nara`) | Free plan | Only the intersection of the account `/v1/models` list and the active public Free-plan model list | Current public `GET https://router.bynara.id/api/plans` Free plan lists `agnes-2.5-flash` (7,000,000 tokens/day, 15 RPM); it does **not** list legacy `agnes-3-flash`. The latter remains honored when configured, but is never silently called free. If the public plan fetch is unavailable, discovered models remain free-status `unknown`. See [Nara docs](https://router.bynara.id/docs), [plans](https://router.bynara.id/api/plans). |
| Groq (`groq`) | Account entitlement unverified | **Blocked** in FREE_ONLY | Its published rate-limit table is a Developer-plan baseline, not proof of a no-charge production entitlement. Account and model eligibility are not inferred from that table. Exact strict-schema support is model-specific; see [Structured Outputs](https://console.groq.com/docs/structured-outputs) and [rate limits](https://console.groq.com/docs/rate-limits). |
| OpenRouter (`openrouter`) | Free plan | `:free` models and the documented `openrouter/free` router only | The key endpoint provides free daily usage (`used`, `limit`, `remaining`); Gamas observes it and also maintains a conservative ledger. Public free accounts: 20 RPM and 50 requests/day; accounts with at least $10 purchased credits may receive a 1,000/day free-model limit. See [OpenRouter docs](https://openrouter.ai/docs). |
| Mistral La Plateforme (`mistral`) | Free mode exists; account-scoped entitlement | **Blocked until an administrator attests Free mode/no-overage and the key has a live account catalog** | Official usage docs distinguish included monthly Free-mode usage from Pay-as-you-go usage beyond it; no documented read-only API verifies the account's active mode. The registry therefore requires provider-account attestation. `mistral-small-latest` is a capability seed, not proof of free quota; model access must be present in the live account catalog. Account attestation does not override a paid model, expired/deprecated model, terms, or per-key billing checks. JSON-object mode is distinct from schema enforcement. See [usage limits](https://docs.mistral.ai/admin/billing-usage/usage-limits), [Free mode](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key), [JSON mode](https://docs.mistral.ai/capabilities/structured_output/json_mode), and [structured output](https://docs.mistral.ai/capabilities/structured_output/custom). |
| SambaNova Cloud (`sambanova`) | Free tier when no payment method is linked | Reviewed production models + per-key no-overage attestation | The official rate-limit table lists `DeepSeek-V3.1`, `Meta-Llama-3.3-70B-Instruct`, and `gpt-oss-120b` at 20 RPM / 20 RPD / 200K TPD each on the Free Tier. Preview models are not seeded for production routing. JSON Schema is enabled only for the documented DeepSeek model; Llama and GPT OSS use JSON-object mode absent stronger exact-model evidence. Gamas uses conservative provider-level caps and runtime rate headers. See [rate limits](https://docs.sambanova.ai/docs/en/models/rate-limits), [function calling / JSON mode](https://docs.sambanova.ai/docs/en/features/function-calling), and [chat completions](https://docs.sambanova.ai/docs/api-reference/chat-completions/create-chat-based-completion). |
| Z.AI GLM (`zai`) | Free plan, mixed catalog | **Conservatively blocked from all Gamas note routes** | Pricing lists `glm-4.5-flash` and `glm-4.7-flash` as Free. The [Terms of Use §III.06](https://docs.z.ai/legal-agreement/terms-of-use) restrict education-related decision-making that may materially affect rights/well-being, plus services requiring educational qualifications or professional review; this is not a blanket ban on every educational use. Because Gamas generates student learning notes without integrated educator review, the provider remains blocked pending scope clarification. Price or key attestation does not override the terms gate. JSON mode and reasoning controls remain model-specific for future policy review. See [pricing](https://docs.z.ai/guides/overview/pricing), [structured output](https://docs.z.ai/guides/capabilities/struct-output), [thinking](https://docs.z.ai/guides/capabilities/thinking). |
| NVIDIA NIM / `build.nvidia.com` (`nvidia`) | Trial-only | **Blocked**, including paid fallback | Hosted Developer Program endpoints are for prototyping/development; production NIM requires the applicable NVIDIA AI Enterprise license. Commercial use is prohibited by this registry entry. See [NVIDIA Build docs](https://build.nvidia.com/docs). |
| Cloudflare Workers AI (`cloudflare`) | Account/plan based | **Requires account no-overage attestation + live Model Search; paid-only model IDs are blocked and other IDs remain unknown until account attestation** | Workers AI includes 10,000 Neurons/day; Workers Paid can bill usage beyond it. The provider-level no-overage account attestation and Gamas' conservative local Neuron guard are required. Account Model Search (OpenRouter-format) provides IDs, declared input modalities, context/output metadata, and supported-parameter hints; catalog presence and reported `$0` prices do not establish free quota. Other IDs retain `unknown` free status; the official pricing docs identify seven model IDs requiring paid billing, which are recorded as paid and have `requires_paid_billing`. `CLOUDFLARE_ACCOUNT_ID` is required. See [pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) and [Model Search API](https://developers.cloudflare.com/api/resources/ai/subresources/models/methods/list/). |
| Hugging Face Inference Providers (`huggingface`) | Paid only / experimental | **Blocked** | Free HF users have no monthly inference credit; use requires purchased credits or a paid subscription. Experimental and not a primary/free route. See [pricing](https://huggingface.co/docs/inference-providers/pricing). |
| Alibaba Cloud Model Studio (`alibaba`) | Region restricted | **Not auto-routed; explicit unlock, account attestation, eligible region/service-scope pair, future quota expiry, matching regional endpoint, and live model catalog required** | The official free-quota guide describes 90-day validity and model/account-specific token quotas. Gamas accepts only Beijing + `china_mainland` and Singapore (`ap-southeast-1`) + `international` for FREE_ONLY; other pairs fail closed. Before attesting no-overage, the admin must verify the chosen model's remaining quota and expiry in Alibaba's console and enable its model-level **Free Quota Only** safeguard. Gamas also records the production environment marker; the credential Base URL must match the selected region (Singapore's documented regional DashScope or workspace endpoint; the key editor supports per-credential Base URLs). Add Alibaba to a task route explicitly. See [new free quota](https://help.aliyun.com/en/model-studio/new-free-quota), [Singapore regional access](https://help.aliyun.com/en/model-studio/singapore-regional-access-information), [regional Base URLs](https://help.aliyun.com/en/model-studio/base-url), [OpenAI-compatible API](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope), and [pricing](https://help.aliyun.com/en/model-studio/model-pricing). |
| Cohere Trial API (`cohere`) | Trial-only | **Blocked** | Trial credentials are for evaluation, not production/commercial note generation. See [Cohere docs](https://docs.cohere.com/docs/rate-limits) and the provider terms. |
| Cerebras (`cerebras`) | Trial-only | **Blocked** | A time-limited trial credit/payment-method requirement is not a free production plan. Kept for evaluation/benchmarks only. Structured-output support is per model/tier; see [structured outputs](https://inference-docs.cerebras.ai/capabilities/structured-outputs), [rate limits](https://inference-docs.cerebras.ai/support/rate-limits). |
| Anthropic (`anthropic`) | Paid only | Blocked unless an explicit paid route is configured | Kept for backward compatibility. See [pricing](https://platform.claude.com/docs/en/about-claude/pricing). |
| Generic OpenAI-compatible (`openai_compatible`) | Paid / unknown gateway | Blocked as free unless hostname resolves to a reviewed provider | The canonical resolver preserves gateway deployments such as the current NaraRouter base URL. The configured `NOTE_API_MODEL` is preserved but is not granted free status by aliasing. |

Cloudflare's current pricing page names these paid-billing-only IDs, which are rejected before a generation request in FREE_ONLY: `@cf/moonshotai/kimi-k2.6`, `@cf/moonshotai/kimi-k2.7-code`, `@cf/zai-org/glm-5.2`, `@cf/zai-org/glm-5.3`, `@cf/zai-org/glm-5.3-flash`, `@cf/deepseek-ai/deepseek-v4-flash-0731`, and `@cf/deepseek-ai/deepseek-v4-pro-0813`.

The provider facts are mirrored in the persistent `ai_provider_registry` table for admin visibility; last successful catalog and quota observation times are stored separately from code-reviewed policy. The global routes do not insert Alibaba automatically. Providers that require account verification (Groq, Mistral, Cloudflare, Alibaba) remain blocked until their account attestation is present.

The experimental providers HF, Alibaba, Cohere, and Cerebras remain registered for discovery, admin visibility, or benchmarking as appropriate; they are not silently inserted into a production FREE_ONLY route. NVIDIA is held out even when experimental settings are unlocked because the reviewed hosted terms are development-only and commercial-prohibited.

## Account-entitlement attestation

Some providers publish quotas or a possible free mode but **no documented
read-only API that proves this deployment's current account mode and
no-overage state**:

* Groq's published rate-limit table is the *Developer*-plan baseline and does
  not establish a no-charge production entitlement.
* Mistral Free mode includes monthly usage within displayed model limits, while
  Pay-as-you-go extends beyond the included amount; the key/account's current
  mode is not read through a documented endpoint.
* Cloudflare Workers AI includes 10,000 Neurons/day, but Workers Paid can bill
  above it; Gamas cannot read the account's billing/overage setting.
* Alibaba's free quota is region/account/model-specific and time-limited; Gamas
  cannot read the console's exact per-model balance, expiry, or
  **Free Quota Only** switch.

These providers carry `requires_account_verification` in the registry and are
blocked from `AI_FREE_ONLY` routes until an administrator attests them:

**AI → provider → 🧾 تأیید استحقاق حساب**

The attestation is stored per provider (`account_entitlement_attested_at` and
`account_entitlement_attested_by_admin_id`) and is audited and revocable. It
allows an account-attested provider to consider an otherwise-unknown model
only after that exact model appears in a successful live account catalog. It
**never** relaxes:

* the per-key billing attestation (a key must still be attested `free`),
* terms-of-use, deprecation, region, live-model-availability, or
  `requires_paid_billing` gates,
* the local request/token/unit quota ledger,
* Alibaba's explicit provider unlock, production environment marker, supported
  region/service-scope pair, matching endpoint, and future quota expiry. The
  administrator's account/model review must also confirm a live remaining
  quota and Alibaba's model-level **Free Quota Only** safeguard; Gamas cannot
  read either setting.

Without both account attestation fields the provider is skipped with
`account_entitlement_unverified`, visible in provider/route panels. This is
the conservative default: Gamas will not infer a free account from a rate
limit or a `$0` model row.

## Extra-pass overrides

The outline, QA-repair and final-compilation calls each cost free-tier quota on
top of the chunk calls, so restrictive free providers default them off (Groq,
OpenRouter, Cloudflare, Cohere, Cerebras). An administrator can override the
default per provider:

**AI → provider → ✅/❌ طرح‌کلی / تعمیر / تلفیق**

The toggle cycles **inherit → explicit → inherit**, so an override can always
be removed. The panel shows whether the current value came from the provider
profile or from an administrator. `PlannedRoute.pass_state()` reports the same
origin, and `NoteJobSession` proxies it, so every call site that can spend
quota sees the same decision.

## Non-token metering (Cloudflare Neurons)

Cloudflare Workers AI meters inference in **Neurons**, not tokens, and its
OpenAI-compatible endpoint returns token usage only — no neuron count. Gamas
therefore estimates neuron spend locally so the documented daily inclusion
(10,000 Neurons/day, last verified 2026-10-09) can be protected:

* the estimate uses a deliberately **over**-estimated rate
  (25 Neurons per 1,000 tokens) — over-counting only makes Gamas fail over to
  the next free provider early, whereas under-counting would let a job walk
  past the allocation and onto a billable plan;
* actual token usage from the provider is used when it is reported, otherwise
  the pre-flight estimate plus the reserved output budget is charged;
* the running total for the UTC day is shown under **AI → provider**, labelled
  as an estimate;
* when the estimated total reaches the budget, `AI_FREE_ONLY` stops routing to
  Cloudflare and uses the next eligible free provider;
* the per-deployment budget is configurable (`neuron_budget_daily` in
  `ai_provider_settings`) because the included allocation is plan scoped.

Models that cannot run on the free allocation at all are flagged
`requires_paid_billing` from the Models panel (💳), which makes `FREE_ONLY`
reject them up front instead of discovering it after the quota is spent.

## Model capability and discovery policy

`gamas_bot/ai/models.py` keeps reviewed static seeds, a live SQLite catalog, and conservative fallback metadata. Exact reviewed capabilities and other seed facts fill gaps when a generic catalog omits them; live non-unknown free-status facts may supersede a seed. An incomplete `unknown` catalog row cannot erase a reviewed status. Model deprecation is latched once recorded (catalog sync does not silently revive it); a model absent from a successful snapshot becomes unavailable, while historical rows/usage remain. Unknown models receive only the common chat request and prompt-enforced JSON; unsupported multimodal, schema, reasoning, and tool options are not inferred by a name pattern.

Provider-specific notes:

* NVIDIA "Free Endpoint" availability is a live capability, not a guarantee. Discovery records `free_endpoint` per model; a sync that omits the flag does not clear a previously observed value, and a model that disappears from the catalog is marked unavailable (never deleted, so historical usage stays attributable). The Models panel suggests a live replacement for a deprecated or withdrawn model, preferring a free endpoint, then a non-deprecated model, then the largest context window.
* Gemini native schema support and Gemini 3 reasoning levels are advertised only for reviewed exact model IDs. Gemini 3.x defaults omit temperature and send `thinkingConfig` only for an explicitly selected supported reasoning level.
* Groq strict schema support is exact-model only (`openai/gpt-oss-20b`, `openai/gpt-oss-120b`, and `qwen/qwen3.8-27b`; the guard model has best-effort behavior). Groq eligibility remains account-unverified until attested and live-discovered.
* SambaNova seeds only the three documented Free Tier production models. JSON Schema is enabled for DeepSeek-V3.1; Llama 3.3 and GPT OSS use JSON-object mode unless exact live/reviewed evidence strengthens it.
* Cerebras constrained decoding is limited by its model and availability tier; it is not generalized to the provider.
* Mistral JSON-object mode is not JSON Schema. Its public examples do not prove account mode; FREE_ONLY needs account attestation and a live account catalog.
* Nara plan discovery marks only the intersection of the active public Free plan and that account’s authenticated model list. Unknown plan data never replaces a previous valid catalog.
* Cloudflare pagination is bounded. Model Search contributes account-listed model IDs and declared architecture/parameter metadata; Gamas extracts only explicit modalities, context/output caps, and supported-parameter hints. It ignores `$0` pricing as free evidence and applies the provider-owned paid-billing model list. A failed, empty, malformed, or truncated refresh leaves the last valid catalog intact. The catalog is not proof of account billing/no-overage state.

Generation strategy is selected from the exact model capability record: strict JSON Schema → JSON Schema → Gemini native schema/MIME → JSON object → prompt-enforced JSON. An explicit provider rejection can trigger one supported downgrade; the canonical Gamas note schema, output parser, QA/repair, and note-scrubbing rules remain authoritative.

## Request budgets and legacy compatibility

`gamas_bot/ai/profiles.py` holds conservative per-provider chunk-token/character/output budgets, local RPM/TPM/daily caps, retry counts, concurrency, and pipeline-pass policy. Nullable `ai_provider_settings` overrides let an admin tune chunk-token/character, output/compile budgets, retry count, timeout, and max concurrent requests; `NULL` inherits the reviewed profile. The Telegram provider panel bounds and audits these changes. A dynamic provider-scoped limiter applies the effective concurrency setting in addition to the existing global job semaphore. Provider-supplied rate/quota headers refine the ledger. Retry caps are bounded by the provider profile and `AI_MAX_GENERATION_RETRIES`; failed attempts count against local budgets. Before each attempt, the router projects input plus the full output-token cap against RPM/TPM/daily ledgers and atomically reserves that budget across in-flight workers in the bot process; recorded usage is committed before the reservation is released. This is a conservative local guard, not a replacement for the provider's own cap or the required no-overage billing attestation. A quota-ledger read failure fails closed.

Routes are stored per `(service, task_type)` for outline, chunk structuring, repair, final compilation, health tests, and benchmarks. Admins can reorder/enable/disable each task route, set its FREE_ONLY/paid lane, and choose a cached/live model per route. The selected model is still rejected at execution if it is paid, expired/deprecated, unavailable, term-restricted, non-text, requires paid billing, or not live-discovered where account verification is required. Legacy chunk callbacks and `NOTE_API_PROVIDER=openai_compatible` gateway resolution are preserved.

All existing `NOTE_API_*` options remain parsed and operational. `NOTE_API_TIMEOUT`, `NOTE_API_RETRIES`, and `NOTE_API_MAX_OUTPUT_TOKENS` are upper bounds on the profile budget (they may reduce, never inflate, a provider-specific safety limit). `NOTE_API_PROVIDER`, `NOTE_API_KEY`, `NOTE_API_BASE_URL`, `NOTE_API_MODEL`, `NOTE_API_EXTRA_HEADERS_JSON`, and `NOTE_API_JSON_MODE` remain intact. In particular, the existing `NOTE_API_PROVIDER=openai_compatible` + Nara base URL + `agnes-3-flash` stays configured as-is; that model is not silently relabeled free.

## Credential billing attestation

Migration `007_provider_billing_attestations.sql` adds `billing_attested_at` and `billing_attested_by_admin_id`, resets every pre-existing credential to `unknown`, and clears any unaudited legacy flags. A generation lane requires the state plus both attestation metadata fields. The encrypted secret and existing audit log are reused.

In **AI → provider → keys**, the admin may attest:

* **FREE/no-overage** only after confirming billing is disabled or a provider-side hard `$0` spend cap makes any charge impossible. A free quota by itself is not that safeguard.
* **Paid** only after explicitly authorizing billable requests. This does not turn on `AI_ALLOW_PAID_FALLBACK`; that remains a deployment-level opt-in and paid route order remains last.
* **Clear** to return to `unknown` and block generation.

Environment-backed legacy keys remain configured, and may be used for read-only discovery/health, but are not auto-attested. To generate safely from one, add the same secret to the encrypted key vault and attest it; the existing `NOTE_API_*` values are not rewritten. Telegram key messages are deleted before storage; if message deletion fails, storage is refused. Audit and UI only show labels, billing state, attestation timestamp, and a masked suffix.

## SQLite migrations

Migrations are forward-only and applied in filename order by `Database.open`:

* `006_ai_provider_platform.sql`: model catalog, provider settings/routes, usage and quota ledger, structured events, and additive credential metadata.
* `007_provider_billing_attestations.sql`: additive billing attestation/output-estimate fields, canonical usage index, and fail-closed reset of all existing credentials to `unknown`.
* `008_provider_entitlement_and_pass_policy.sql`: account-entitlement attestation, extra-pass overrides, Neuron budget/rollup and region/quota metadata; existing account attestations are reset fail-closed.
* `009_provider_registry.sql`: new secret-free persistent provider facts table; successful catalog/quota observation timestamps are updated separately.
* `010_provider_policy_overrides.sql`: nullable, bounded per-provider request-budget, timeout, retry and concurrency overrides (`NULL` inherits profile defaults).
* `011_alibaba_service_scope.sql`: additive service-deployment-scope metadata, kept separate from Gamas' production environment marker so region/scope eligibility can fail closed.

No encrypted secret is rewritten or exposed. Existing usage rows, audit records, routes, note schema, and historical models are retained.

## Admin and operations

The Telegram AI panel covers provider policy and account entitlement, encrypted keys, billing attestations, task-scoped routes and model choices, per-provider request-profile/concurrency overrides, Alibaba's explicit unlock and region/service-scope/production/quota-expiry metadata, extra-pass overrides, health, read-only catalog sync, quota/usage summaries (including estimated Neuron spend), redacted logs with status/date/error filters, dry-run request shape, and benchmark results. Credential activation is refused until its billing state and attestation metadata are explicitly set.

* **🩺 سلامت** — last known probe state per credential (status, HTTP, latency, last checked, `read_only` vs `generation`). Opening the panel never submits a generation request; the explicit refresh re-runs the read-only probes and logs a `provider_health_checked` event with `check_type=read_only`. Only the per-key "test request" action sends a real completion, logged with `check_type=generation`.
* **⚙️ تنظیمات** — the deployment switches (`AI_FREE_ONLY`, `AI_ALLOW_PAID_FALLBACK`, routing, sync TTL, failover and retry budgets, quota safety margin) and every provider-level override currently stored. They are read-only here on purpose: flipping a free-only guard casually is exactly how a deployment crosses a provider's daily cap.
* **🧾 لاگ‌ها** — filterable by provider, event type, HTTP status class (4xx/5xx), error class, job id and date (today / 7 days). Every stored field is metadata: `ai_events` never contains prompts, transcripts, model output or secrets.

Health/catalog probes are read-only; the separate generation-test path is explicitly billable and must never be mistaken for a health probe. Local usage counters cannot be manually reset from the admin UI because doing so could bypass a provider’s daily cap.

`python -m scripts.validate_provider_platform` runs an offline end-to-end validation of the platform (legacy NaraRouter compatibility, provider-aware chunk budgets, provider-level failover on a 429, the FREE_ONLY and paid-fallback gates, backend-metadata scrubbing and secret hygiene) against scripted provider responses. It needs no API key.

Start-up checks, backups, deployment, and incident response are in [OPERATIONS.md](OPERATIONS.md). Key encryption, log redaction, note scrubbing, and student-data protections are in [SECURITY.md](SECURITY.md). The required test/benchmark commands are in [TESTING.md](TESTING.md).

## Known limitations / not verified

* No real provider API key or account was used in CI. Per-account billing, exact provider quota state, regional eligibility, and overage settings cannot be verified by Gamas; an administrator must attest them.
* Groq's Developer-plan limits do not establish a free production entitlement. Groq remains blocked until account entitlement is explicitly attested and the selected model is live-discovered; this is an operator assertion, not provider verification.
* Nara's active Free-plan catalog currently lists `agnes-2.5-flash`, not legacy `agnes-3-flash`; account model access must still agree with the public plan result.
* Mistral's live catalog proves model access, not the account's Free versus Pay-as-you-go mode. Both account no-overage and per-key billing attestations are required for FREE_ONLY.
* Cloudflare Model Search supplies account-listed IDs and selected declared capabilities, while the official pricing list blocks known paid-only IDs. Other IDs stay free-status `unknown`; neither catalog presence nor `$0` price establishes free quota. The account no-overage attestation and local Neuron guard are required, and Gamas cannot read the actual billing state.
* Alibaba's official free-quota page states 90-day validity and model-specific balances; the service-scope pair is cross-checked against the [Singapore regional-access documentation](https://help.aliyun.com/en/model-studio/singapore-regional-access-information). Gamas does not read the account's per-model remaining quota, expiry, or Free Quota Only setting. Admins must confirm these in Alibaba's console and use a credential Base URL matching the recorded region; otherwise keep the provider locked.
* SambaNova's published limits apply to its listed production Free Tier models; provider access and no-payment-method state still require administrator review and key attestation. Preview models are not treated as production-free.
* A provider-account or `free` key attestation is an operator statement, not cryptographic proof. Billing settings, commercial terms, and quotas can change later; re-review provider account settings and the official links regularly.
* Provider catalogs and quotas can change after the 2026-10-09 review; automatic discovery cannot prove commercial rights, deprecation policy, or account-level billing state.
* Cloudflare Neurons are estimated, not measured: the provider does not return a neuron count, so the daily-inclusion guard uses a deliberately pessimistic conversion. Treat the panel figure as an upper bound.
