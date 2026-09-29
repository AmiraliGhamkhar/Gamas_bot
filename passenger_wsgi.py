"""Optional cPanel "Setup Python App" (Passenger) entry point.

The bot is a long-running Telegram client, not a website, so this WSGI app does
only two things:

* every request makes sure the bot process is running (start-up trigger), and
* it answers a tiny JSON status document that an uptime monitor can poll.

It never exposes configuration, user data or logs.  Point an external monitor
(for example UptimeRobot) at the app URL every 5 minutes as a second watchdog
next to the cron job described in docs/DEPLOY_CPANEL.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gamas_bot.launcher import MISCONFIGURED, RUNNING, ensure_running  # noqa: E402


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/") or "/"
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if path not in {"/", "/health"} or method not in {"GET", "HEAD"}:
        body = b'{"error":"not found"}'
        start_response(
            "404 Not Found",
            [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
        )
        return [b"" if method == "HEAD" else body]

    try:
        state = ensure_running()
    except Exception:  # never let a launcher failure become a 500 stack trace
        state = MISCONFIGURED
    if state == RUNNING:
        status, label = "200 OK", "running"
    elif state == MISCONFIGURED:
        status, label = "503 Service Unavailable", "error"
    else:
        status, label = "503 Service Unavailable", "starting"
    body = json.dumps({"service": "gamas-bot", "status": label}).encode()
    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [b"" if method == "HEAD" else body]
