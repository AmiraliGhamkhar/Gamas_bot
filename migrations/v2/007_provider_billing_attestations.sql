-- Explicit, auditable account-billing attestation for provider credentials.
-- Forward-only additions; all credentials that predate this migration are
-- intentionally unknown and therefore cannot authorize free or paid generation
-- until an administrator re-attests them through the AI key panel.
ALTER TABLE provider_credentials ADD COLUMN billing_attested_at TEXT;
ALTER TABLE provider_credentials ADD COLUMN billing_attested_by_admin_id INTEGER;
ALTER TABLE ai_usage_records ADD COLUMN estimated_output_tokens INTEGER;
CREATE INDEX IF NOT EXISTS idx_ai_usage_canonical_created
    ON ai_usage_records(canonical, created_at DESC);
UPDATE provider_credentials
SET billing_state='unknown', billing_attested_at=NULL, billing_attested_by_admin_id=NULL,
    free_only=NULL, paid_allowed=NULL;
