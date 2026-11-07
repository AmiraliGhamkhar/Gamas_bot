-- Optional fingerprint of the normalized receipt, for review warnings only.
ALTER TABLE payments ADD COLUMN receipt_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_payments_receipt_hash ON payments(receipt_hash) WHERE receipt_hash IS NOT NULL;
