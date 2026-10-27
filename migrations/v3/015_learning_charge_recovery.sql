-- Paid learning generations must be recoverable just like free-use claims.
-- A row is inserted in the same transaction as its debit and allocations;
-- saving the corresponding deck/quiz finalizes it in the artifact transaction.
-- Startup refunds only rows still pending, exactly once.
CREATE TABLE IF NOT EXISTS learning_charges (
    operation_id TEXT PRIMARY KEY NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    debit_id INTEGER NOT NULL UNIQUE REFERENCES ledger(id),
    seconds INTEGER NOT NULL CHECK (seconds > 0),
    note TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'finalized', 'refunded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learning_charges_pending
    ON learning_charges(status) WHERE status = 'pending';
