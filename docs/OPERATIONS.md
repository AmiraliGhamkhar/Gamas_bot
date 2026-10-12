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
.venv/bin/python -m gamas_bot.media_worker check
```

`🩺 وضعیت سرویس‌ها` shows provider health; `📜 گزارش مدیر` shows admin actions
and usage metadata. Provider health/catalog checks use read-only endpoints where
available. A generation test is a separate, billable operation and must never
be presented as a health check.

## Note-provider operations

* **Order is policy.** `NOTE_PROVIDERS` (default `nara,openrouter`) lists the
  providers in the order they are attempted; only a provider with its own
  credential is attempted at all. Changing the order means changing that one
  variable — nothing is reordered at runtime and nothing is remembered in the
  database, so a restart cannot change the priority.
* **Credentials are environment-only.** `NARA_API_KEY`, `OPENROUTER_API_KEY`,
  `GEMINI_API_KEY` and the v2-era `NOTE_API_KEY` live in `.env`/systemd; there is
  no credential vault, key wizard or encrypted store in v3, and a key is never
  written to the database or a user-facing message.
* **Missing keys are visible, not silent.** `python -m gamas_bot --check` lists
  the eligible providers and warns (without failing the process) when note
  generation is not configured — the bot then delivers raw transcripts only.
  The job log names every provider that was skipped for missing credentials and
  every provider that was left behind after a failure.
* **Failures are bounded.** Transient failures get a small retry budget per
  provider, then the next provider is tried; an authentication failure or a
  rejected request moves on immediately; an oversized request is split once and
  retried, never re-sent unchanged. Duplicate provider calls are not made, and a
  slow provider cannot extend a job past its budget: every note request has its
  own 5-minute timeout.
* **Only explicitly configured providers are used.** There is no implicit paid
  fallback: a provider participates when it is listed in `NOTE_PROVIDERS`
  (or is the configured `NOTE_PROVIDER` slot) **and** has its own credential.
  Pointing `NOTE_PROVIDER`/`NOTE_BASE_URL` at a provider host keeps that
  deployment in its place in the chain — a NaraRouter base URL stays the Nara
  leg and is never duplicated.
* **Model free status is not assumed.** The defaults are the identifiers the
  providers publish as free (`agnes-2.5-flash` on Nara's free plan,
  `openrouter/free` on OpenRouter); changing `NARA_MODEL`/`OPENROUTER_MODEL` is
  an explicit decision by the operator, and Gamas never relabels a model as free.
* **Quota/limit diagnostics stay in the log**, and no provider panel exists in
  the bot: see [PROVIDERS.md](PROVIDERS.md#provider-diagnostics).
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
