-- Study Packs, private Study Assets (filesystem-backed), and separate Reward Points ledger.

CREATE TABLE IF NOT EXISTS study_packs (
    pack_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'upload',
    status      TEXT NOT NULL DEFAULT 'ready',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_study_packs_user_created
    ON study_packs(user_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS study_assets (
    asset_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    mime_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    byte_size    INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    FOREIGN KEY (pack_id) REFERENCES study_packs(pack_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_study_assets_pack
    ON study_assets(pack_id, asset_id);

CREATE TABLE IF NOT EXISTS reward_points_ledger (
    entry_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    ref_id     TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reward_points_user
    ON reward_points_ledger(user_id, created_at DESC);
