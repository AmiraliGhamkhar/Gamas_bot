-- Enforce valid payment states and irreversible terminal transitions.
--
-- Legacy 'pending' rows are retained for backward compatibility with historical
-- imports; all new orders start in 'awaiting_payment' and move explicitly
-- through 'pending_review' before reaching 'approved' or 'rejected'.
-- Only status changes are guarded on UPDATE so receipt expiration cleanup
-- (which sets receipt_path = NULL without changing status) remains allowed.

CREATE TRIGGER IF NOT EXISTS trg_payments_insert_status
BEFORE INSERT ON payments
WHEN NEW.status NOT IN (
    'awaiting_payment', 'pending_review', 'approved', 'rejected', 'cancelled', 'pending'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid payment status');
END;

CREATE TRIGGER IF NOT EXISTS trg_payments_update_status
BEFORE UPDATE OF status ON payments
BEGIN
    SELECT CASE
        WHEN NEW.status NOT IN (
            'awaiting_payment', 'pending_review', 'approved', 'rejected', 'cancelled', 'pending'
        ) THEN RAISE(ABORT, 'invalid payment status')
        WHEN OLD.status IN ('approved', 'rejected', 'cancelled') AND NEW.status != OLD.status
            THEN RAISE(ABORT, 'payment status is terminal')
        WHEN OLD.status != 'approved' AND NEW.status = 'approved'
            AND (NEW.receipt_path IS NULL OR NEW.purchased_seconds IS NULL OR NEW.purchased_seconds <= 0)
            THEN RAISE(ABORT, 'approved payment requires receipt and positive entitlement')
        WHEN NEW.status = 'cancelled'
            AND (OLD.status NOT IN ('awaiting_payment', 'pending') OR OLD.receipt_path IS NOT NULL)
            THEN RAISE(ABORT, 'cannot cancel submitted or reviewed payment')
    END;
END;
