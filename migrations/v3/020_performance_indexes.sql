-- Read-path indexes for balances, administration and retention.
CREATE INDEX IF NOT EXISTS idx_ledger_user_kind ON ledger(user_id, kind, id);
CREATE INDEX IF NOT EXISTS idx_users_quiz_points ON users(quiz_points);
CREATE INDEX IF NOT EXISTS idx_users_banned_seen ON users(banned, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_users_seen_active ON users(last_seen_at) WHERE banned = 0;
CREATE INDEX IF NOT EXISTS idx_transcriptions_created ON transcriptions(created_at);
CREATE INDEX IF NOT EXISTS idx_documents_library_item ON documents(library_item_id);
