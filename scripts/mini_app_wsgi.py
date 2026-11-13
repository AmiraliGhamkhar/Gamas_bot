"""WSGI entry point for the Mini App (cPanel Passenger / Cloudflare proxy).

Two deployment routes are supported, and both use this file:

* **cPanel Passenger** — add this file as the application's WSGI entry point
  (``mini_app_wsgi.py`` / ``application``) in "Setup Python App". The bot keeps
  running from cron (``scripts/ensure_running.py``); this worker only serves the
  Mini App.
* **Cloudflare** — run the same app behind a WSGI server (gunicorn, uWSGI, or
  ``wsgiref`` for a smoke test) on a port Cloudflare proxies to. Nothing here
  needs websockets, so a proxied HTTPS hostname is enough.

Configuration comes from the same ``.env`` the bot uses; only ``TELEGRAM_BOT_TOKEN``
and ``DB_PATH`` are required. ``MINI_APP_STATIC_DIR`` points at the built bundle
(``mini_app/`` by default) and ``MINI_APP_AUTH_MAX_AGE_SECONDS`` tunes the
``initData`` freshness window.

The bot token is read per request, not at import time, because Passenger imports
the module before it has loaded ``.env``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gamas_bot.config import from_env  # noqa: E402
from gamas_bot.mini_app import MiniAppData, create_app  # noqa: E402
from gamas_bot.mini_app_store import MiniAppStore  # noqa: E402

logger = logging.getLogger("gamas_bot.mini_app")

_cached_settings = None


def _settings():
    """Load the environment once, but tolerate a slow or absent ``.env``."""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = from_env()
    return _cached_settings


def _data():
    """Resolve the full Gamas 3.0 data facade per request.

    ``MiniAppStore`` duck-types the original four-call surface, but the Studio
    also needs Home, weekly/monthly boards, revision history, exports, speaker
    names, the submissions overview and the structured editor. Building the
    facade explicitly keeps every call bound to the one connection-per-request
    store and makes a missing method an import-time error rather than a 500 in
    production.
    """
    store = MiniAppStore(_settings().db_path)
    return MiniAppData(
        leaderboard=store.leaderboard,
        rank=store.rank,
        booklets=store.booklets,
        booklet=store.booklet,
        save_booklet=store.save_booklet,
        profile=store.profile,
        leaderboard_window=store.leaderboard_window,
        rank_window=store.rank_window,
        revisions=store.revisions,
        restore_revision=store.restore_revision,
        speaker_turns=store.speaker_turns,
        rename_speaker=store.rename_speaker,
        submissions=store.submissions,
        save_structured=store.save_structured,
        export_bytes=store.export_bytes,
    )


def _token() -> str:
    return _settings().bot_token


def _static_dir() -> str:
    """Where the built bundle lives (``MINI_APP_STATIC_DIR``, default ``./mini_app``)."""
    configured = os.environ.get("MINI_APP_STATIC_DIR", "").strip()
    return str(Path(configured).resolve()) if configured else str(ROOT / "mini_app")


def _health():
    """Liveness only: report whether the configured database path exists."""
    settings = _settings()
    return {
        "service": "gamas-mini-app",
        "ok": True,
        "database": Path(settings.db_path).is_file(),
    }


def _readiness():
    """Read-only SQLite and schema probe; never migrates or mutates the DB."""
    settings = _settings()
    path = Path(settings.db_path)
    if not path.is_file():
        return {"service": "gamas-mini-app", "ok": False, "database": False, "schema": False}
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("SELECT 1").fetchone()
            rows = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('users', 'schema_migrations')"
                ).fetchall()
            }
        finally:
            connection.close()
    except sqlite3.Error:
        logger.warning("mini app readiness database probe failed")
        return {"service": "gamas-mini-app", "ok": False, "database": False, "schema": False}
    schema_ok = rows == {"users", "schema_migrations"}
    return {
        "service": "gamas-mini-app",
        "ok": schema_ok,
        "database": True,
        "schema": schema_ok,
    }


def _max_age() -> int:
    raw = os.environ.get("MINI_APP_AUTH_MAX_AGE_SECONDS", "300").strip()
    try:
        value = int(raw)
    except ValueError:
        return 300
    return max(30, min(3600, value))


application = create_app(
    data=_data,
    bot_token=_token,
    max_age_seconds=_max_age(),
    health=_health,
    readiness=_readiness,
    static_dir=_static_dir(),
)
