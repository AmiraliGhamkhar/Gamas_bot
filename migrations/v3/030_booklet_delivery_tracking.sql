-- Bounded retry telemetry for user-initiated delivery of persisted PDF booklets.
-- The artifact remains the source of truth for booklet success; counters record
-- button-driven send attempts without creating an unbounded event log.
ALTER TABLE submission_binary_artifacts ADD COLUMN delivery_attempts INTEGER NOT NULL
    DEFAULT 0 CHECK (delivery_attempts >= 0);
ALTER TABLE submission_binary_artifacts ADD COLUMN delivery_successes INTEGER NOT NULL
    DEFAULT 0 CHECK (delivery_successes >= 0);
ALTER TABLE submission_binary_artifacts ADD COLUMN delivery_failures INTEGER NOT NULL
    DEFAULT 0 CHECK (delivery_failures >= 0);
ALTER TABLE submission_binary_artifacts ADD COLUMN last_delivery_attempt_at TEXT;
ALTER TABLE submission_binary_artifacts ADD COLUMN last_delivery_error TEXT;
