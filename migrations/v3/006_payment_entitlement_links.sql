-- Link each manually approved payment to at most one credit ledger entry.
--
-- Existing ledger rows and approved payments keep their current values: the
-- new column is NULL until a new approval is processed. The partial unique
-- index is a database-level backstop for retries or concurrent reviewers; the
-- status transition and credit insert still commit in one transaction.
ALTER TABLE ledger ADD COLUMN payment_id INTEGER REFERENCES payments(payment_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_one_credit_per_payment
    ON ledger(payment_id)
    WHERE kind = 'credit' AND payment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_payments_user_history
    ON payments(user_id, payment_id DESC);
