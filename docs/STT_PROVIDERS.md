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

1. The route is `STT_DEFAULT_ROUTE` if set. Otherwise it is `STT_PRIMARY`
   followed by `speechmatics`, `deepgram`, `openai_compatible` in that order,
   which matches the pre-platform behaviour.
2. Every candidate is evaluated and gets either `eligible` or a list of stable
   denial reasons: `trial_not_allowed`, `paid_not_allowed`, `not_configured`,
   `admin_disabled`, `billing_attested_paid`, `persian_unsupported`,
   `language_unsupported`, `file_too_large`, `duration_too_long`,
   `feature_unsupported:*`, `provider_disabled`, `unknown_provider`.
3. Eligible candidates run in route order. Paid-tier candidates always run after
   every free, trial and legacy candidate.
4. `fa` is a hard capability requirement. A provider without `persian_batch` is
   never given Persian audio.
5. `auto` and `multi` are sent only where the provider documents them. Otherwise
   the provider's documented omission or language identification is used, or the
   candidate is refused with `language_unsupported`.

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

* Keys are stored as Fernet ciphertext only. Keys travel in request headers,
  never in URLs. Gemini and Google Cloud STT both send the key as
  `x-goog-api-key`, per Google's guidance to avoid the `key=` query parameter. This
  was changed in this increment and has not been exercised against the live APIs.
* Events and usage rows carry codes, provider names, sizes, timings and HTTP
  status only. Transcripts, audio and raw provider responses are never logged.
* A free tier may let the provider use the content to improve its products. For
  Gemini, the official pricing page says so for the free tier and says no for the
  paid tier. **Do not send confidential or medical audio to a free tier.** Gamas
  makes no "HIPAA-safe", "GDPR-safe" or "no-training" claim for any provider
  without official support.

## Operations

* There is no Telegram admin screen for STT provider settings yet. `enabled` and
  `billing_state` are set through the database helper
  `Database.stt_provider_settings_upsert`, and budgets through
  `Database.stt_quota_set_budget`. Both are audited with the admin ID.
* `stt_provider_events` and `stt_usage_records` hold the audit trail, with the
  events listed in `gamas_bot/stt_platform/events.py`.
* Per-provider concurrency defaults to the provider profile (1 or 2). A third
  concurrent job for one provider waits in a queue. It does not fail.

## Not done

* Full Telegram admin wizard. The STT route editor and budget editor are not built.
* Real-provider production validation. There are no keys in CI, and no live call
  was made in this increment. Native adapters are covered by offline fakes.
* Real-fixture benchmarks. The benchmark script is upgraded (route plan,
  evidence column, route pinning with policy), but no measured WER or latency
  has been recorded, so no provider is ranked.
* AWS Transcribe, Azure Speech, Soniox and ElevenLabs remain scaffolds, disabled.
* The `x-goog-api-key` header for Gemini and Google Cloud STT, and the Gemini
  `interactions` path, have not been exercised against the live APIs.
* Speechmatics, AssemblyAI, Gladia, Google, IBM, Azure, AWS, ElevenLabs and
  Soniox free-tier facts beyond the evidence column are not re-verified here.
* Emergency chunking is not implemented. Whole-file transcription only.

## Sources

* Gemini Transcribe: https://ai.google.dev/gemini-api/docs/transcribe
* Gemini API pricing: https://ai.google.dev/gemini-api/docs/pricing
* Groq speech-to-text and rate limits: https://console.groq.com/docs/speech-to-text
* Deepgram pre-recorded audio: https://developers.deepgram.com/docs/pre-recorded-audio
* Speechmatics pricing: https://www.speechmatics.com/pricing
* AssemblyAI pricing: https://www.assemblyai.com/pricing
* Soniox free-credit change: official blog, 2025-10-27
