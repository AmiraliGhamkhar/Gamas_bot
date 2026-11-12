-- Keep the source v2 payment-request ID as a durable cross-reference when the
-- one-shot importer builds new v3 payment IDs. NULL for native v3 purchases.
ALTER TABLE payments ADD COLUMN v2_payment_request_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_v2_request
    ON payments(v2_payment_request_id) WHERE v2_payment_request_id IS NOT NULL;
