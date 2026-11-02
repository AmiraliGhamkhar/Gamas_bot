-- Workstream C: invite attribution and idempotent referral rewards.
--
-- Attribution is first-start only: one row per referred user, enforced by the
-- primary key. Rewards are append-only ledger credits; the unique key makes a
-- replayed approval or a re-sent /start unable to pay twice.

ALTER TABLE users ADD COLUMN referral_code TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code
    ON users(referral_code) WHERE referral_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS referrals (
    referred_user_id INTEGER PRIMARY KEY
                     REFERENCES users(telegram_id) ON DELETE CASCADE,
    referrer_id      INTEGER NOT NULL
                     REFERENCES users(telegram_id) ON DELETE CASCADE,
    code             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    CHECK (referred_user_id <> referrer_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer
    ON referrals(referrer_id);

CREATE TABLE IF NOT EXISTS referral_rewards (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id      INTEGER NOT NULL
                     REFERENCES users(telegram_id) ON DELETE CASCADE,
    referred_user_id INTEGER NOT NULL
                     REFERENCES users(telegram_id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('join', 'payment')),
    payment_id       INTEGER NOT NULL DEFAULT 0,
    seconds          INTEGER NOT NULL CHECK (seconds > 0),
    created_at       TEXT NOT NULL,
    UNIQUE (referred_user_id, kind, payment_id)
);

CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer
    ON referral_rewards(referrer_id);
