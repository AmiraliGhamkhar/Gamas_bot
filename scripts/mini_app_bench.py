"""Repeatable local WSGI/SQLite Mini App microbenchmark (synthetic data only).

Run from the repository root: ``python scripts/mini_app_bench.py --iterations 100``.
This bypasses HTTP, Passenger, TLS, Telegram's SDK and real devices; it is a
local comparison tool, not a production latency promise.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import logging
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from gamas_bot.mini_app import MiniAppData, create_app
from gamas_bot.mini_app_store import MiniAppStore

BOT_TOKEN = "benchmark-only-synthetic-token"
USER_ID = 123456789
NOW = int(time.time())


class CountingStore(MiniAppStore):
    connection_opens = 0

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        type(self).connection_opens += 1
        return super()._connect(write=write)


def _init_data() -> str:
    fields = {
        "auth_date": str(NOW),
        "user": json.dumps({"id": USER_ID, "first_name": "Benchmark"}),
    }
    check = "\n".join(f"{name}={fields[name]}" for name in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": signature})


def _seed(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE users (
            telegram_id INTEGER PRIMARY KEY, first_name TEXT, quiz_points INTEGER,
            banned INTEGER NOT NULL DEFAULT 0, is_unlimited INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE user_balances (
            user_id INTEGER PRIMARY KEY, available_seconds INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE library_items (item_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL);
        CREATE TABLE submissions (submission_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL);
        CREATE TABLE submission_artifacts (
            artifact_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, available INTEGER NOT NULL
        );
        INSERT INTO users VALUES (123456789, 'Benchmark', 150, 0, 0);
        INSERT INTO user_balances VALUES (123456789, 86400);
        INSERT INTO library_items(user_id) VALUES (123456789), (123456789), (123456789);
        INSERT INTO submissions(user_id) VALUES (123456789), (123456789);
        INSERT INTO submission_artifacts(user_id, available) VALUES (123456789, 1);
        """
    )
    connection.close()


def _call(app, path: str, *, method: str = "GET", body: bytes = b"", headers=None):
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": method,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        **(headers or {}),
    }
    result = b"".join(app(environ, start_response))
    return captured, result


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.999999)))
    return ordered[index]


def _measure(app, path: str, count: int, *, body: bytes = b"") -> tuple[list[float], int, str]:
    durations: list[float] = []
    byte_count = 0
    status = ""
    for _ in range(count):
        started = time.perf_counter()
        response, payload = _call(app, path, method="POST" if body else "GET", body=body)
        durations.append((time.perf_counter() - started) * 1000)
        byte_count = len(payload)
        status = response["status"].split(" ", 1)[0]
    return durations, byte_count, status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if not 10 <= args.iterations <= 10000:
        parser.error("--iterations must be between 10 and 10000")
    logging.disable(logging.CRITICAL)

    with tempfile.TemporaryDirectory(prefix="gamas-miniapp-bench-") as directory:
        db_path = Path(directory) / "synthetic.sqlite"
        _seed(db_path)
        app = create_app(
            data=lambda: MiniAppData(
                leaderboard=lambda _limit: [],
                rank=lambda _user_id: {"rank": 1, "points": 150},
                booklets=lambda _user_id, _limit: [],
                booklet=lambda _user_id, _item_id: None,
                save_booklet=lambda _user_id, _item_id, _text: False,
                profile=CountingStore(db_path).profile,
            ),
            bot_token=BOT_TOKEN,
            now=lambda: NOW,
            static_dir=str(Path(__file__).resolve().parents[1] / "mini_app"),
        )
        body = json.dumps({"initData": _init_data()}).encode()
        # Warm file reads / Python import paths but exclude warmup from timings.
        for path in ("/static/app.css", "/static/app.js"):
            _call(app, path)
        _call(app, "/api/home", method="POST", body=body)

        for path in ("/", "/static/app.css", "/static/app.js"):
            timings, byte_count, status = _measure(app, path, args.iterations)
            print(
                f"{path}: status={status} bytes={byte_count} n={args.iterations} "
                f"p50_ms={statistics.median(timings):.3f} "
                f"p95_ms={_percentile(timings, 0.95):.3f}"
            )

        before = CountingStore.connection_opens
        timings, byte_count, status = _measure(app, "/api/home", args.iterations, body=body)
        connections = CountingStore.connection_opens - before
        print(
            f"/api/home (signed synthetic user): status={status} bytes={byte_count} "
            f"n={args.iterations} p50_ms={statistics.median(timings):.3f} "
            f"p95_ms={_percentile(timings, 0.95):.3f} "
            f"sqlite_connections_per_request={connections / args.iterations:.1f}"
        )
        print("scope=local in-process WSGI; synthetic DB; no network/TLS/Passenger/device/SDK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
