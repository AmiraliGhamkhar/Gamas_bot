# Deploy on cPanel

This is a background Telegram client, not a website. Your host must allow Git,
Terminal/SSH, Python **3.11–3.13**, Cron Jobs, background processes and outbound
TCP 443 to Telegram/providers. Confirm those requirements with your host first.

## 1. Install privately

Run in **cPanel Terminal**, outside `public_html` and every other web root:

```bash
cd "$HOME"
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
```

Create `.env` only once. Fill [required settings](CONFIGURATION.md). Use your host's
Python command if named differently. With **Setup Python App**, activate the
virtualenv cPanel shows instead of creating another one.

## 2. Validate

```bash
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

Fix every `FAIL`; review every `WARN`. Preflight writes temporary permission/lock
probes and tests network connections. `--check` is offline and does not migrate data.
Send `/start` and short audio, then **Ctrl+C** and wait for shutdown before enabling cron.

Start with shipped limits: 3 active jobs, 8 queued, 1 media worker, 128 MiB media
uploads. These are ceilings, not RAM guarantees. See [Performance](PERFORMANCE.md).

## 3. Add the watchdog

In **cPanel → Cron Jobs**, choose once per minute. Replace `USER` and paths with
your account values. Put only this in the **Command** field:

```bash
/home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

The equivalent full crontab entry is:

```text
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

Setup Python App users must substitute its interpreter path. The watchdog prevents
second instances and throttles failed starts. Inspect status and logs:

```bash
python scripts/ensure_running.py status
tail -n 80 data/logs/bot.log
tail -n 80 data/logs/launcher.err
```

## 4. Optional Passenger

Use the private project as application root, `passenger_wsgi.py` as startup file,
and `application` as entry point. `GET /` or `/health` starts/checks the process and
returns `running`, `starting` or `error` JSON. This is not an end-to-end health test.
Keep cron enabled; Passenger needs requests to trigger the launcher.

## 5. Update safely

1. Pause cron **and Passenger/uptime-monitor triggers**.
2. Stop and confirm shutdown; do not proceed after a stop failure.
3. [Back up configuration and data](OPERATIONS.md#backups-and-restore).
4. Update and validate, stopping on any error:

```bash
cd "$HOME/gamas_bot"
. .venv/bin/activate
python scripts/ensure_running.py stop
python scripts/ensure_running.py status
# Continue only after status reports stopped and the backup is complete:
git pull --ff-only && python -m pip install -r requirements.txt && python -m gamas_bot --check
```

Only after the update/check succeeds, start with
`python scripts/ensure_running.py --force`. Check status/logs, then restore triggers.
Migrations run automatically on startup; no `manage.py` is used. Never run older
code against a migrated database.

If PyAV/lxml wheels fail on old Linux, request CloudLinux/AlmaLinux 8+ and supported
Python. Cron cannot bypass CPU/RAM/process limits; change hosting if needed.

## 6. Apply the tested note-provider profile (optional)

Use this on a cPanel deployment that wants the 2026-11-07 Gemini → OpenRouter
profile. It keeps the existing cron watchdog / launcher architecture: there is
no second process manager. Pause cron and Passenger triggers before maintenance,
as in step 5, and restore them afterwards. Never put credentials in Git, logs or
support tickets; do not replace an existing `.env` wholesale.

1. **Configure the required API credentials securely.** In `.env`, set
   `GEMINI_API_KEY` and `OPENROUTER_API_KEY` with the real values and keep
   `chmod 600 .env`. Leave `NOTE_API_KEY` and `NOTE_BASE_URL` unset/blank unless
   this deployment deliberately uses the legacy single slot: a configured legacy
   base URL appends an extra `openai_compatible` route. Never change Telegram
   tokens/IDs or STT credentials as part of this rollout.
2. **Apply the recommended environment profile** (deployment-specific values,
   not shipped defaults) by adding or editing these lines only:

   ```dotenv
   NOTE_PROVIDERS=gemini,openrouter
   NOTE_STRUCTURED_OUTPUT=0
   NOTE_REASONING_EFFORT=none
   NOTE_MAX_OUTPUT_TOKENS=32768
   NOTE_GEMINI_THINKING_BUDGET=-1
   GEMINI_MODEL=gemini-3.5-flash-lite
   OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free
   ```

   NaraRouter stays installed and configurable but is deliberately absent:
   `laguna-s-2.1` failed real generation with HTTP 502. Keep a copy of the
   previous `.env` for rollback.
3. **Run the configuration inspection** (offline; no provider quota):

   ```bash
   python -m gamas_bot --check
   python -m scripts.probe_note_providers --show-config
   python -m scripts.cpanel_preflight
   ```

   `--check` and `--show-config` print the effective provider order and the
   non-secret settings; fix `FAIL` and review `WARN` (a 32,768-token budget can
   produce a conservative timeout advisory at the shipped 900 s deadline).
4. **Run provider metadata checks** (network, still no generation quota):

   ```bash
   python -m scripts.probe_note_providers
   ```

   Metadata PASS proves authentication and model entitlement only. It does
   **not** prove generation works — Nara's test is the counterexample.
5. **Run controlled real-generation probes** with a short, non-sensitive
   transcript. This consumes provider quota; do not automate it in CI:

   ```bash
   python -m scripts.probe_note_providers --generate /home/USER/short_lecture.txt
   ```

   Expect one real request per route plus bounded retries, and check the
   reported title, section count and elapsed time. If a route fails, remove it
   from `NOTE_PROVIDERS` before starting the bot.
6. **Set the confirmed tariff before restart.** In the application's `.env`
   (never in a production database), set the exact canonical values:

   ```dotenv
   PLAN_3_PRICE_TOMAN=65000
   PLAN_9_PRICE_TOMAN=195000
   PLAN_12_PRICE_TOMAN=260000
   PLAN_24_PRICE_TOMAN=520000
   PLAN_36_PRICE_TOMAN=780000
   PLAN_60_PRICE_TOMAN=1300000
   ```

   This is **100,000 credits = 3 hours = 65,000 Toman**, not 65 Toman.
   Startup only synchronizes the built-in active plan catalogue; it does not
   rewrite custom plans, historical order snapshots, subscriptions, ledger rows,
   lots, holds, refunds, or user balances.
7. **Restart the bot after changes.** Environment values are read only at
   startup, so a running process keeps the previous budget/order:

   ```bash
   python scripts/ensure_running.py stop
   python scripts/ensure_running.py status   # must report stopped
   python scripts/ensure_running.py --force
   python scripts/ensure_running.py status
   ```

   Restore cron/Passenger triggers only after status and logs look healthy.
8. **Confirm the actual runtime provider order** from the restarted process:

   ```bash
   python -m scripts.probe_note_providers --show-config
   grep -n "LLM request sending" data/logs/bot.log | tail -n 20
   ```

   The first field after `provider=` must be `gemini`; `openrouter` appears only
   after an eligible Gemini failure. No `nara` or `openai_compatible` route
   should appear with this profile.
9. **Inspect logs for real request token limits and provider usage:**

   ```bash
   grep -n "LLM request sending" data/logs/bot.log | tail -n 20
   grep -n "note provider usage" data/logs/bot.log | tail -n 20
   ```

   `requested_output_tokens=` is the effective budget actually sent (expect
   32768); `output_tokens=` on usage lines is the provider's real completion
   usage. A `finish_reason=length`/`MAX_TOKENS` answer is rejected as
   incomplete, not delivered as a partial booklet.

The full variable reference is in [Configuration](CONFIGURATION.md); provider
behaviour and test results are in [Note-generation providers](PROVIDERS.md).
