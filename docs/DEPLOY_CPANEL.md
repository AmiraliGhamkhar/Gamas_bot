# Deploy on cPanel shared hosting

Gamas Bot is a long-running Telegram client, not a website. The account needs Terminal/SSH,
Python 3.11+, Cron Jobs, outbound TCP 443 and permission to run a background Python process.
Everything private must stay **outside `public_html`**.

## 1. Install (cPanel Terminal)

```bash
cd "$HOME"
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv          # use the host's 3.11+ command if named differently
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If you use **Setup Python App**, install the requirements into the virtualenv cPanel shows you.
Passenger is optional; the cron watchdog below is still required.

## 2. Configure

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, numeric `ADMIN_IDS`, and
at least one STT key (`SPEECHMATICS_API_KEY` primary + `DEEPGRAM_API_KEY` overflow/failover recommended).
For a typical cPanel account (1 GB RAM, 100 max processes, 10 MB/s disk I/O), start with the
shipped stage-gated defaults:

```dotenv
MAX_ACTIVE_JOBS=3
MAX_ACTIVE_JOBS_PER_USER=2
MAX_QUEUED_JOBS=8
MAX_CONCURRENT_DOWNLOADS=2
MEDIA_WORKER_CONCURRENCY=1
MAX_CONCURRENT_UPLOADS=2
MAX_FILE_SIZE=134217728
MAX_MEDIA_DURATION_SECONDS=7200
TEMP_MIN_FREE_BYTES=67108864
TELEGRAM_DOWNLOAD_CONNECTIONS=1
SPEECHMATICS_MAX_IN_FLIGHT=2
DEEPGRAM_MAX_IN_FLIGHT=4
LLM_PRIMARY_MAX_IN_FLIGHT=4
LLM_FALLBACK_MAX_IN_FLIGHT=4
NOTE_CONCURRENCY=3
NOTE_HEDGE_AFTER_SECONDS=60
PRESENTATION_MAX_CLIPS=50
PRESENTATION_MAX_TOTAL_DURATION=7200
PRESENTATION_MAX_UNPACKED_BYTES=134217728
PRESENTATION_MAX_TEMP_BYTES=268435456
# Free Office: small, bounded, and independent of wallet billing.
OFFICE_ENABLED=true
OFFICE_MAX_INPUT_BYTES=1000000
OFFICE_MAX_CHARS=100000
OFFICE_MAX_SLIDES=30
OFFICE_SESSION_TTL_MINUTES=30
OFFICE_JOB_CONCURRENCY=2
OFFICE_AI_CONCURRENCY=3
OFFICE_MIN_INTERVAL_SECONDS=10
```

These are conservative application ceilings, not a guarantee that every cPanel plan can hold a
128 MiB upload plus its working copies. Reduce `MAX_FILE_SIZE` and the presentation limits if your
quota is smaller; keep a positive `TEMP_MIN_FREE_BYTES` reserve. There are no worker processes or
external queue services: Telegram, provider HTTP, extraction, and media subprocesses remain subject
to the host's CPU, process, network, disk-I/O, and runtime limits. Free Office creates no worker
process or external service; it uses a bounded asyncio semaphore and private temporary files under
`TEMP_DIR`. It works without note-provider credentials by using deterministic, source-safe fallbacks.
A configured provider may incur operator-side API costs, but Office does not debit users. The document
source limit for note/export actions is 250,000 characters; larger extracted documents remain in the
library and are never silently shortened. PowerPoint narration/audio remains a separate explicit
button and is not started by document upload.

## 3. Test before going live

```bash
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

Fix every `FAIL`; review every `WARN`. Send the bot a test message and a short audio file, then stop
the foreground run with **Ctrl+C**. `--check` is offline; the preflight also tests disk,
permissions, the lock and outbound network.

## 4. Add the cron watchdog

In **cPanel → Cron Jobs**, add this once per minute (replace `USER`):

```text
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

With **Setup Python App**, use its interpreter instead, for example
`/home/USER/virtualenv/gamas_bot/3.11/bin/python`. The watchdog starts the bot only when it is not
running and throttles repeated failed starts. Logs: `data/logs/bot.log` and
`data/logs/launcher.err`.

Optionally point an uptime monitor at `passenger_wsgi.py` if you enabled a Python app; it returns
`running`, `starting` or `error` as JSON and starts the bot when needed.

## 5. Update

```bash
cd "$HOME/gamas_bot"
# Pause the cron job in cPanel so it does not restart the bot mid-update,
# then stop the bot only if the PID recorded in the lock is still alive:
PID="$(cat data/gamas.lock 2>/dev/null)"
ps -p "$PID" >/dev/null 2>&1 && kill "$PID"
. .venv/bin/activate
git pull
python -m pip install -r requirements.txt
python -m gamas_bot --check
```

Re-enable the cron job; it starts the bot within a minute.

Database migrations run at startup. Migration 016 rebuilds only the existing library table to allow
Office message references and preserves item ids, rows and content; still take a SQLite-aware backup
before upgrading. Never run an older version against a migrated database. The cron job restarts the
bot within a minute.

## 6. Back up

Back up `.env`, `data/gamas_v3.db`, `data/gamas.session` and `data/receipts/`. Stop the bot first,
or use a SQLite-aware backup so WAL data is included. Store backups encrypted and outside
`public_html` and Git.

## If it fails

| Symptom | Fix |
| --- | --- |
| `Project location` or receipt directory `FAIL` | Move the project/receipts outside every web root |
| `Telegram connectivity FAIL` | Ask the host to allow outbound TCP 443 |
| PyAV install fails / old OS | Request CloudLinux 8+/AlmaLinux 8+ and a supported Python wheel, or use a VPS |
| Process disappears without a Python error | The host killed it: lower concurrency and limits, or move to a VPS |
| Bot stops after a second copy starts | Run one process per token/session |
| `Permission denied` on data files | `chmod 700 data`, `chmod 600 .env`; check the account owns the files |

The preflight is the final check for your account layout.

## 7. Upgrade to the payment and support release

This release includes `005_payments_and_support.sql` (explicit payment states and support tables),
`006_payment_entitlement_links.sql` (payment-linked credit and uniqueness indexes),
`007_payment_state_guards.sql`, and `008_payment_transition_integrity.sql` (complete forward-only
review transitions and reviewer metadata). They run automatically at startup. Migration 008 replaces
only a trigger; it rewrites no rows. Existing balances, purchases, expiry dates and tickets are not
reset. No new environment variables or dependencies are required.

```bash
cd "$HOME/gamas_bot"
# 1. In cPanel -> Cron Jobs, pause the watchdog so it cannot restart mid-upgrade.
PID="$(cat data/gamas.lock 2>/dev/null)"
if [ -n "$PID" ] && ps -p "$PID" >/dev/null 2>&1; then kill "$PID"; fi

# 2. Back up configuration and the stopped SQLite database outside public_html.
BACKUP_DIR="$HOME/backups/$(date +%F-%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp -p .env "$BACKUP_DIR/.env"
cp -p data/gamas_v3.db "$BACKUP_DIR/gamas_v3.db"
# If sidecars exist after the clean stop, preserve them too.
for FILE in data/gamas_v3.db-wal data/gamas_v3.db-shm data/gamas.session; do
    [ ! -f "$FILE" ] || cp -p "$FILE" "$BACKUP_DIR/"
done
chmod 600 "$BACKUP_DIR/.env" "$BACKUP_DIR"/*

# 3. Update without replacing .env, then run offline checks.
. .venv/bin/activate
git pull
python -m pip install -r requirements.txt
python -m gamas_bot --check
# Optional on a development checkout with dev extras installed (never use production data):
# python -m pytest -q

# 4. Re-enable the cron job. It starts the bot and applies migrations within a minute.
# Check that the log contains startup, not a migration or configuration error.
tail -n 80 data/logs/bot.log
```

Smoke test after the first start (use an admin account and a small test order):

1. `/start` shows the lecture guide and the **🗂 آفیس رایگان** button when `OFFICE_ENABLED=true`,
   alongside Library, Wallet, Leaderboard, Support and Help (admins also see their separate panel).
   No mode button is required before sending a test audio file; verify the transcript arrives before
   the Word booklet and both Telegram documents appear in the library.
2. Open **آفیس رایگان** (Free Office) and paste a short Persian message; build Word, Excel, PowerPoint and PDF in turn.
   Check that the files open, the PDF text can be selected/copied, the Excel text remains literal,
   each Telegram file is in the user's library, and wallet/free-use balances are unchanged. Repeat
   with a `.csv` containing a quoted comma and a formula-looking value. Cancel an unexported draft
   and verify it is not reused; leave a draft idle and confirm it expires after the configured TTL.
3. Upload a text PDF, DOCX and PPTX. Confirm the original remains downloadable, page/section/slide
   locators survive into note outputs, full and compact notes differ, both are available as Word and
   PDF, and return-to-library works. A PPTX must not start audio/STT until the explicit audio button
   is pressed. A scanned PDF must be refused with no OCR claim. Repeat Office/document-note actions
   with no note provider configured; deterministic fallback should still produce safe files.
4. Open a library item and verify the bot forwards the saved Telegram message. A definitively deleted
   source should show «این فایل دیگر در دسترس نیست.»; temporary Telegram errors remain retryable.
   A successful retry also clears a stale unavailable flag from earlier versions.
5. Create a ticket from «🎧 پشتیبانی», filter it under «🎧 مدیریت پشتیبانی» (`📂 جدید` + category),
   and reply to it. Confirm both notifications and that the history remains after `/start` and restart.
6. Open «🛒 خرید اشتراک»: confirm plan options come first, then balance and recent payments on the same screen. Create
   an order, send a receipt directly (photo or PNG/JPEG/WebP file), and open it from the wallet or
   `payopen`. The first approval tap must only show confirmation; «✅ تأیید نهایی» activates credit
   once. Reject another test order and verify its reason reaches the user without granting credit.
7. Tap the stale approval button again and confirm the balance and payment-linked ledger credit do
   not change. Check approved/rejected status in the wallet after a restart.
8. Reply from both sides of a ticket, close and reopen it, and confirm the earlier messages remain.
   If Telegram is temporarily unavailable, confirm the stored history is still intact after recovery.

**Rollback.** Pause cron and stop the bot. Set `BACKUP_DIR` to the actual pre-upgrade directory,
move the current database aside (do not delete it), restore the matching backup, and only then start
older code:

```bash
cd "$HOME/gamas_bot"
BACKUP_DIR="$HOME/backups/YYYY-MM-DD-HHMMSS"  # replace with the real backup
RESCUE_DIR="$HOME/backups/rescue-$(date +%F-%H%M%S)"
mkdir -p "$RESCUE_DIR"
for FILE in data/gamas_v3.db data/gamas_v3.db-wal data/gamas_v3.db-shm; do
    [ ! -f "$FILE" ] || mv "$FILE" "$RESCUE_DIR/"
done
cp -p "$BACKUP_DIR/gamas_v3.db" data/gamas_v3.db
for NAME in gamas_v3.db-wal gamas_v3.db-shm; do
    [ ! -f "$BACKUP_DIR/$NAME" ] || cp -p "$BACKUP_DIR/$NAME" "data/$NAME"
done
git checkout <previous-commit>
# Re-enable cron after python -m gamas_bot --check succeeds with the restored code.
```

Do not run older code against the migrated database without restoring its matching backup. Keep both
backup directories until acceptance tests and normal billing have been verified.
