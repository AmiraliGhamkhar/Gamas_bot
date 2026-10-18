"""End-to-end test of the one-shot v2 -> v3 database import."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_DIR / "scripts" / "import_v2.py"
V2_MIGRATIONS = PROJECT_DIR / "migrations" / "v2"


def insert(conn: sqlite3.Connection, table: str, **values) -> int:
    """Insert a row, filling omitted columns from the real table defaults."""
    info = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = [
        name for name, column in info.items()
        if name != "id" and name not in values
        and column[3] and column[4] is None  # NOT NULL without a default
    ]
    if missing:
        raise AssertionError(f"{table}: missing NOT NULL columns {missing}")
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO {table}({names}) VALUES ({marks})", tuple(values.values())
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class ImportV2TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.source_path = root / "v2.db"
        self.target_path = root / "v3.db"

        source = sqlite3.connect(self.source_path)
        for path in sorted(V2_MIGRATIONS.glob("*.sql")):
            source.executescript(path.read_text(encoding="utf-8"))

        # Four users; 333 is banned, 444 was unlimited in v2.
        rows = [
            (111, "2025-01-01", 0, 0),
            (222, "2025-02-01", 0, 0),
            (333, "2025-03-01", 1, 0),
            (444, "2025-04-01", 0, 1),
        ]
        for telegram_id, first_seen, banned, unlimited in rows:
            insert(
                source, "users",
                telegram_id=telegram_id, username="u", first_seen=first_seen,
                is_banned=banned, is_unlimited=unlimited,
            )
        ids = {
            telegram_id: row[0]
            for telegram_id, row in zip(
                (111, 222, 333, 444), source.execute(
                    "SELECT id FROM users ORDER BY id"
                ).fetchall(), strict=True,
            )
        }
        plan_free = source.execute(
            "SELECT id FROM plans WHERE code = 'free_1h'"
        ).fetchone()[0]
        plan_paid = source.execute(
            "SELECT id FROM plans WHERE code = 'paid_5h_30d'"
        ).fetchone()[0]

        # 222 has an approved payment (so the entitlement passes the v2 CHECK).
        payment_id = insert(
            source, "payment_requests",
            user_id=ids[222], plan_id=plan_paid, amount_toman=50000,
            status="approved", receipt_path="r/1.jpg", receipt_message_id=10,
            receipt_submitted_at="2025-02-02", created_at="2025-02-01",
            updated_at="2025-02-02", reviewed_at="2025-02-03",
            reviewer_telegram_id=999, rejection_reason=None,
        )
        # 333 has a receipt awaiting review (must survive as pending).
        insert(
            source, "payment_requests",
            user_id=ids[333], plan_id=plan_paid, amount_toman=50000,
            status="awaiting_receipt", receipt_path="r/2.jpg", receipt_message_id=11,
            receipt_submitted_at="2025-03-05", created_at="2025-03-04",
            updated_at="2025-03-04", reviewed_at=None, reviewer_telegram_id=None,
            rejection_reason=None,
        )

        # Entitlements: 111 free lifetime untouched; 222 paid, half consumed,
        # expires 2030; 333 revoked (must contribute nothing).
        insert(
            source, "entitlements",
            user_id=ids[111], plan_id=plan_free, payment_id=None,
            granted_seconds=3600, remaining_seconds=3600, starts_at="2025-01-02",
            expires_at=None, status="active", source="free_lifetime",
            granted_by_admin_id=None, reason=None,
            created_at="2025-01-02", updated_at="2025-01-02",
        )
        insert(
            source, "entitlements",
            user_id=ids[222], plan_id=plan_paid, payment_id=payment_id,
            granted_seconds=18000, remaining_seconds=9000, starts_at="2025-02-02",
            expires_at="2030-01-01", status="active", source="payment",
            granted_by_admin_id=None, reason=None,
            created_at="2025-02-02", updated_at="2025-03-03",
        )
        insert(
            source, "entitlements",
            user_id=ids[333], plan_id=plan_free, payment_id=None,
            granted_seconds=3600, remaining_seconds=100, starts_at="2025-03-02",
            expires_at=None, status="revoked", source="admin_credit",
            granted_by_admin_id=None, reason="gone",
            created_at="2025-03-02", updated_at="2025-03-02",
        )
        source.commit()
        source.close()

    def tearDown(self):
        self._tmp.cleanup()

    def run_import(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--from", str(self.source_path),
             "--into", str(self.target_path)],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )

    def target(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.target_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_balances_plans_payments_and_users_migrate(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = self.target()
        try:
            def balance(user_id: int) -> int:
                return conn.execute(
                    "SELECT COALESCE(SUM(CASE kind WHEN 'credit' THEN seconds "
                    "ELSE -seconds END), 0) FROM ledger WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]

            # The derived v3 ledger balances equal the v2 remaining balances.
            self.assertEqual(balance(111), 3600)
            self.assertEqual(balance(222), 9000)
            self.assertEqual(balance(333), 0)  # revoked entitlement dropped

            # The v2 unlimited user got a century of credit instead.
            self.assertGreater(balance(444), 99 * 365 * 24 * 3600)

            # Users and the ban flag.
            self.assertEqual(
                conn.execute("SELECT banned FROM users WHERE telegram_id=333")
                .fetchone()[0], 1,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4)

            # Paid plans migrated with hours computed from seconds.
            plan = conn.execute(
                "SELECT hours, price FROM plans WHERE code = 'paid_5h_30d'"
            ).fetchone()
            self.assertEqual(plan["hours"], 5.0)
            self.assertEqual(plan["price"], 50000)
            # The free plan is granted, not sold.
            self.assertIsNone(
                conn.execute("SELECT 1 FROM plans WHERE code = 'free_1h'").fetchone()
            )

            # Payments: approved one kept, awaiting_receipt became pending.
            payments = {
                row["user_id"]: row["status"]
                for row in conn.execute("SELECT user_id, status FROM payments")
            }
            self.assertEqual(payments, {222: "approved", 333: "pending"})

            # Expiry dates survive the replay.
            self.assertEqual(
                conn.execute(
                    "SELECT expires_at FROM ledger "
                    "WHERE user_id = 222 AND kind = 'credit'"
                ).fetchone()[0], "2030-01-01",
            )
        finally:
            conn.close()

    def test_expired_and_unlimited_entitlements_do_not_abort_the_import(self):
        """The verification uses the ledger's own predicate (audit F-01)."""
        source = sqlite3.connect(self.source_path)
        try:
            ids = {
                row[1]: row[0]
                for row in source.execute("SELECT id, telegram_id FROM users")
            }
            plan_free = source.execute(
                "SELECT id FROM plans WHERE code = 'free_1h'"
            ).fetchone()[0]
            plan_paid = source.execute(
                "SELECT id FROM plans WHERE code = 'paid_5h_30d'"
            ).fetchone()[0]
            # 111: an "active" row whose expiry is long past — v2 itself does
            # not count it, and the importer skips it.
            insert(
                source, "entitlements",
                user_id=ids[111], plan_id=plan_free, payment_id=None,
                granted_seconds=500, remaining_seconds=500, starts_at="2019-01-01",
                expires_at="2020-01-01", status="active", source="admin_credit",
                granted_by_admin_id=None, reason="expired",
                created_at="2019-01-01", updated_at="2019-01-01",
            )
            # 444: unlimited in v2 *and* holding a live entitlement; the century
            # credit must not make the comparison fail.
            insert(
                source, "entitlements",
                user_id=ids[444], plan_id=plan_paid, payment_id=None,
                granted_seconds=18000, remaining_seconds=18000, starts_at="2025-04-02",
                expires_at="2030-01-01", status="active", source="admin_credit",
                granted_by_admin_id=None, reason=None,
                created_at="2025-04-02", updated_at="2025-04-02",
            )
            source.commit()
        finally:
            source.close()
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = self.target()
        try:
            balance = conn.execute(
                "SELECT COALESCE(SUM(CASE kind WHEN 'credit' THEN seconds "
                "ELSE -seconds END), 0) FROM ledger WHERE user_id = 111"
            ).fetchone()[0]
            self.assertEqual(balance, 3600)  # expired row contributed nothing
            self.assertGreater(
                conn.execute(
                    "SELECT COALESCE(SUM(seconds), 0) FROM ledger "
                    "WHERE user_id = 444 AND kind = 'credit'"
                ).fetchone()[0],
                99 * 365 * 24 * 3600,
            )
        finally:
            conn.close()

    def test_second_run_is_refused(self):
        first = self.run_import()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_import()
        self.assertEqual(second.returncode, 2)
        self.assertIn("one-shot", second.stderr)

    def test_missing_source_is_refused(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--from", str(Path(self._tmp.name) / "nope.db"),
             "--into", str(self.target_path)],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
