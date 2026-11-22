"""SQLite-backed cross-process request/token budget reservations for LLM calls.

The ledger uses BEGIN IMMEDIATE so independent bot processes sharing the
configured SQLite database cannot reserve the same quota concurrently. Token
charges are conservative estimates (input plus the full requested output cap)
when provider usage cannot be reconciled by the public completion API.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class QuotaUnavailable(RuntimeError):
    """A provider has no configured operational budget left for this request."""


@dataclass(frozen=True, slots=True)
class ProviderBudget:
    rpm: int = 0
    tpm: int = 0
    rpd: int = 0
    max_in_flight: int = 1


@dataclass(frozen=True, slots=True)
class Reservation:
    token: str
    provider: str
    db_path: str
    reserved_tokens: int
    attempts: int = 1


def estimate_tokens(text: str, *, chars_per_token: float = 1.5) -> int:
    """Conservative, configurable-ratio estimate for mixed Persian/Latin text."""
    ratio = max(0.5, min(8.0, float(chars_per_token)))
    return max(1, int(len(text or "") / ratio + 0.999))


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        # Concurrent first opens can race while SQLite changes the journal
        # mode. The winner enables WAL; this connection still uses busy_timeout
        # and BEGIN IMMEDIATE for its transactional reservation.
        if "locked" not in str(exc).lower():
            conn.close()
            raise
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("""CREATE TABLE IF NOT EXISTS llm_quota_reservations (
        token TEXT PRIMARY KEY, provider TEXT NOT NULL, started REAL NOT NULL,
        reserved_tokens INTEGER NOT NULL, state TEXT NOT NULL, expires REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS llm_quota_requests (
        token TEXT PRIMARY KEY, provider TEXT NOT NULL, started REAL NOT NULL,
        tokens INTEGER NOT NULL, succeeded INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS llm_quota_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
        happened REAL NOT NULL, succeeded INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS llm_quota_circuit (
        provider TEXT PRIMARY KEY, open_until REAL NOT NULL DEFAULT 0,
        probe_token TEXT NOT NULL DEFAULT ''
    )""")
    return conn


def reserve(
    db_path: str, provider: str, budget: ProviderBudget, tokens: int, attempts: int = 1
) -> Reservation:
    now = time.time()
    token = uuid.uuid4().hex
    tokens = max(1, int(tokens))
    attempts = max(1, min(4, int(attempts)))
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # If a process died after dispatch, account the held estimate rather
        # than refunding tokens that the provider may have consumed.
        conn.execute(
            "UPDATE llm_quota_requests SET tokens=(SELECT reserved_tokens FROM llm_quota_reservations r WHERE r.token=llm_quota_requests.token) "
            "WHERE token IN (SELECT token FROM llm_quota_reservations WHERE expires < ?)",
            (now,),
        )
        conn.execute("DELETE FROM llm_quota_reservations WHERE expires < ?", (now,))
        conn.execute("DELETE FROM llm_quota_requests WHERE started < ?", (now - 86400,))
        conn.execute("DELETE FROM llm_quota_health WHERE happened < ?", (now - 86400,))
        circuit = conn.execute(
            "SELECT open_until, probe_token FROM llm_quota_circuit WHERE provider=?",
            (provider,),
        ).fetchone()
        is_probe = bool(circuit and circuit[0] > 0)
        if circuit and circuit[0] > now:
            raise QuotaUnavailable(f"{provider} circuit breaker open")
        if is_probe and circuit[1]:
            probe_active = conn.execute(
                "SELECT 1 FROM llm_quota_reservations WHERE token=? AND expires >= ?",
                (circuit[1], now),
            ).fetchone()
            if probe_active:
                raise QuotaUnavailable(f"{provider} circuit probe already in flight")
        requests_minute = conn.execute(
            "SELECT COUNT(*) FROM llm_quota_requests WHERE provider=? AND started >= ?",
            (provider, now - 60),
        ).fetchone()[0]
        requests_day = conn.execute(
            "SELECT COUNT(*) FROM llm_quota_requests WHERE provider=? AND started >= ?",
            (provider, now - 86400),
        ).fetchone()[0]
        inflight = conn.execute(
            "SELECT COUNT(*) FROM llm_quota_reservations WHERE provider=? AND expires >= ?",
            (provider, now),
        ).fetchone()[0]
        used_tokens = conn.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM llm_quota_requests WHERE provider=? AND started >= ?",
            (provider, now - 60),
        ).fetchone()[0]
        held_tokens = conn.execute(
            "SELECT COALESCE(SUM(reserved_tokens),0) FROM llm_quota_reservations WHERE provider=? AND expires >= ?",
            (provider, now),
        ).fetchone()[0]
        if budget.rpm and requests_minute + 1 > budget.rpm:
            raise QuotaUnavailable(f"{provider} RPM budget exhausted")
        if budget.rpd and requests_day + 1 > budget.rpd:
            raise QuotaUnavailable(f"{provider} daily request budget exhausted")
        if budget.rpm:
            attempts = min(attempts, budget.rpm - requests_minute)
        if budget.rpd:
            attempts = min(attempts, budget.rpd - requests_day)
        if budget.tpm and used_tokens + held_tokens + tokens > budget.tpm:
            raise QuotaUnavailable(f"{provider} TPM budget exhausted")
        if budget.max_in_flight and inflight >= budget.max_in_flight:
            raise QuotaUnavailable(f"{provider} in-flight budget exhausted")
        conn.execute(
            "INSERT INTO llm_quota_reservations VALUES(?,?,?,?,?,?)",
            (token, provider, now, tokens, "reserved", now + 1800),
        )
        if is_probe:
            conn.execute(
                "INSERT INTO llm_quota_circuit(provider,open_until,probe_token) VALUES(?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET probe_token=excluded.probe_token",
                (provider, circuit[0], token),
            )
        # Count the dispatch conservatively, including a failure after the
        # provider may already have received the request (important for free RPD).
        for attempt_index in range(attempts):
            conn.execute(
                "INSERT INTO llm_quota_requests VALUES(?,?,?,?,?)",
                (token if attempt_index == 0 else f"{token}:{attempt_index}", provider, now, 0, 0),
            )
        conn.commit()
        return Reservation(token, provider, db_path, tokens, attempts)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def retry_after_seconds(
    db_path: str,
    provider: str,
    budget: ProviderBudget,
    *,
    tokens: int = 1,
    attempts: int = 1,
) -> float | None:
    """Seconds until an RPM/TPM minute-window denial can clear, or ``None``.

    Returns ``None`` for daily-cap (RPD) or open-circuit denials where waiting
    a few seconds cannot help.
    """
    now = time.time()
    tokens = max(1, int(tokens))
    attempts = max(1, min(4, int(attempts)))
    conn = _connect(db_path)
    try:
        circuit = conn.execute(
            "SELECT open_until FROM llm_quota_circuit WHERE provider=?",
            (provider,),
        ).fetchone()
        if circuit and circuit[0] > now:
            return None
        if budget.rpd:
            requests_day = conn.execute(
                "SELECT COUNT(*) FROM llm_quota_requests WHERE provider=? AND started >= ?",
                (provider, now - 86400),
            ).fetchone()[0]
            if requests_day + 1 > budget.rpd:
                return None
        waits: list[float] = []
        if budget.rpm:
            rows = conn.execute(
                "SELECT started FROM llm_quota_requests WHERE provider=? AND started >= ? "
                "ORDER BY started ASC",
                (provider, now - 60),
            ).fetchall()
            excess = len(rows) + 1 - budget.rpm
            if excess > 0 and len(rows) >= excess:
                waits.append(max(0.05, (rows[excess - 1][0] + 60.0) - now))
        if budget.tpm:
            rows = conn.execute(
                "SELECT started, tokens FROM llm_quota_requests WHERE provider=? AND started >= ? "
                "ORDER BY started ASC",
                (provider, now - 60),
            ).fetchall()
            held_tokens = conn.execute(
                "SELECT COALESCE(SUM(reserved_tokens),0) FROM llm_quota_reservations "
                "WHERE provider=? AND expires >= ?",
                (provider, now),
            ).fetchone()[0]
            used_tokens = sum(row[1] for row in rows)
            deficit = used_tokens + held_tokens + tokens - budget.tpm
            if deficit > 0 and rows:
                freed = 0
                for started, row_tokens in rows:
                    freed += row_tokens
                    if freed >= deficit:
                        waits.append(max(0.05, (started + 60.0) - now))
                        break
        return max(waits) if waits else None
    finally:
        conn.close()


def finish(
    reservation: Reservation,
    *,
    actual_tokens: int | None,
    succeeded: bool,
    circuit_failure: bool | None = None,
    attempts_used: int | None = None,
) -> None:
    conn = _connect(reservation.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if actual_tokens is not None:
            charged = max(0, int(actual_tokens))
        elif succeeded:
            # No provider usage envelope: retain the full reservation conservatively.
            charged = reservation.reserved_tokens
        else:
            charged = 0
        conn.execute(
            "UPDATE llm_quota_requests SET tokens=?, succeeded=? WHERE token=?",
            (charged, int(succeeded), reservation.token),
        )
        if attempts_used is not None and reservation.attempts > 1:
            used = max(1, min(reservation.attempts, int(attempts_used)))
            for unused_index in range(used, reservation.attempts):
                conn.execute(
                    "DELETE FROM llm_quota_requests WHERE token=?",
                    (f"{reservation.token}:{unused_index}",),
                )
        now = time.time()
        failed_health_check = (not succeeded) if circuit_failure is None else circuit_failure
        health_success = not failed_health_check
        conn.execute(
            "INSERT INTO llm_quota_health(provider,happened,succeeded) VALUES(?,?,?)",
            (reservation.provider, now, int(health_success)),
        )
        circuit = conn.execute(
            "SELECT open_until,probe_token FROM llm_quota_circuit WHERE provider=?",
            (reservation.provider,),
        ).fetchone()
        if circuit and circuit[1] == reservation.token:
            conn.execute(
                "UPDATE llm_quota_circuit SET open_until=?,probe_token='' WHERE provider=?",
                (0 if health_success else now + 60, reservation.provider),
            )
        elif failed_health_check:
            recent = conn.execute(
                "SELECT succeeded,happened FROM llm_quota_health WHERE provider=? "
                "ORDER BY happened DESC,id DESC LIMIT 3",
                (reservation.provider,),
            ).fetchall()
            if len(recent) == 3 and all(not row[0] for row in recent) and now - recent[-1][1] <= 60:
                conn.execute(
                    "INSERT INTO llm_quota_circuit(provider,open_until,probe_token) VALUES(?,?, '') "
                    "ON CONFLICT(provider) DO UPDATE SET open_until=excluded.open_until,probe_token=''",
                    (reservation.provider, now + 60),
                )
        conn.execute("DELETE FROM llm_quota_reservations WHERE token=?", (reservation.token,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
