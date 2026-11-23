-- Phase 1: link Study Packs to the real processing pipeline.
--
-- Purely additive. Every new column is nullable or carries a non-null default,
-- so historical rows stay valid and no existing value is rewritten. Nothing is
-- dropped and no table is recreated.
--
-- One accepted source file owns exactly one Study Pack:
--   * media / presentation / pasted-text submissions are identified by
--     ``submissions.submission_id``;
--   * document uploads (PDF/DOCX/PPTX) are identified by ``documents.id``
--     because document ingestion deliberately does not create a submission.
-- The partial unique indexes make pack creation idempotent in the database
-- itself, so a retried delivery can never create a second pack.
ALTER TABLE study_packs ADD COLUMN submission_id INTEGER;
ALTER TABLE study_packs ADD COLUMN document_id INTEGER;
-- The durable library row the pack was produced from (``library_items.item_id``).
-- It is the anchor Review uses, and it survives the delivered-file retention
-- window because source rows are stored with ``delivered = 0``.
ALTER TABLE study_packs ADD COLUMN source_item_id INTEGER;
ALTER TABLE study_packs ADD COLUMN source_filename TEXT NOT NULL DEFAULT '';
-- Failure information follows the ``submissions`` convention (error/error_kind).
ALTER TABLE study_packs ADD COLUMN error TEXT NOT NULL DEFAULT '';
ALTER TABLE study_packs ADD COLUMN error_kind TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_study_packs_submission
    ON study_packs(user_id, submission_id) WHERE submission_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_study_packs_document
    ON study_packs(user_id, document_id) WHERE document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_study_packs_source_item
    ON study_packs(user_id, source_item_id) WHERE source_item_id IS NOT NULL;

-- Study assets can either live in the private filesystem (the pre-existing
-- 'file' kind) or reference bytes that are already stored elsewhere:
--   artifact       -> submission_binary_artifacts (PDF, DOCX, review HTML)
--   transcription  -> transcriptions.text (raw transcript)
--   document_text  -> documents.text (extracted document text)
--   library_blob   -> library_file_blobs (delivered quiz HTML)
-- Reference kinds never copy bytes, which is why the same booklet the bot
-- already serves is not stored twice.
ALTER TABLE study_assets ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'file';
ALTER TABLE study_assets ADD COLUMN source_ref TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_study_assets_reference
    ON study_assets(pack_id, kind, source_kind, source_ref)
    WHERE source_ref <> '';

-- Attach generated questions to the pack that produced them. Nullable so every
-- historical session (and any session whose source has been removed) stays
-- valid; the API resolves a NULL through the source library item instead.
ALTER TABLE quiz_sessions ADD COLUMN pack_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_quiz_sessions_pack ON quiz_sessions(pack_id);
