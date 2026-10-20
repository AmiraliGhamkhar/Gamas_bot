-- Credit lots preserve consumed amounts when the unused remainder expires.
CREATE TABLE IF NOT EXISTS debit_allocations (
    debit_id INTEGER NOT NULL REFERENCES ledger(id),
    credit_id INTEGER NOT NULL REFERENCES ledger(id),
    seconds INTEGER NOT NULL CHECK(seconds > 0),
    PRIMARY KEY(debit_id, credit_id)
);
CREATE TABLE IF NOT EXISTS hold_allocations (
    submission_id INTEGER NOT NULL REFERENCES holds(submission_id),
    credit_id INTEGER NOT NULL REFERENCES ledger(id),
    seconds INTEGER NOT NULL CHECK(seconds > 0),
    PRIMARY KEY(submission_id, credit_id)
);
CREATE INDEX IF NOT EXISTS idx_debit_credit ON debit_allocations(credit_id);
CREATE INDEX IF NOT EXISTS idx_hold_credit ON hold_allocations(credit_id);
-- A dedicated reference avoids depending on human-readable ledger notes.
ALTER TABLE ledger ADD COLUMN submission_id INTEGER REFERENCES submissions(submission_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_debit_submission ON ledger(submission_id)
    WHERE kind = 'debit' AND submission_id IS NOT NULL;
ALTER TABLE submissions ADD COLUMN delivered_at TEXT;
CREATE TABLE IF NOT EXISTS billing_migrations (name TEXT PRIMARY KEY);
