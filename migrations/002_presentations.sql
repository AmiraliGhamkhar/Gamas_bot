-- افزودن پشتیبانی فایل‌های ارائه (PowerPoint) به جدول ارسال‌ها
ALTER TABLE audio_submissions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'audio';
ALTER TABLE audio_submissions ADD COLUMN slide_count INTEGER;
ALTER TABLE audio_submissions ADD COLUMN clip_count INTEGER;
ALTER TABLE audio_submissions ADD COLUMN media_duration REAL;

CREATE INDEX IF NOT EXISTS idx_submissions_source_type
    ON audio_submissions(source_type);

-- هر ردیف، یک فایل رسانه‌ای استخراج‌شده از ارائه است
CREATE TABLE IF NOT EXISTS presentation_clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES audio_submissions(id) ON DELETE CASCADE,
    slide_number INTEGER,
    part_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('audio', 'video')),
    duration REAL,
    included INTEGER NOT NULL DEFAULT 1 CHECK (included IN (0, 1)),
    skip_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_presentation_clips_submission
    ON presentation_clips(submission_id);
