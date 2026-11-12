-- Record the one first-approved-payment decision for each attributed referral.
-- A referrer ban at that first approval forfeits the reward; later payments do
-- not reopen qualification. Existing referral reward rows remain authoritative
-- and are recognized by the runtime without rewriting/backfilling history.
ALTER TABLE referrals ADD COLUMN first_approved_payment_id INTEGER
    REFERENCES payments(payment_id);
ALTER TABLE referrals ADD COLUMN payment_reward_status TEXT
    CHECK (payment_reward_status IN (
        'rewarded', 'forfeited_banned', 'forfeited_prior_approval'
    ));
