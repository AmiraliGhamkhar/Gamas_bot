-- Store only a hash of broadcast text; admins re-enter text when resuming.
ALTER TABLE users ADD COLUMN broadcast_hard_fails INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS broadcast_runs (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    pages INTEGER NOT NULL,
    last_telegram_id INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','stopped')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
