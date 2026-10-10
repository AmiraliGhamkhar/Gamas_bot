# Operations

## Starting and stopping

* **systemd**: `deploy/gamas-bot.service` runs under a dedicated `bot` user,
  `UMask=0077`, a `ProtectSystem=strict` sandbox, and a restart policy. Install
  it with `sudo cp deploy/gamas-bot.service /etc/systemd/system/` and adjust
  paths for the deployment; [DEPLOY_FA.md](DEPLOY_FA.md) walks through setup.
* **cPanel / Passenger**: `passenger_wsgi.py`, `gamas_bot/launcher.py`, and
  `scripts/ensure_running.py` keep one bot process alive. The instance lock
  prevents a second Telegram polling client.

Shutdown order is deliberate: stop intake, reject queued work, cancel workers,
disconnect Telegram, then close SQLite. A submission is recorded as either
failed or processed; it is not left in `processing` after restart.

## Health and diagnostics

```bash
.venv/bin/python -m scripts.cpanel_preflight
.venv/bin/python -m gamas_bot --check
.venv/bin/python -m scripts.validate_docx --render
```

`🩺 وضعیت سرویس‌ها` shows provider health; `📜 گزارش مدیر` shows admin actions
and usage metadata. Provider health/catalog checks use read-only endpoints where
available. A generation test is a separate, billable operation and must never
be presented as a health check.

## AI provider operations

* `🤖 پلتفرم AI` in the Telegram admin panel manages providers, routes, encrypted
  keys, explicit billing attestations, read-only catalog sync, quota summaries,
  redacted logs, and dry-run requests.
* Defaults are `AI_FREE_ONLY=true` and `AI_ALLOW_PAID_FALLBACK=false`. After
  migration 007, old credentials are `unknown` and cannot generate until an
  admin attests each stored key. Read-only health and catalog checks remain
  available. A `free` attestation is valid only when the provider account cannot
  bill overage (billing disabled or an enforced `$0` hard cap). The `paid`
  attestation alone does not enable paid fallback.
* Existing `NOTE_API_*` values are not deleted or rewritten. The current
  `NOTE_API_PROVIDER=openai_compatible` + Nara base URL/model remains in place;
  to authorize its environment key for generation, add the same secret to the
  encrypted vault and attest it. Do not mark `agnes-3-flash` free: the active
  Nara Free-plan catalog currently lists `agnes-2.5-flash`, not that legacy
  model.
* Provider failover is distinct from credential rotation. A failed key rotates
  inside its provider pool before the router tries the next provider leg.
  Provider-specific retries, RPM/TPM/daily caps, local chunk/output budgets,
  provider quota headers, and live entitlement probes all apply. Each attempt
  projects estimated input plus the full output-token cap and reserves it against
  in-flight requests in the bot process before network I/O. Failed calls count
  against local caps. Completed calls without usage metadata conservatively
  book the configured output ceiling in the quota ledger. A failed local
  quota-ledger read fails closed when that provider has a configured cap. These
  process-local estimates supplement—but do not replace—the provider's
  enforcement or required hard no-overage attestation; reservations do not
  coordinate multiple bot processes or survive a restart.
* The admin UI does **not** provide a manual daily-counter reset: that could
  bypass known provider quotas. Wait for the UTC daily window/provider reset or
  obtain a fresh documented entitlement probe. A provider 429 places the key in
  cooldown and routes to another eligible leg.
* Model sync uses documented endpoints with a TTL (`AI_PROVIDER_SYNC_TTL`,
  default 24 h). Cloudflare Model Search is account-scoped and paginated; Nara
  discovery intersects its account model list with its public Free-plan list.
  Empty, malformed, incomplete, or failed refreshes preserve the previous valid
  catalog. Discovery data alone never proves price, capability, or account
  billing state.
* Structured logs contain provider/model/status/attempt/latency/route position
  and redacted error classes only. Prompts, transcripts, raw outputs, provider
  response bodies, and secrets are not logged or placed in student notes.
  See [SECURITY.md](SECURITY.md) and [AI_PROVIDERS.md](AI_PROVIDERS.md).
* Providers whose free entitlement is a property of the account rather than of
  published documentation (Groq today) stay out of `AI_FREE_ONLY` routes until an
  administrator attests them under **AI → provider → 🧾 تأیید استحقاق حساب**.
  The attestation is per deployment, audited and revocable, and it never
  replaces the per-key billing attestation.
* Cloudflare Workers AI is metered in Neurons, which the API does not return.
  Gamas estimates spend pessimistically and stops before the configured daily
  budget; the estimate is visible under **AI → provider**. Adjust
  `neuron_budget_daily` only against a measured allocation.
* Extra pipeline passes (outline, QA repair, final compilation) each spend
  free-tier quota. Restrictive free providers default them off; override per
  provider under **AI → provider → ✅/❌ طرح‌کلی / تعمیر / تلفیق** when the extra
  quality is worth the quota.
* Offline end-to-end platform validation (no API key required):
  `.venv/bin/python -m scripts.validate_provider_platform`. Run it after any
  change to the registry, profiles, routing or migrations.
* Offline route and profile report (no API calls):
  `.venv/bin/python -m scripts.benchmark_notes --router`.
  Deterministic note/DOCX benchmark (offline):
  `.venv/bin/python -m scripts.benchmark_notes --json`.
  `--live` calls the configured model and may incur provider cost; use only after
  reviewing the billing attestation, route, and account spend cap.

## STT platform operations

* `🎙 پلتفرم STT` in the Telegram admin panel manages STT providers (registry
  cards with free class, limits and privacy fields), STT models (including
  `DEPRECATED` marking with a replacement suggestion), the data-driven route
  (`stt_routes`: order, enable/disable, per-leg model override), quotas
  (admin budget ceilings + observed snapshots with the safety margin), read-only
  health, event logs, the provider test tool and the redacted dry-run request
  viewer. The full reference is [STT_PROVIDERS.md](STT_PROVIDERS.md).
* Defaults are `STT_FREE_ONLY=true`, `STT_ALLOW_TRIAL_PROVIDERS=false`,
  `STT_ALLOW_PAID_FALLBACK=false`. Speechmatics and Deepgram stay in the trial
  allowlist so existing deployments keep working; any other trial-class provider
  needs an explicit opt-in. A `paid` billing attestation blocks free/trial use of
  that provider.
* Route precedence: admin `stt_routes` rows, then `STT_DEFAULT_ROUTE`, then the
  legacy `STT_PRIMARY` chain. Editing the route in the panel needs no restart
  and no code change.
* Quota budgets are admin attestations of the remaining no-overage allocation
  seen in the provider console. The job reserves from them atomically before the
  HTTP request (safety margin applied) and never displays a manufactured
  remaining value: unknown stays Unknown.
* Health checks are READ_ONLY_HEALTH (metadata/model/quota endpoints, no audio).
  The test tool's sample modes are GENERATION_TEST and consume real provider
  quota; run them deliberately. Sample fixtures live in
  `tests/fixtures/stt/samples/` (see `tests/fixtures/stt/README.md`).
* Benchmarks: `python -m scripts.benchmark_stt --plan sample.wav` shows route
  decisions without sending audio; a full run records WER/CER, terminology and
  numeric preservation, quality signals and the weighted score per profile.
  Provider ranking must come from real Persian Gamas fixtures — never from
  marketing claims or English WER.
* Structured STT events (`stt_provider_events`, `stt_usage_records`) carry
  provider/model/status/latency/quota metadata only. Transcript text, audio
  content, prompts and secrets are never logged (spec §54/§73).

## Logs

`LOG_FORMAT=json` emits structured operational metadata. `LOG_FILE` enables
rotation (`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`); otherwise output goes to
stdout/stderr for systemd. Never attach raw request/response bodies, API keys,
transcripts, or student documents to a provider issue report.

## Backups

Back up together, or not at all:

* `DATABASE_PATH` and its WAL/SHM sidecars (or a consistent SQLite backup);
* the Telegram session file;
* `PROVIDER_CREDENTIALS_ENCRYPTION_KEY`—stored API keys cannot be recovered
  without it;
* `RECEIPT_DIR` if receipts must outlive the retention window.

Keep encryption-key backups offline and access-controlled. Do not put secrets in
a Git repository or ordinary log archive.

## Capacity

Concurrency is bounded by `MAX_CONCURRENT_JOBS` active jobs and
`MAX_PENDING_JOBS` waiting ones. Beyond that, uploads are rejected explicitly
instead of building an unbounded queue. Practical throughput is usually limited
by speech and provider latency; document generation is short and runs outside
the event loop.
