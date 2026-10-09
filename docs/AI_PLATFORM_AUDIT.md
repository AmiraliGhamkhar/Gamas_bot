# AI Provider Platform — Repository Audit & Architecture Notes

Audit date: 2026-10-08 (pre-upgrade, commit `e8d2adc`).

This document records **what the repository actually did before the AI provider
platform upgrade**, and the architecture the upgrade adds around it. It exists
so reviewers can verify that no working Gamas behaviour was rewritten for
stylistic reasons.

## 1. Pre-upgrade behaviour (verified in source)

### 1.1 How note requests are built
`gamas_bot/structuring.py::_provider_request` dispatches on
`settings.note_api_provider` ∈ {`gemini`, `openai_compatible`, `anthropic`}:

* **Gemini**: native `POST {base}/models/{model}:generateContent`,
  `x-goog-api-key` header, `generationConfig = {temperature: 0.2,
  maxOutputTokens, responseMimeType: "application/json"}`. No `responseSchema`.
* **OpenAI-compatible**: `POST {base}/chat/completions`, `Authorization: Bearer`,
  `messages=[system,user]`, `temperature`, `max_tokens`; `response_format
  {"type":"json_object"}` only when the global `NOTE_API_JSON_MODE=true`.
* **Anthropic**: `POST {base}/messages`, `x-api-key` + `anthropic-version`,
  model-specific temperature (`temperature` and `top_p` never sent together;
  `top_k` never sent).

Authentication headers are reserved (`RESERVED_HEADER_NAMES`); operator-supplied
extra headers can never override a credential.

### 1.2 Credentials, rotation, cooldown, quarantine
`provider_credentials.py`: Fernet-encrypted SQLite rows (`provider_credentials`
table, migration 003), masked `last4`, env-fallback credentials, priority order,
at most 5 candidates per request. HTTP 401/403 quarantines a key, HTTP 429 sets
a cooldown honouring `Retry-After`, 5xx records a failure and rotates. Result
records never store raw provider bodies.

### 1.3 Health checks
`provider_health.py`: manual, cached (300 s), read-only probes (job/model
lists). Probes feed the same rotation state as live traffic. Panel redraws never
hit the network. No generation test existed; the only end-to-end check was the
per-credential "تست" probe, still read-only.

### 1.4 Admin key management
`bot.py` `_show_provider_credentials` lists stored/env keys with state;
enable/disable/delete/reorder/test buttons exist. Adding a key required typing
`provider | label | base_url | model` manually, then sending the secret as a
separate message deleted before persistence (deletion failure ⇒ key dropped).

### 1.5 Audit logging
`admin_audit_log` (migration 003) via `Database._insert_audit`; credential
actions (add/enable/disable/delete/reorder/test) are audited with secret-free
details.

### 1.6 Note QA / repair / compilation acceptance
`qa.py::run_note_qa` + `structuring._repair_is_better` / `compile_is_better`:
a repair or final-compile slice is accepted only when semantic coverage, signal
coverage, missing numbers and compression do not regress. Preserved verbatim.

### 1.7 Configuration validation
`config.py::Settings.from_env`: validates provider names, URLs, numeric fields,
NOTE_API_* legacy variables. `effective_note_api_key`/`effective_note_model`
provide the legacy Gemini fallbacks.

### 1.8 Env vs database credentials
Stored credentials take priority; an env key is appended as a synthetic
`source="environment"` credential (or a keyless `openai_compatible` endpoint
when no stored key exists). `apply_to_settings` builds a request-local Settings
copy; the master config stays immutable.

### 1.9 Telegram admin panel
Persian UI; admin menu rows: stats, users, payments, credits, plans, special
users, health, API keys, audit log, broadcast, ban/unban.

### 1.10 Chunking
`split_transcript(text, max_chars)` is paragraph/sentence-aware and lossless;
budget: `TRANSCRIPT_CHUNK_CHARS = 22000` characters (minus a 200-char prefix
reserve). Character-based only — no token awareness. Presentation documents are
budgeted the same way.

### 1.11 Pipeline call graph
`structure_transcript` / `structure_presentation`:
optional outline call (multi-part only) → one strict-JSON call per chunk →
optional QA-triggered repair (one call per chunk) → optional final compilation
(bounded slices) — every extra call is accepted only via deterministic QA.

## 2. What the upgrade adds

Additives only; the pieces above stay in place unless listed here.

* `gamas_bot/ai/` — the provider platform package:
  * `registry.py` — provider registry: classification (`permanent_free`,
    `free_plan`, `promotional_free`, `trial_only`, `paid_only`,
    `unavailable`, `region_restricted`, `account_unverified`), protocol, base URL, auth style,
    docs/pricing URLs, free-tier policy, data-use policy; URL→provider
    resolution so a legacy `NOTE_API_BASE_URL=https://router.bynara.id/v1`
    deployment is classified as NaraRouter without any config change.
  * `models.py` — model registry: static fallback capability metadata +
    live discovery (`/v1/models`, Gemini `/models`) cached in SQLite with
    TTL; per-model capabilities (JSON schema/strict, reasoning effort,
    context window, max output, free status, free-until, deprecation,
    region restriction, commercial-use).
  * `adapters.py` — one adapter per provider family: request building
    (URL/headers/payload), response parsing, usage extraction, error
    mapping, retry classification, structured-output selection, model
    discovery parsing. Adapters never log or expose secrets; authentication
    is owned by the adapter.
  * `profiles.py` — `RequestPolicy`/`NoteProviderProfile`: per-provider
    chunk budget (tokens), max output, timeout, retries, JSON strategy,
    extra-pass policy (outline/repair/final compile), concurrency hints,
    free-only eligibility.
  * `routing.py` — task-aware routing + provider-level failover on top of
    the existing credential-level rotation; free-only safety; route order
    stored in SQLite (admin-editable), seeded from environment for backward
    compatibility. Current Z.AI pricing lists selected GLM models as Free, but
    Terms §III.06 restrict specified education-related decision-making and
    services requiring educational qualifications or professional review. The
    wording is not a blanket ban on all educational use; Gamas conservatively
    blocks student-note routes pending scope clarification.
  * `tokens.py` — provider-aware token budget estimation (character预算
    fallback kept) and automatic chunk shrinking.
  * `usage.py` — per-request usage ledger, daily aggregates, quota
    snapshots, structured lifecycle events (`note_request_started`, …) with
    mandatory metadata fields and a secrets sanitizer.
* `migrations/006_ai_provider_platform.sql` — forward-only tables:
  `ai_models`, `ai_provider_routes`, `ai_provider_settings`,
  `ai_usage_records`, `ai_usage_daily`, `ai_quota_snapshots`, `ai_events` +
  non-destructive `provider_credentials` column additions.
* `migrations/007_provider_billing_attestations.sql` — additive billing
  attestation owner/timestamp columns and fail-closed reset of all existing
  credentials to `unknown`.
* `gamas_bot/bot.py` — upgraded admin panel: provider/model/routing/usage
  panels, step-by-step key-add wizard, key actions (replace secret, edit
  metadata, explicit free-no-overage/paid billing attestations, delete with
  confirmation), dry-run request view, generation-test vs read-only distinction.
* `scripts/benchmark_notes.py` — provider-profile benchmarking keeping the
  deterministic CI gate untouched.
* Configuration: `AI_FREE_ONLY` (default true), `AI_ALLOW_PAID_FALLBACK`
  (default false), `AI_ROUTING_ENABLED`, `AI_PROVIDER_SYNC_TTL`,
  `AI_DEFAULT_NOTE_ROUTE`, `AI_MAX_PROVIDER_FAILOVERS`,
  `AI_MAX_GENERATION_RETRIES`, `AI_QUOTA_SAFETY_MARGIN`.

## 3. Backward-compatibility contract

* `AI_ROUTING_ENABLED=false` (or no admin-configured route at all) reproduces
  the pre-upgrade single-provider behaviour byte-for-byte: same chunk sizes
  (22 000 chars), same prompts, same payloads, same retries.
* An existing NaraRouter deployment
  (`NOTE_API_PROVIDER=openai_compatible`,
  `NOTE_API_BASE_URL=https://router.bynara.id/v1`,
  `NOTE_API_MODEL=agnes-3-flash`) keeps those settings unchanged and resolves to
  the NaraRouter adapter. After migration 007, an environment key is not
  auto-attested for generation; the same secret must be stored encrypted and
  explicitly attested. `agnes-3-flash` is not claimed free because it is absent
  from the currently published Nara Free-plan list.
* `NOTE_API_JSON_MODE` keeps working as the generic fallback json-object
  switch; capability-aware strategies take precedence only for providers whose
  models advertise the feature.
* All existing QA, repair, merge, DOCX/PPTX, billing, STT, credential
  encryption, health, and deployment components keep their public contracts.
