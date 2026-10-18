-- Payment snapshots and the one-open-request invariant.
--
-- Before this migration an approval read the plan row at review time, so a
-- deactivated or re-priced plan could approve a payment and credit 0 seconds
-- (audit F-02/F-14). The purchase terms are now copied onto the payment row
-- when it is created and the approval credits exactly that snapshot.
ALTER TABLE payments ADD COLUMN purchased_seconds INTEGER;
ALTER TABLE payments ADD COLUMN purchased_price INTEGER;
ALTER TABLE payments ADD COLUMN purchased_validity_days INTEGER;
ALTER TABLE payments ADD COLUMN review_reason TEXT;

-- Collapse duplicate open requests before the unique index can be created:
-- the oldest request per user stays pending, later ones are closed as
-- superseded (they were never approved, so no credit is affected).
UPDATE payments
   SET status = 'rejected',
       reviewed_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'),
       review_reason = 'superseded by an earlier open request'
 WHERE status = 'pending'
   AND payment_id NOT IN (
       SELECT MIN(payment_id) FROM payments WHERE status = 'pending' GROUP BY user_id
   );

CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_one_pending
    ON payments(user_id) WHERE status = 'pending';
