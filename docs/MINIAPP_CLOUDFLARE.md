# Mini App on Cloudflare (Tunnel)

Cloudflare cannot run this app itself: it is a WSGI application that reads the
bot's SQLite database from disk. Cloudflare is the **public HTTPS edge**; the app
keeps running on the machine that holds `data/gamas_v3.db` (the bot's host), and a
**Cloudflare Tunnel** publishes it without opening a port.

Use this route on a Windows PC or a VPS that already runs the bot. On shared
cPanel hosting, use [Passenger](MINIAPP_CPANEL.md) instead — you cannot run an
outbound `cloudflared` daemon there.

| Piece | File |
| --- | --- |
| WSGI callable | `scripts/mini_app_wsgi.py` → `application` |
| Static bundle | `mini_app/` (default `MINI_APP_STATIC_DIR`) |
| Database | the same `DB_PATH` the bot uses |

## 1. Start the app locally first

The bot must have started once (migrations 027/028), and the port must stay on
loopback so only the tunnel can reach it.

**Windows PowerShell** — `waitress` is the WSGI server that works on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install waitress
.\.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:8080 scripts.mini_app_wsgi:application
```

**Linux VPS** — `gunicorn`:

```bash
. .venv/bin/activate
pip install gunicorn
gunicorn --workers 2 --threads 4 --bind 127.0.0.1:8080 scripts.mini_app_wsgi:application
```

Check it before exposing anything:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

```bash
curl -s http://127.0.0.1:8080/health
# {"service": "gamas-mini-app", "ok": true, "database": true}
```

`"database": false` means the app cannot see `DB_PATH`; fix that before continuing.
Run both commands from the repository root so `scripts.mini_app_wsgi` is importable.

## 2. Create the tunnel

Install `cloudflared` (`winget install --id Cloudflare.cloudflared` on Windows,
`cloudflared` package or the official `.deb` on Linux), then:

```bash
cloudflared tunnel login            # opens a browser, pick the zone
cloudflared tunnel create gamas-studio
cloudflared tunnel route dns gamas-studio studio.example.com
```

`tunnel create` prints a tunnel ID and writes
`~/.cloudflared/<ID>.json` (`%USERPROFILE%\.cloudflared\<ID>.json` on Windows).
Create `config.yml` next to it:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: C:\Users\USER\.cloudflared\<TUNNEL_ID>.json   # Linux: /home/USER/.cloudflared/<ID>.json
ingress:
  - hostname: studio.example.com
    service: http://127.0.0.1:8080
  - service: http_status:404
```

Run it:

```bash
cloudflared tunnel run gamas-studio
```

Then install it as a service so it survives reboots:

```powershell
cloudflared service install        # elevated PowerShell; needs config.yml in place
```

```bash
sudo cloudflared service install   # uses /etc/cloudflared/config.yml
sudo systemctl enable --now cloudflared
```

Keep the WSGI server running the same way: as a Task Scheduler task on Windows
(see [Windows](WINDOWS.md#5-keep-it-running-after-a-reboot-task-scheduler)) or a
small systemd unit on Linux (`ExecStart=…/gunicorn …`, `Restart=always`).

## 3. Cloudflare dashboard settings

| Setting | Value | Why |
| --- | --- | --- |
| DNS record | proxied (orange cloud), created by `route dns` | traffic must pass the tunnel |
| SSL/TLS mode | **Full** | the origin is plain HTTP on loopback; Flexible/Off breaks TLS behaviour |
| Always Use HTTPS | On | Telegram needs HTTPS |
| Rocket Loader | Off | it rewrites scripts and can break the bundle |
| Bot Fight Mode / Under Attack Mode | Off for this hostname (or a skip rule) | the Telegram WebView cannot solve a JS challenge → blank app |
| `/api/*` cache | bypass (optional) | POSTs are never cached, but this removes doubt |
| Websockets | not required | the app is request/response only |
| Sticky sessions | not required | each request opens its own short SQLite connection |

The origin port must **not** be reachable from the internet; `127.0.0.1:8080` plus
the tunnel is the whole exposure. Cloudflare's 100 MB body limit is far above the
app's 1 MB cap.

## 4. Connect it to the bot

In the bot's `.env`, set the public hostname and restart the bot:

```dotenv
MINI_APP_URL=https://studio.example.com
MINI_APP_AUTH_MAX_AGE_SECONDS=300
```

No path, no trailing slash, `https://` only. Optionally register the same URL as
the bot menu button in [@BotFather](https://t.me/BotFather) → *Bot Settings* →
*Menu Button*.

## 5. Verify

```bash
curl -s https://studio.example.com/health
curl -s -o /dev/null -w "%{http_code}\n" https://studio.example.com/api/home   # 405 for GET is correct
```

Open the app from a Telegram chat: the leaderboard loads, a booklet opens, saving
appends a revision. Outside Telegram the page shows a "signature" error — that is
correct, `initData` only exists inside the WebView.

## 6. Security notes

- TLS terminates at Cloudflare; the tunnel is authenticated by its credentials
  file. Treat that file like a private key.
- Every API request still verifies Telegram's `initData` HMAC with the bot token,
  so a leaked URL grants nothing.
- Keep `TELEGRAM_BOT_TOKEN`, the tunnel credentials and `.env` off shared folders.
- Logs on both sides are metadata only; the app never logs `initData` or text.
- If the origin machine's clock drifts, signed data looks expired — enable time
  sync (Windows: *Settings → Time & language → Sync now*).

## 7. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Cloudflare `1033` (Argo Tunnel error) | tunnel not running or not registered: `cloudflared tunnel list`, restart the service |
| `502 Bad Gateway` | origin down or bound to another port; check the WSGI server and `config.yml` ingress |
| `522` | Cloudflare cannot reach the tunnel at all — service stopped or firewall blocks outbound 443 |
| `403` / challenge page in the WebView | Bot Fight Mode / Under Attack Mode / a WAF rule; exclude the hostname |
| App loads, API `401` | opened outside Telegram, expired `auth_date`, or the wrong bot token in `.env` |
| Blank page, `404 /static/app.css` | hostname routed to a path instead of the origin root |
| `503` when saving | the bot never ran on this database (migrations 027/028) |
| `database: false` in `/health` | app started from another directory → wrong `DB_PATH`/`.env` |

Fallback server for a one-off test (not for production):
`python -m wsgiref.simple_server` is not needed — use waitress/gunicorn above.

More: [Configuration](CONFIGURATION.md) · [Troubleshooting](TROUBLESHOOTING.md).
