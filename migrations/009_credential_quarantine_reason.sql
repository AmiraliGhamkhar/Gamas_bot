-- Credential quarantine reason (spec §32).
-- Forward-only and non-destructive: one ADD COLUMN. Existing quarantined rows
-- keep quarantined_at and get a NULL reason; the next status change or admin
-- action writes it. Stores only a bounded machine label such as "http_401".
ALTER TABLE provider_credentials ADD COLUMN quarantine_reason TEXT;
