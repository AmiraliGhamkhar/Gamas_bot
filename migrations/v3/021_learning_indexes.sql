-- Admission and cascading quiz cleanup indexes.
CREATE INDEX IF NOT EXISTS idx_quiz_sessions_user_created ON quiz_sessions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_quiz_answers_session ON quiz_answers(session_id);
