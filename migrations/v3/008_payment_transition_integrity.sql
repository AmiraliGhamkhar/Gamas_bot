-- Close the remaining payment state-machine gaps without rewriting any rows.
--
-- Migration 007 blocked unknown states and terminal-state changes, but still
-- allowed backwards transitions (for example pending_review -> awaiting_payment)
-- and direct SQL reviews without complete audit metadata. Replace only its UPDATE
-- trigger; the INSERT trigger remains permissive enough for historical importers.
-- Every statement is idempotent so an interrupted migration can be retried.

DROP TRIGGER IF EXISTS trg_payments_update_status;

CREATE TRIGGER IF NOT EXISTS trg_payments_update_status
BEFORE UPDATE OF status ON payments
BEGIN
    SELECT CASE
        WHEN NEW.status NOT IN (
            'awaiting_payment', 'pending_review', 'approved', 'rejected', 'cancelled', 'pending'
        ) THEN RAISE(ABORT, 'invalid payment status')

        WHEN OLD.status IN ('approved', 'rejected', 'cancelled') AND NEW.status != OLD.status
            THEN RAISE(ABORT, 'payment status is terminal')

        -- Approval is valid only from receipt-backed review, with the complete
        -- frozen entitlement/price snapshot and reviewer audit fields.
        WHEN OLD.status != 'approved' AND NEW.status = 'approved'
            AND (
                OLD.status NOT IN ('pending_review', 'pending')
                OR OLD.receipt_path IS NULL OR NEW.receipt_path IS NULL
                OR NEW.purchased_seconds IS NULL OR NEW.purchased_seconds <= 0
                OR NEW.purchased_price IS NULL OR NEW.purchased_price <= 0
                OR NEW.reviewed_at IS NULL OR NEW.reviewed_by IS NULL
            ) THEN RAISE(ABORT, 'invalid payment approval transition')

        -- Rejection also requires a receipt, reviewer and durable reason.
        WHEN OLD.status != 'rejected' AND NEW.status = 'rejected'
            AND (
                OLD.status NOT IN ('pending_review', 'pending')
                OR OLD.receipt_path IS NULL OR NEW.receipt_path IS NULL
                OR NEW.reviewed_at IS NULL OR NEW.reviewed_by IS NULL
                OR NEW.review_reason IS NULL OR length(trim(NEW.review_reason)) = 0
            ) THEN RAISE(ABORT, 'invalid payment rejection transition')

        -- Only an unsubmitted order can be cancelled.
        WHEN OLD.status != 'cancelled' AND NEW.status = 'cancelled'
            AND (
                OLD.status NOT IN ('awaiting_payment', 'pending')
                OR OLD.receipt_path IS NOT NULL OR NEW.receipt_path IS NOT NULL
            ) THEN RAISE(ABORT, 'cannot cancel submitted or reviewed payment')

        -- A receipt moves an unpaid order forward into review, never backwards.
        WHEN OLD.status != 'pending_review' AND NEW.status = 'pending_review'
            AND (
                OLD.status NOT IN ('awaiting_payment', 'pending')
                OR NEW.receipt_path IS NULL
            ) THEN RAISE(ABORT, 'invalid receipt review transition')

        -- This form is retained solely for legacy rows and migration tooling.
        WHEN OLD.status != 'awaiting_payment' AND NEW.status = 'awaiting_payment'
            AND (OLD.status != 'pending' OR NEW.receipt_path IS NOT NULL)
            THEN RAISE(ABORT, 'invalid awaiting payment transition')

        -- New code must not reintroduce the ambiguous legacy state.
        WHEN OLD.status != 'pending' AND NEW.status = 'pending'
            THEN RAISE(ABORT, 'cannot restore legacy payment status')
    END;
END;
