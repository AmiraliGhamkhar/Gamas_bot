-- Provider profiles remain conservative code defaults but can be tuned per
-- deployment. NULL always means inherit the reviewed NoteProviderProfile.
-- All limits are bounded here as well as in the admin callback.

ALTER TABLE ai_provider_settings ADD COLUMN chunk_token_budget INTEGER
    CHECK (chunk_token_budget IS NULL OR chunk_token_budget BETWEEN 256 AND 1000000);
ALTER TABLE ai_provider_settings ADD COLUMN chunk_char_cap INTEGER
    CHECK (chunk_char_cap IS NULL OR chunk_char_cap BETWEEN 1000 AND 2000000);
ALTER TABLE ai_provider_settings ADD COLUMN max_output_tokens INTEGER
    CHECK (max_output_tokens IS NULL OR max_output_tokens BETWEEN 256 AND 131072);
ALTER TABLE ai_provider_settings ADD COLUMN compile_token_budget INTEGER
    CHECK (compile_token_budget IS NULL OR compile_token_budget BETWEEN 512 AND 1000000);
ALTER TABLE ai_provider_settings ADD COLUMN max_concurrency INTEGER
    CHECK (max_concurrency IS NULL OR max_concurrency BETWEEN 1 AND 32);
ALTER TABLE ai_provider_settings ADD COLUMN max_retries INTEGER
    CHECK (max_retries IS NULL OR max_retries BETWEEN 0 AND 10);
ALTER TABLE ai_provider_settings ADD COLUMN timeout_seconds INTEGER
    CHECK (timeout_seconds IS NULL OR timeout_seconds BETWEEN 10 AND 600);
