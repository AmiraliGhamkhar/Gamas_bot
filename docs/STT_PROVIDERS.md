# Gamas Speech Platform: STT providers, policy and routing

This document describes the STT platform as it is implemented in this
increment, the evidence behind each provider record, and what is **not** done.
Read it before enabling a provider or changing `STT_*` settings.

The short version:

* Speechmatics and Deepgram keep working exactly as before and are the operator's
  priority. They are the default trial allowlist (`STT_TRIAL_ALLOWLIST`).
* Native adapters exist for Groq, Gemini Transcribe and the OpenAI-compatible
  endpoint, plus the scaffolded adapters for AssemblyAI, Gladia, Google Cloud,
  IBM Watson, Azure, Soniox and ElevenLabs. Every native provider is **disabled
  or unvalidated** until its keys are configured and it passes a real-provider
  smoke test (not yet run; see [Not done](#not-done)).
* Routing is explicit. The default order is unchanged, and no provider is picked
  from marketing claims. Accuracy ranking must come from benchmarks.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `STT_PRIMARY` | `speechmatics` | First provider in the route. Any registry slug is accepted. |
| `STT_DEFAULT_ROUTE` | empty | Explicit comma-separated route. When set it replaces the legacy chain entirely. |
| `STT_FALLBACK_ENABLED` | `true` | `false` runs only the first eligible provider. |
| `STT_MAX_PROVIDER_FAILOVERS` | `4` | Upper bound on providers one job may try. |
| `STT_FREE_ONLY` | `true` | Master switch. Trial and paid providers are gated while it is on. |
| `STT_TRIAL_ALLOWLIST` | `speechmatics,deepgram` | Trial-class providers allowed while `STT_FREE_ONLY=true`. |
| `STT_ALLOW_TRIAL_PROVIDERS` | `false` | Global opt-in for every trial-class provider. |
| `STT_ALLOW_PAID_FALLBACK` | `false` | Allows paid-only providers, always after every free/trial candidate. |
| `STT_QUOTA_SAFETY_MARGIN` | `0.10` | Fraction of each budget kept in reserve. |
| `STT_QUALITY_GATE_ENABLED` | `true` | Hard transcript checks (see below). |
| `STT_REQUEST_DIARIZATION` / `STT_REQUEST_WORD_TIMESTAMPS` | `false` | Feature requests. Native providers that cannot serve them are skipped with a reason. |

Defaults are fail-closed. `STT_FREE_ONLY=true` with no allowlist change keeps
the legacy Speechmatics, Deepgram and OpenAI-compatible behaviour, and adds no
new trial or paid provider to any route.

## Classification

Every provider has one `free_type` in `gamas_bot/stt_platform/registry.py`.
A provider is never "free" because its website shows a "Start free" button.

| Class | Gate in free-only mode | Providers today |
| --- | --- | --- |
| `permanent_free`, `free_monthly`, `free_allocation` | allowed | Groq (`free_allocation`), Gemini Transcribe (`free_allocation`), Gladia, Google Cloud STT, IBM Watson (`free_monthly`) |
| `free_credit`, `promotional_free`, `trial_only` | only via `STT_TRIAL_ALLOWLIST` or `STT_ALLOW_TRIAL_PROVIDERS` | Speechmatics, Deepgram, AssemblyAI (`free_credit`); AWS Transcribe (`promotional_free`) |
| `paid_only` | only via `STT_ALLOW_PAID_FALLBACK`, after free legs | Azure Speech (F0 is realtime-only), Soniox, OpenAI-compatible gateways |
| `region_restricted`, `unsupported` | never routed | none |

Operator-configured legacy providers (speechmatics, deepgram, openai_compatible)
keep their pre-platform behaviour. A paid-only `openai_compatible` endpoint stays
usable because the operator chose it. Gamas cannot tell whether a custom base URL
is billed, so the decision carries the warning `openai_compatible_endpoint_billing_unverified`.

An admin billing attestation of `paid` (`stt_provider_settings.billing_state`)
blocks free and trial use of that provider in free-only mode. Gamas then never
moves a job onto an account that is card-billed.

### Evidence per provider

`evidence` records how each record was checked. Only `official_docs`,
`official_pricing_partial` and `official_blog` count as official. `last_verified_at`
is set only for those. The other providers are `unverified` or
`search_snippet`, and must not be routed on the strength of their numbers.

Gemini Transcribe (transcribe + pricing pages) and Groq (speech-to-text +
rate-limits pages) were re-verified against the live official documentation on
**2026-10-10**; every recorded claim (models, limits, quota numbers, the
30-minute diarization/timestamp cap, the free-of-charge tier and the free-tier
training policy) matched the current pages.

| Provider | Evidence | Free facts (as recorded) | Notes |
| --- | --- | --- | --- |
| Gemini Transcribe (`gemini_transcribe`) | `official_docs` | Free tier with rate limits; caps not published in the pages read | Free tier content is **used by Google to improve its products**; paid is not. Caps Unknown. Standard limit 1 h; 30 min with diarization or word timestamps. `custom_vocabulary` cannot be combined with diarization or timestamps. |
| Groq (`groq`) | `official_docs` | Free plan: 20 RPM, 2,000 RPD, 7,200 ASH, 28,800 ASD for whisper-large-v3 variants | 25 MB free-tier file limit. Audio-second quotas are not in response headers and are tracked locally. |
| Deepgram (`deepgram`) | `official_docs` | $200 one-time credit, no card, pay-as-you-go afterwards | 504 for requests over 10 min (Nova) or 20 min (Whisper). Persian is supported in batch Nova-3. |
| Speechmatics (`speechmatics`) | `official_pricing_partial` | $100 one-time credit (pre-2026-08-01 accounts: $25), no card | Pauses at zero balance without a card. |
| AssemblyAI (`assemblyai`) | `official_pricing_partial` | $50 one-time credit | Expiry sources disagree. Persian is not in every model's language list. |
| Soniox (`soniox`) | `official_blog` | Free API credits discontinued for new sign-ups (2025-10-27) | `paid_only`. Default model `stt-async-v5` (current per the Soniox changelog; `stt-async-v3` was retired 2026-02-28). |
| Gladia, Google Cloud STT, IBM Watson, AWS Transcribe, Azure Speech, ElevenLabs Scribe | `search_snippet` | Gladia 10 h/month + €50; Google 60 min/month (V1); IBM Lite 500 min/month; AWS 60 min/month for 12 months; Azure F0 5 h/month realtime-only; ElevenLabs 10,000 shared credits | Not verified against the official pages. Azure, AWS and ElevenLabs are disabled. Gladia and ElevenLabs are not Persian-capable in the registry. |
| OpenAI-compatible (`openai_compatible`) | `generic_gateway` | Depends on the gateway | Operator-supplied endpoint. |

**Quota numbers are not invented.** A budget without an admin-entered value is
shown as Unknown. A budget row that cannot be reserved denies the candidate.

**`quality_score` in the registry is an unbenchmarked placeholder hint.** It is
shown in the advisory score only and never reorders providers. Benchmarks must
replace it before anyone relies on it.

## Routing

1. The effective route is resolved in this order (spec §28/§61):
   1. the admin-edited `stt_routes` table (the 🎙 STT Platform → «مسیرها» panel),
      when it holds at least one enabled row;
   2. `STT_DEFAULT_ROUTE` when set;
   3. `STT_PRIMARY` followed by `speechmatics`, `deepgram`, `openai_compatible`
      in that order (the pre-platform behaviour).
2. Each `stt_routes` row can also carry a per-leg `model_override`, which is
   applied only for that route leg through the provider's own model field
   (Speechmatics `model`, Deepgram `model`, native `*_STT_MODEL` mapping).
3. Every candidate is evaluated and gets either `eligible` or a list of stable
   denial reasons: `trial_not_allowed`, `paid_not_allowed`, `not_configured`,
   `admin_disabled`, `billing_attested_paid`, `persian_unsupported`,
   `language_unsupported`, `file_too_large`, `duration_too_long`,
   `feature_unsupported:*`, `provider_disabled`, `unknown_provider`.
4. Eligible candidates run in route order. Paid-tier candidates always run after
   every free, trial and legacy candidate.
5. `fa` is a hard capability requirement. A provider without `persian_batch` is
   never given Persian audio.
6. `auto` and `multi` are sent only where the provider documents them. Otherwise
   the provider's documented omission or language identification is used, or the
   candidate is refused with `language_unsupported`.
7. The user's subscription narrows the policy per job (spec §52): a **free**
   user's job is always routed free-only even when the deployment enabled paid
   fallback; a **paid** user's job runs under the deployment's configured
   `STT_*` policy. The tier can never widen access, and users cannot override
   provider security.

`scripts/benchmark_stt.py --plan sample.wav` prints the decision for every
engine without sending audio.

## Quotas and budgets

* Budgets are admin-maintained rows in `stt_quota_budgets`. A row fixes
  `(provider, account_scope, quota_type)`. Supported units: `audio_seconds*`,
  `minutes*`, and request counts (`rpm`, `rpd`, `requests`).
* Before each credential attempt the job reserves its units atomically, with the
  safety margin applied. A successful call commits the reservation, and a failed
  call releases it. A quality rejection after a successful call still commits,
  because the provider billed for it.
* With no budget row the candidate stays eligible. With a budget row that cannot
  be reserved (exhausted, stale, unknown units) the candidate is denied.
* Provider headers, when the adapter reads them, are recorded in
  `stt_quota_snapshots`. Nothing is guessed when a header is absent.

## Quality gate and confidence

Hard failures reject a transcript and move to the next provider:

* empty text, no alphanumeric content, massive repeated sequences;
* an embedded provider error payload (only for texts up to 400 characters, so a
  lecture about API keys is not rejected);
* a Persian job whose text is almost entirely non-Persian letters (at least 200
  letters are needed before this check applies, and the Persian share must be
  below 20%);
* `too_much_text_for_duration` (only for audio of 30 s or more).

Soft signals (`too_little_text_for_duration`, `excessive_repetition`,
`high_words_per_second`, low non-whitespace ratio) are warnings only.

Confidence is never fabricated. A native adapter passes its provider confidence on
only when `NormalizedConfidence.comparable` is true. The legacy rule is unchanged:
a low confidence triggers the next provider, and the more confident outcome wins.

## Privacy and data use

* Provider records carry `data_training_policy`, `retention_policy`
  (`data_retention`), `data_region`, `medical_compliance` and `commercial_use`
  (spec §53). Values are only recorded where the provider's own documentation
  supports the claim; everything else stays `unknown` / `none_claimed`, and the
  admin panel shows that literally. Gamas never labels a provider "HIPAA-safe",
  "GDPR-safe" or "no-training" on marketing copy.
* Keys are stored as Fernet ciphertext only. Keys travel in request headers,
  never in URLs. Gemini and Google Cloud STT both send the key as
  `x-goog-api-key`, per Google's guidance to avoid the `key=` query parameter. This
  was changed in this increment and has not been exercised against the live APIs.
* Events and usage rows carry codes, provider names, sizes, timings and HTTP
  status only. Transcripts, audio and raw provider responses are never logged.
* A free tier may let the provider use the content to improve its products. For
  Gemini, the official pricing page says so for the free tier and says no for the
  paid tier. **Do not send confidential or medical audio to a free tier.** For
  medical audio the admin panel surfaces a warning whenever the selected
  provider's `medical_compliance` is `none_claimed` (the default).

## Operations

The 🎙 STT Platform admin screens (Telegram: «⚙️ پنل مدیریت» → «🎙 پلتفرم STT»,
namespace `admin:stt`, implemented in `gamas_bot/admin_stt.py`) cover:

* **Providers** — registry-backed cards (protocol, auth, free class, limits,
  languages, batch/realtime, privacy fields) with enable/disable and
  `billing_state` attestation (`unknown` / `free` / `paid`);
* **Models** — the `stt_model_registry` cache and reviewed seeds; a model can be
  marked `DEPRECATED` (never deleted) and the panel offers a replacement
  recommendation (spec §48/§49);
* **Routes** — the data-driven route editor (move/enable/disable/remove/add,
  per-leg `model_override`) writing `stt_routes`, plus the policy summary
  (Free-only / Trial / Paid fallback) (spec §61);
* **Quotas** — admin-entered budget ceilings (`stt_quota_budgets`) and observed
  snapshots (`stt_quota_snapshots`) with the safety margin applied; values the
  provider never exposed are shown as **Unknown**, never manufactured (spec §62);
* **Health** — READ_ONLY_HEALTH probes by default; GENERATION_TEST only through
  the explicit test button (spec §38);
* **Logs** — `stt_provider_events` with failure filtering (spec §44);
* **Test tool** — metadata-only or one of three sample transcriptions
  (10-second / Persian / medical) from `tests/fixtures/stt/samples/`; if the
  fixture is not installed nothing is sent, and neither audio nor transcript
  content is ever displayed or logged (spec §39/§64);
* **Dry-run** — a redacted, provider-native request preview (endpoint, method,
  headers redacted, form fields, payload structure) with no audio and no secret
  (spec §63);
* **Benchmarks** — how to run `scripts/benchmark_stt.py` and the reviewed
  scoring weights.

The API-key wizard («افزودن کلید (Wizard)») lists **every** STT registry
provider with its metadata card and model list. The secret is sent alone, the
message is deleted before persisting — if deletion fails the key is **not**
stored (spec §36).

Route precedence detail: `stt_routes` rows win over `STT_DEFAULT_ROUTE`, which
wins over the legacy `STT_PRIMARY` chain. An empty `stt_routes` table (the state
after upgrading) keeps the pre-platform behaviour unchanged (spec §66).

`stt_provider_events` and `stt_usage_records` hold the audit trail, with the
events listed in `gamas_bot/stt_platform/events.py`. Per-provider concurrency is
independent of `MAX_CONCURRENT_JOBS` (provider profile 1-2); a third concurrent
job for one provider waits in a queue and does not fail (spec §42/§69).

## Benchmarking

`gamas_bot/stt_platform/benchmark_metrics.py` is the offline metric library
(WER, CER, terminology/numeric preservation, punctuation, quality signals and
the weighted provider score). `scripts/benchmark_stt.py` measures configured
engines on real fixtures; the ten benchmark categories and the fixture layout
are documented in `tests/fixtures/stt/README.md`. No production keys are needed
in CI and no transcript text is written to results.

## Not done

* Real-provider production validation. There are no keys in CI, and no live call
  was made in this increment. Native adapters are covered by offline fakes.
* Real-fixture benchmarks. The benchmark tooling is complete (route plan,
  metrics, profile column, weighted score, evidence column), but no measured WER
  or latency has been recorded on Gamas fixtures, so no provider is ranked and
  the registry `quality_score` values remain unbenchmarked placeholders.
* AWS Transcribe, Azure Speech, Soniox and ElevenLabs Scribe remain scaffolds,
  disabled. Soniox is classified `paid_only` (free credits discontinued for new
  sign-ups) and ElevenLabs Scribe is **not** classified as permanently free.
* The `x-goog-api-key` header for Gemini and Google Cloud STT, and the Gemini
  `interactions` path, have not been exercised against the live APIs.
* Gladia, Google Cloud STT, IBM Watson, AWS, Azure and ElevenLabs free-tier
  facts carry `search_snippet` evidence and must be re-verified against the
  official pages before routing on the strength of their numbers.
* Emergency chunking is not implemented. Whole-file transcription only.

## Sources

* Gemini Transcribe: https://ai.google.dev/gemini-api/docs/transcribe
* Gemini API pricing: https://ai.google.dev/gemini-api/docs/pricing
* Groq speech-to-text and rate limits: https://console.groq.com/docs/speech-to-text
* Deepgram pre-recorded audio: https://developers.deepgram.com/docs/pre-recorded-audio
* Speechmatics pricing: https://www.speechmatics.com/pricing
* AssemblyAI pricing: https://www.assemblyai.com/pricing
* Soniox free-credit change: official blog, 2025-10-27
