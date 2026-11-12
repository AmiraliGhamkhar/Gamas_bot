"""One-shot importer: bring v2 users, plans, payments and credit into a v3 database.

v3 keeps one append-only ledger (credit/debit) and derives every balance from
it, so the v2 ``entitlements`` state (the authoritative current balances) is
replayed into the ledger instead of being copied as a stored number:

    each active entitlement  ->  one ``credit`` row (granted seconds + expiry)
                              +  one ``debit``  row (seconds already consumed)

The derived v3 balance therefore equals the v2 remaining balance, and the
expiry dates survive. Payment entitlements link to their imported v3 payment,
with the original v2 request ID retained as a cross-reference. The full v2
``usage_ledger`` event history stays in the v2 database file as the audit
archive; only live balances are migrated.

Usage::

    python scripts/import_v2.py --from data/gamas_v2.db --into data/gamas.db

The script is one-shot by design: it refuses to run if the target already has
users, and everything happens inside a single transaction (all or nothing).

It never writes to the source database. This command imports users, plans,
payment requests and eligible current credit only; v2 submissions, transcripts,
usage events and admin audit rows remain in that source archive rather than being
translated into unrelated v3 tables.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from gamas_bot.db import ADD_COLUMN_RE, MIGRATIONS_DIR  # noqa: E402

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
    except ValueError as exc:
        raise ValueError(f"invalid v2 entitlement timestamp: {expires_at!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _without_applied_columns(target: sqlite3.Connection, raw_sql: str) -> str:
    """Drop ``ADD COLUMN`` statements whose column already exists.

    ``executescript`` is not transactional, so a crash midway through a
    migration must be safe to retry; SQLite has no ``ADD COLUMN IF NOT
    EXISTS``.
    """
    known: dict[str, set[str]] = {}
    kept: list[str] = []
    for line in raw_sql.splitlines():
        match = ADD_COLUMN_RE.match(line)
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
            "pr.created_at, pr.reviewed_at, pr.reviewer_telegram_id, "
            "pr.amount_toman, p.included_seconds, p.validity_days "
            "FROM payment_requests pr "
            "JOIN users u ON u.id = pr.user_id "
            "JOIN plans p ON p.id = pr.plan_id"
        ).fetchall()
        imported_payments = 0
        v2_payment_ids: dict[int, int] = {}
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
            # v2 did not snapshot all plan fields on the order. The order's
            # amount_toman is its actual paid amount; an entitlement, when one
            # exists, is the strongest source for seconds actually granted.
            entitlement = source.execute(
                "SELECT granted_seconds FROM entitlements WHERE payment_id = ?",
                (int(payment["id"]),),
            ).fetchone()
            purchased_seconds = int(
                entitlement["granted_seconds"]
                if entitlement is not None
                else payment["included_seconds"]
            )
            inserted_payment = target.execute(
                "INSERT INTO payments"
                "(user_id, plan_code, status, receipt_path, created_at, "
                "reviewed_at, reviewed_by, purchased_seconds, purchased_price, "
                "purchased_validity_days, v2_payment_request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payment["telegram_id"],
                    payment["code"],
                    status,
                    payment["receipt_path"],
                    payment["created_at"],
                    payment["reviewed_at"],
                    payment["reviewer_telegram_id"],
                    purchased_seconds,
                    int(payment["amount_toman"]),
                    payment["validity_days"],
                    int(payment["id"]),
                ),
            )
            v2_payment_ids[int(payment["id"])] = int(inserted_payment.lastrowid)
            imported_payments += 1
        print(f"payments imported: {imported_payments}")

        # -------------------------------------------- credit -> the ledger
        now = datetime.now(timezone.utc)
        credit_rows = debit_rows = 0
        # Expected balances are accumulated with the *same* predicate the
        # ledger replay uses: active, not expired, and only entitlements this
        # importer actually converts (audit F-01).
        expected_by_user: dict[int, int] = {}
        expected_available_by_user: dict[int, int] = {}
        payment_credit_links: list[tuple[int, int, int]] = []
        entitlements = source.execute(
            "SELECT e.user_id, e.granted_seconds, e.remaining_seconds, "
            "e.starts_at, e.expires_at, e.status, e.source, e.reason, e.updated_at, "
            "e.payment_id "
            "FROM entitlements e"
        ).fetchall()
        for entry in entitlements:
            if entry["status"] != "active":
                continue
            expiry = parse_expiry(entry["expires_at"])
            if expiry is not None and expiry <= now:
                continue  # expired credit is gone in v2 as well
            starts_at = parse_expiry(entry["starts_at"])
            if starts_at is None:
                raise ValueError("active v2 entitlement has no valid starts_at timestamp")
            telegram_id = source.execute(
                "SELECT telegram_id FROM users WHERE id = ?", (entry["user_id"],)
            ).fetchone()
            if telegram_id is None:
                continue
            user_id = telegram_id[0]
            legacy_payment_id = entry["payment_id"]
            imported_payment_id = (
                v2_payment_ids.get(int(legacy_payment_id))
                if legacy_payment_id is not None
                else None
            )
            if legacy_payment_id is not None and imported_payment_id is None:
                raise ValueError(
                    "active entitlement references a payment request that was not imported: "
                    f"entitlement_user={user_id} v2_payment_request={legacy_payment_id}"
                )
            if imported_payment_id is not None:
                payment_owner = target.execute(
                    "SELECT user_id FROM payments WHERE payment_id = ?",
                    (imported_payment_id,),
                ).fetchone()
                if payment_owner is None or int(payment_owner[0]) != int(user_id):
                    raise ValueError(
                        "active entitlement and imported payment belong to different users: "
                        f"entitlement_user={user_id} v2_payment_request={legacy_payment_id}"
                    )
            granted = int(entry["granted_seconds"])
            remaining = int(entry["remaining_seconds"])
            note = f"v2 import ({entry['source']})"
            if entry["reason"]:
                note += f": {entry['reason']}"
            credit = target.execute(
                "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at, payment_id) "
                "VALUES (?, ?, 'credit', ?, ?, ?, ?)",
                (
                    user_id, granted, note, entry["expires_at"], entry["starts_at"],
                    imported_payment_id,
                ),
            )
            credit_rows += 1
            if imported_payment_id is not None and legacy_payment_id is not None:
                payment_credit_links.append(
                    (int(credit.lastrowid), int(user_id), int(legacy_payment_id))
                )
            consumed = granted - remaining
            if consumed > 0:
                debit = target.execute(
                    "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
                    "VALUES (?, ?, 'debit', 'v2 import: consumed before migration', NULL, ?)",
                    (user_id, consumed, entry["updated_at"] or entry["starts_at"]),
                )
                target.execute(
                    "INSERT INTO debit_allocations(debit_id, credit_id, seconds) VALUES (?, ?, ?)",
                    (debit.lastrowid, credit.lastrowid, consumed),
                )
                debit_rows += 1
            expected_by_user[user_id] = expected_by_user.get(user_id, 0) + remaining
            if starts_at <= now:
                expected_available_by_user[user_id] = (
                    expected_available_by_user.get(user_id, 0) + remaining
                )
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
        imported_user_ids = sorted(int(user["telegram_id"]) for user in users)
        for user_id in imported_user_ids:
            if user_id in unlimited_set:
                continue
            expected = expected_by_user.get(user_id, 0)
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
            available = target.execute(
                "SELECT COALESCE(SUM(MAX(0, l.seconds - COALESCE(("
                "SELECT SUM(a.seconds) FROM debit_allocations a WHERE a.credit_id = l.id"
                "), 0))), 0) FROM ledger l "
                "WHERE l.user_id = ? AND l.kind = 'credit' "
                "AND julianday(l.created_at) <= julianday(?) "
                "AND (l.expires_at IS NULL OR julianday(l.expires_at) > julianday(?))",
                (user_id, now.isoformat(), now.isoformat()),
            ).fetchone()[0]
            expected_available = expected_available_by_user.get(user_id, 0)
            if int(expected_available) != int(available or 0):
                print(
                    f"  AVAILABLE MISMATCH user={user_id}: "
                    f"v2 available={expected_available}, v3 available={available}",
                    file=sys.stderr,
                )
                problems += 1
        for credit_id, user_id, legacy_payment_id in payment_credit_links:
            link = target.execute(
                "SELECT l.user_id, p.user_id, p.v2_payment_request_id "
                "FROM ledger l JOIN payments p ON p.payment_id = l.payment_id "
                "WHERE l.id = ? AND l.kind = 'credit'",
                (credit_id,),
            ).fetchone()
            if (
                link is None
                or int(link[0]) != user_id
                or int(link[1]) != user_id
                or int(link[2]) != legacy_payment_id
            ):
                print(
                    f"  PAYMENT LINK MISMATCH credit={credit_id} user={user_id} "
                    f"v2_payment_request={legacy_payment_id}",
                    file=sys.stderr,
                )
                problems += 1
        foreign_key_errors = target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            print(
                f"  FOREIGN KEY CHECK failed: {len(foreign_key_errors)} violation(s)",
                file=sys.stderr,
            )
            problems += len(foreign_key_errors)
        integrity_rows = target.execute("PRAGMA integrity_check").fetchall()
        if len(integrity_rows) != 1 or str(integrity_rows[0][0]).lower() != "ok":
            print(
                "  SQLITE INTEGRITY CHECK failed: "
                + "; ".join(str(row[0]) for row in integrity_rows[:10]),
                file=sys.stderr,
            )
            problems += max(1, len(integrity_rows))
        if problems:
            raise SystemExit(f"financial/data integrity verification failed for {problems} invariant(s)")

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
