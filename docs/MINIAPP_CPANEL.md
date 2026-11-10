# Mini App on cPanel (Setup Python App / Passenger)

Gamas Studio is a plain WSGI app: no build step, no Node, no websockets, no
event loop. On cPanel it runs as a Passenger application next to the bot. This
guide covers only the cPanel route; for Cloudflare use
[Mini App on Cloudflare](MINIAPP_CLOUDFLARE.md).

| Piece | File |
| --- | --- |
| WSGI entry point | `scripts/mini_app_wsgi.py` → callable `application` |
| HTTP layer, `initData` verification | `gamas_bot/mini_app.py` |
| SQLite access (one connection per request) | `gamas_bot/mini_app_store.py` |
| Front end (no build) | `mini_app/index.html`, `app.css`, `app.js` |

## Before you start

1. The bot is installed and **has started at least once** — the app never
   migrates the schema, and needs migrations 027/028 to exist. Until then saves
   answer `503`.
2. You have a **subdomain** (for example `studio.example.com`) with AutoSSL. The
   bundle and the API use absolute paths (`/static/…`, `/api/…`), so the app must
   be served at the **root of its own host**, not under `/miniapp`.
3. The app runs as the same cPanel user as the bot, so it can open the same
   `data/gamas_v3.db`. Never point it at a copy while the bot writes to another.

## 1. Create the subdomain

*cPanel → Domains → Create A New Domain*. Use `studio.example.com` and keep the
default document root. The document root is irrelevant: this WSGI app serves its
own files, and the repository stays outside `public_html`.

Wait for AutoSSL to issue the certificate (`https://studio.example.com` must not
show a warning). Telegram refuses plain HTTP and invalid certificates.

## 2. Create the Python application

*cPanel → Setup Python App → Create Application*:

| Field | Value |
| --- | --- |
| Python version | 3.11 or 3.12 (matching the bot) |
| Application root | `gamas_bot` (the repository directory) |
| Application URL | `studio.example.com` (root, no subpath) |
| Application startup file | `scripts/mini_app_wsgi.py` |
| Entry point | `application` |

If the startup-file field rejects the subdirectory, add a two-line file at the
repository root:

```python
# mini_app_wsgi.py — root-level Passenger entry point
from scripts.mini_app_wsgi import application  # noqa: F401
```

and use `mini_app_wsgi.py` in the field instead.

Install the runtime packages into the application's virtualenv (the command is
shown at the top of the Setup Python App page):

```bash
source /home/USER/virtualenv/gamas_bot/3.11/bin/activate && cd /home/USER/gamas_bot
pip install -r requirements.txt
```

The app reads the same `.env` as the bot (`TELEGRAM_BOT_TOKEN`, `DB_PATH`), and
`MINI_APP_STATIC_DIR` defaults to `mini_app/` inside the repository. Restart the
app after installing packages.

## 3. Verify the app

```bash
curl -s https://studio.example.com/health
# {"service": "gamas-mini-app", "ok": true, "database": true}
```

`"database": false` means `DB_PATH` is wrong or unreadable for this user. Opening
the URL in a desktop browser shows the shell and a Telegram "open from the bot"
notice — `initData` exists only inside Telegram, so that is expected.

## 4. Connect it to the bot

Edit the bot's `.env` (not the app's environment) and restart the bot:

```dotenv
MINI_APP_URL=https://studio.example.com
MINI_APP_AUTH_MAX_AGE_SECONDS=300
```

The URL must be `https://`, must have **no trailing slash and no path**. With
`MINI_APP_URL` empty the bot draws no app button at all. The bot validates
nothing here, so a typo shows up as a broken button, not a failed `--check`.

Optionally set a Telegram menu button: [@BotFather](https://t.me/BotFather) →
`/mybots` → your bot → *Bot Settings* → *Menu Button* → *Edit menu button URL* →
`https://studio.example.com`.

Then in the bot: the final message of a finished job offers **✏️ ویرایش در
استودیو**, and the learning screen links to the leaderboard.

## 5. How a request is trusted

Every API call carries Telegram's `initData`. The server recomputes
`HMAC-SHA256(HMAC-SHA256("WebAppData", bot_token), data_check_string)`, compares
it in constant time, and requires `auth_date` to be within
`MINI_APP_AUTH_MAX_AGE_SECONDS` (300 s default) and not in the future. Only the
signed `user.id` identifies the caller; every SQL statement is scoped to it. No
cookie, session, login page or CORS header is involved.

Request bodies are capped at 1 MB, edited text at 200 000 characters, structured
documents at 400 000 serialized characters / 200 sections.

## 6. Operations

| Task | Action |
| --- | --- |
| Reload after code or `.env` change | cPanel → Setup Python App → **Restart**, or `mkdir -p /home/USER/gamas_bot/tmp && touch /home/USER/gamas_bot/tmp/restart.txt` |
| Watch for downtime | Point an uptime monitor at `/health` (5-minute interval) |
| Update | Update the bot as in [cPanel](CPANEL.md#6-update-safely), then restart the app |
| Stop serving | cPanel → Setup Python App → **Stop** (the bot keeps running) |

Passenger starts the worker on the first request, so the app can be idle between
visits without affecting the bot. A crashed worker is restarted on the next
request.

## 7. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `500` or Passenger error page | Missing packages in the app virtualenv; check the app's error log |
| `/health` 404 | Startup file or entry point wrong, or app URL points at a subpath |
| Blank page, console `404 /static/app.css` | App served under a subpath — recreate it on the subdomain root |
| `401` in the app | Opened outside Telegram, expired `auth_date`, or a different bot token in `.env` |
| `503` when saving | Bot never ran on this database (migration 027/028 missing) |
| Button in Telegram opens nothing | `MINI_APP_URL` not HTTPS or has a path; restart the bot after editing `.env` |
| App slow / times out | Shared-host Passenger limits; the app is light, but very large booklets still take seconds to render |

More: [Configuration](CONFIGURATION.md) · [Troubleshooting](TROUBLESHOOTING.md).
