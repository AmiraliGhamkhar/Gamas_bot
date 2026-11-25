"""cPanel "Setup Python App" (Passenger) WSGI entry point.

This single WSGI application is the authoritative deployment entry point:

* ``/app`` and ``/app/*`` serve the built Telegram Mini App (SPA) from
  ``miniapp/dist``.
* ``/api/*`` (including ``/api/v1/*``) serves the Mini App JSON API.
* ``/`` and ``/health`` answer a tiny bot-watchdog status document that an
  uptime monitor can poll.

The bot itself is a long-running Telegram client started/kept alive by the
launcher; the Mini App and API are served by the same Passenger process. This
module never exposes configuration, user data or logs.

cPanel setup: Application root = repository root, Startup file =
``passenger_wsgi.py``, Entry point = ``application``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gamas_bot.launcher import FAILED, MISCONFIGURED, RUNNING, ensure_running  # noqa: E402
from gamas_bot.webapp_api import make_wsgi_app  # noqa: E402

_WEBAPP = make_wsgi_app()


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/") or "/"
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if path.startswith("/api/") or path == "/app" or path.startswith("/app/"):
        return _WEBAPP(environ, start_response)
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
    elif state in {MISCONFIGURED, FAILED}:
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
