"""One-shot importer: bring v2 users, plans, payments and credit into a v3 database.

v3 keeps one append-only ledger (credit/debit) and derives every balance from
it, so the v2 ``entitlements`` state (the authoritative current balances) is
replayed into the ledger instead of being copied as a stored number:

    each active entitlement  ->  one ``credit`` row (granted seconds + expiry)
                              +  one ``debit``  row (seconds already consumed)

The derived v3 balance therefore equals the v2 remaining balance, and the
expiry dates survive. The full v2 ``usage_ledger`` event history stays in the
v2 database file as the audit archive; only live balances are migrated.

Usage::

    python scripts/import_v2.py --from data/gamas_v2.db --into data/gamas.db

The script is one-shot by design: it refuses to run if the target already has
users, and everything happens inside a single transaction (all or nothing).

It never writes to the source database.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from gamas_bot.db import MIGRATIONS_DIR  # noqa: E402

V2_STATUS_MAP = {
    "awaiting_receipt": "pending",
    "pending": "pending",
    "approved": "approved",
    "rejected": "rejected",
    "cancelled": "rejected",
}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_expiry(expires_at: str | None) -> datetime | None:
    if not expires_at:
        return None
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_ADD_COLUMN_RE = re.compile(
    r"(?i)^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"[\"'`]?([A-Za-z_][A-Za-z0-9_]*)[\"'`]?"
)


def _without_applied_columns(target: sqlite3.Connection, raw_sql: str) -> str:
    """Drop ``ADD COLUMN`` statements whose column already exists.

    ``executescript`` is not transactional, so a crash midway through a
    migration must be safe to retry; SQLite has no ``ADD COLUMN IF NOT
    EXISTS``.
    """
    known: dict[str, set[str]] = {}
    kept: list[str] = []
    for line in raw_sql.splitlines():
        match = _ADD_COLUMN_RE.match(line)
        if match:
            table, column = match.group(1), match.group(2)
            if table not in known:
                known[table] = {
                    row[1] for row in target.execute(f"PRAGMA table_info({table})")
                }
            if column in known[table]:
                continue
        kept.append(line)
    return "\n".join(kept)


def ensure_v3_schema(target: sqlite3.Connection) -> None:
    """Apply the v3 migrations to a fresh target (same as bot startup)."""
    target.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in target.execute("SELECT name FROM schema_migrations")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        raw_sql = path.read_text(encoding="utf-8")
        target.executescript(_without_applied_columns(target, raw_sql))
        target.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (path.name, iso_now()),
        )


def source_counts(source: sqlite3.Connection) -> dict[str, int]:
    def count(table: str) -> int:
        try:
            return source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            return 0

    return {
        name: count(name)
        for name in (
            "users", "plans", "payment_requests", "entitlements",
            "audio_submissions", "usage_ledger",
        )
    }


def import_v2(source_path: Path, target_path: Path) -> int:
    if not source_path.is_file():
        print(f"source database not found: {source_path}", file=sys.stderr)
        return 2
    target_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(target_path)
    # The database holds Telegram ids and full transcripts; never leave it
    # world-readable just because this entry point does not set a umask
    # (audit F-12).
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        print(f"warning: could not restrict permissions on {target_path}", file=sys.stderr)

    try:
        target.execute("PRAGMA foreign_keys=ON")
        ensure_v3_schema(target)
        target.commit()  # executescript leaves an open transaction

        existing = target.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing:
            print(
                f"target already has {existing} users; the import is one-shot "
                "and refuses to merge into a used database",
                file=sys.stderr,
            )
            return 2

        counts = source_counts(source)
        print(
            "source overview: "
            + ", ".join(f"{name}={count}" for name, count in counts.items())
        )

        target.execute("BEGIN")

        # ---------------------------------------------------------- users
        users = source.execute(
            "SELECT telegram_id, first_seen, is_banned, is_unlimited FROM users "
            "WHERE telegram_id IS NOT NULL"
        ).fetchall()
        unlimited: list[int] = []
        for user in users:
            target.execute(
                "INSERT INTO users(telegram_id, joined_at, banned, is_unlimited, "
                "unlimited_reason) VALUES (?, ?, ?, ?, ?)",
                (
                    user["telegram_id"],
                    user["first_seen"],
                    1 if user["is_banned"] else 0,
                    1 if user["is_unlimited"] else 0,
                    "v2 import" if user["is_unlimited"] else None,
                ),
            )
            if user["is_unlimited"]:
                unlimited.append(user["telegram_id"])
        print(f"users imported: {len(users)}")

        # ---------------------------------------------------------- plans
        plans = source.execute(
            "SELECT code, included_seconds, price_toman, validity_days, enabled "
            "FROM plans WHERE is_free = 0"
        ).fetchall()
        for plan in plans:
            target.execute(
                "INSERT INTO plans(code, hours, price, validity_days, is_custom, active) "
                "VALUES (?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(code) DO UPDATE SET active = excluded.active",
                (
                    plan["code"],
                    plan["included_seconds"] / 3600.0,
                    plan["price_toman"],
                    plan["validity_days"],
                    1 if plan["enabled"] else 0,
                ),
            )
        print(f"paid plans imported: {len(plans)}")

        # -------------------------------------------------------- payments
        payments = source.execute(
            "SELECT pr.id, u.telegram_id, p.code, pr.status, pr.receipt_path, "
            "pr.created_at, pr.reviewed_at, pr.reviewer_telegram_id "
            "FROM payment_requests pr "
            "JOIN users u ON u.id = pr.user_id "
            "JOIN plans p ON p.id = pr.plan_id"
        ).fetchall()
        imported_payments = 0
        for payment in payments:
            status = V2_STATUS_MAP.get(payment["status"])
            if status is None:
                continue
            # The plan must exist for the foreign key; insert an inactive stub
            # if a plan disappeared from the v2 catalogue meanwhile.
            row = target.execute(
                "SELECT 1 FROM plans WHERE code = ?", (payment["code"],)
            ).fetchone()
            if row is None:
                source_plan = source.execute(
                    "SELECT included_seconds, price_toman, validity_days FROM plans "
                    "WHERE code = ?",
                    (payment["code"],),
                ).fetchone()
                if source_plan is None:
                    continue  # unreferenced plan: skip the payment row
                target.execute(
                    "INSERT OR IGNORE INTO plans"
                    "(code, hours, price, validity_days, is_custom, active) "
                    "VALUES (?, ?, ?, ?, 0, 0)",
                    (
                        payment["code"],
                        source_plan["included_seconds"] / 3600.0,
                        source_plan["price_toman"],
                        source_plan["validity_days"],
                    ),
                )
            # Freeze the purchase terms: the v3 approval credits this
            # snapshot, never a later plan edit (audit F-02/F-14).
            plan_row = target.execute(
                "SELECT hours, price, validity_days FROM plans WHERE code = ?",
                (payment["code"],),
            ).fetchone()
            purchased_seconds = (
                int(round(float(plan_row[0]) * 3600)) if plan_row else None
            )
            target.execute(
                "INSERT INTO payments"
                "(user_id, plan_code, status, receipt_path, created_at, "
                "reviewed_at, reviewed_by, purchased_seconds, purchased_price, "
                "purchased_validity_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payment["telegram_id"],
                    payment["code"],
                    status,
                    payment["receipt_path"],
                    payment["created_at"],
                    payment["reviewed_at"],
                    payment["reviewer_telegram_id"],
                    purchased_seconds,
                    int(plan_row[1]) if plan_row else None,
                    plan_row[2] if plan_row else None,
                ),
            )
            imported_payments += 1
        print(f"payments imported: {imported_payments}")

        # -------------------------------------------- credit -> the ledger
        now = datetime.now(timezone.utc)
        credit_rows = debit_rows = 0
        # Expected balances are accumulated with the *same* predicate the
        # ledger replay uses: active, not expired, and only entitlements this
        # importer actually converts (audit F-01).
        expected_by_user: dict[int, int] = {}
        entitlements = source.execute(
            "SELECT e.user_id, e.granted_seconds, e.remaining_seconds, "
            "e.starts_at, e.expires_at, e.status, e.source, e.reason, e.updated_at "
            "FROM entitlements e"
        ).fetchall()
        for entry in entitlements:
            if entry["status"] != "active":
                continue
            expiry = parse_expiry(entry["expires_at"])
            if expiry is not None and expiry <= now:
                continue  # expired credit is gone in v2 as well
            telegram_id = source.execute(
                "SELECT telegram_id FROM users WHERE id = ?", (entry["user_id"],)
            ).fetchone()
            if telegram_id is None:
                continue
            user_id = telegram_id[0]
            granted = int(entry["granted_seconds"])
            remaining = int(entry["remaining_seconds"])
            note = f"v2 import ({entry['source']})"
            if entry["reason"]:
                note += f": {entry['reason']}"
            target.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
                "VALUES (?, ?, 'credit', ?, ?, ?)",
                (user_id, granted, note, entry["expires_at"], entry["starts_at"]),
            )
            credit_rows += 1
            consumed = granted - remaining
            if consumed > 0:
                target.execute(
                    "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
                    "VALUES (?, ?, 'debit', 'v2 import: consumed before migration', NULL, ?)",
                    (user_id, consumed, entry["updated_at"] or entry["starts_at"]),
                )
                debit_rows += 1
            expected_by_user[user_id] = expected_by_user.get(user_id, 0) + remaining
        # v2 "unlimited" special users keep the v3 flag (set on the user row
        # above, so the admin panel lists them and billing skips them) and
        # also get a century of credit — belt and braces, still auditable if
        # an admin later turns the flag off.
        for user_id in unlimited:
            target.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
                "VALUES (?, ?, 'credit', 'v2 import (unlimited user)', NULL, ?)",
                (user_id, 100 * 365 * 24 * 3600, iso_now()),
            )
            credit_rows += 1
        if unlimited:
            print(f"unlimited users granted a century of credit: {len(unlimited)}")
        print(f"ledger: {credit_rows} credit rows, {debit_rows} debit rows")

        # ---------------------------------------------- verification pass
        # Compare per-user derived v3 balance with the v2 remaining balance for
        # the users that had entitlements. Unlimited users are excluded: their
        # century credit is deliberate and is not part of the v2 balance.
        unlimited_set = set(unlimited)
        problems = 0
        for user_id, expected in sorted(expected_by_user.items()):
            if user_id in unlimited_set:
                continue
            derived = target.execute(
                "SELECT COALESCE(SUM(CASE kind WHEN 'credit' THEN seconds ELSE -seconds END), 0) "
                "FROM ledger WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            if int(expected) != int(derived):
                print(
                    f"  MISMATCH user={user_id}: v2 remaining={expected}, "
                    f"v3 derived={derived}",
                    file=sys.stderr,
                )
                problems += 1
        if problems:
            raise SystemExit(f"balance verification failed for {problems} user(s)")

        # Pending payments still need an admin decision; surface them.
        pending = target.execute(
            "SELECT COUNT(*) FROM payments WHERE status = 'pending'"
        ).fetchone()[0]
        target.commit()
        print("committed.")
        print(
            f"done. users={len(users)} plans={len(plans)} "
            f"payments={imported_payments} (pending review: {pending}) "
            f"ledger credits={credit_rows} debits={debit_rows}"
        )
        return 0
    except SystemExit:
        target.rollback()
        raise
    except Exception as exc:
        target.rollback()
        print(f"import failed and was rolled back: {exc}", file=sys.stderr)
        return 1
    finally:
        source.close()
        target.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot v2 -> v3 database import (users, plans, payments, credit)."
    )
    parser.add_argument(
        "--from", dest="source", required=True, type=Path, help="path to the v2 database"
    )
    parser.add_argument(
        "--into", dest="target", required=True, type=Path, help="path to the v3 database"
    )
    args = parser.parse_args(argv)
    return import_v2(args.source, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
