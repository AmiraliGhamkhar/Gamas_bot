-- Workstream B: defer the mandatory-channel gate to value delivery.
--
-- A user who submits a file before joining keeps that exact request here, so a
-- successful membership check can resume it without asking for the file again.
-- One row per user: a newer blocked request replaces an older one.

ALTER TABLE users ADD COLUMN subscription_checked_at TEXT;

CREATE TABLE IF NOT EXISTS pending_gate_actions (
    user_id       INTEGER PRIMARY KEY
                  REFERENCES users(telegram_id) ON DELETE CASCADE,
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    kind          TEXT NOT NULL
                  CHECK (kind IN ('document', 'presentation', 'media', 'callback')),
    callback_data TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_gate_actions_created
    ON pending_gate_actions(created_at);
