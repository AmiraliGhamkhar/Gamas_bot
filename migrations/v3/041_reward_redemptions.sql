-- Phase 3: one redemption = one reward debit + one credit grant, atomically.
--
-- The redeem endpoint previously debited reward points and then *reported* a
-- credit grant that it never wrote into ``ledger``, so the user lost points and
-- gained nothing (audit D-05), and repeating the request spent points again.
--
-- Three things make the operation verifiable and retry-safe:
--
--   * ``reward_redemptions`` is the unique record of a successful redemption.
--     It exists before the debit and the grant, both of which reference it, so
--     "did this actually happen?" is answered by one row rather than inferred
--     from a ledger note.
--   * ``idempotency_key`` (unique per user when present) lets a retried request
--     return the original result instead of spending a second time. Rows written
--     before this migration simply have NULL and are not affected.
--   * ``ledger_id`` points at the *real* credit lot in ``ledger`` — the same
--     table payments, admin grants and the processing pipeline already use. It
--     stays NULL only for catalog entries that grant no processing credit, which
--     is exactly when the API must not claim one was granted.
--
-- Purely additive: new table and indexes only. No existing row, balance or
-- credit lot is touched, and no back-fill is attempted (a past redemption that
-- never granted credit cannot be reconstructed safely, so it is left as is).

CREATE TABLE IF NOT EXISTS reward_redemptions (
    redemption_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    reward_code     TEXT    NOT NULL,
    cost_points     INTEGER NOT NULL CHECK (cost_points > 0),
    credit_seconds  INTEGER NOT NULL DEFAULT 0 CHECK (credit_seconds >= 0),
    idempotency_key TEXT,
    ledger_id       INTEGER REFERENCES ledger(id),
    created_at      TEXT    NOT NULL
);

-- A retried request must resolve to the same redemption, never a second debit.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reward_redemptions_idempotency
    ON reward_redemptions(user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Per-user, per-reward usage counting (``max_per_user``).
CREATE INDEX IF NOT EXISTS idx_reward_redemptions_usage
    ON reward_redemptions(user_id, reward_code, redemption_id);

-- Every reward debit belongs to exactly one redemption, so the same redemption
-- can never be debited twice even if a caller retried the ledger write itself.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reward_points_redemption_ref
    ON reward_points_ledger(ref_id)
    WHERE ref_id LIKE 'redemption:%';
