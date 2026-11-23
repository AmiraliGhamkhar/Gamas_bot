# Implementation Plan

## Phase 1–3 (Completed & Merged in PR #152)
- Flashcard removal, Study Packs schema (`037`), private FS storage, `initData` HMAC auth, WSGI API, and Mini App scaffold (My Account + Library).

## Remaining Tasks (Phases 4–5)

### Task 1: Fix Remaining Phase 1 Test Expectations
- **Scope**: Update flashcard-era assertions in `tests/test_free_uses.py` and `tests/test_reaudit_workflows.py` to quiz-only.
- **Dependencies**: None.
- **Acceptance Criteria**: `pytest` passes on both files.

### Task 2: Database Migration `038_review_and_explore.sql` & Repository
- **Scope**: Add `explore_questions`, `explore_impressions`, `user_topic_preferences`, and `idx_reward_points_unique_ref`; add `gamas_bot/db_explore.py` and wire into `Database`.
- **Dependencies**: None.
- **Acceptance Criteria**: Migration applies cleanly on fresh and existing DBs; unit tests verify unique reward guard and impression logging.

### Task 3: Review Tab Backend & Frontend (Phase 4)
- **Scope**: Add `/api/review/sessions`, `/api/review/sessions/{id}`, `/api/review/sessions/{id}/answer` in `webapp_api.py`; implement `ReviewTab` in `miniapp/src/App.tsx`.
- **Dependencies**: Phase 2–3.
- **Acceptance Criteria**: Users can list and answer their private Study Pack quiz sessions in the Mini App; ownership enforced.

### Task 4: Explore Recommendation Engine & Seed Bank (Phase 5)
- **Scope**: Implement deterministic ranking (`gamas_bot/explore_recommender.py`), `/api/explore/*` and `/api/rewards/*` endpoints, and 200 approved easy Persian questions (pending user review).
- **Dependencies**: Task 2.
- **Acceptance Criteria**: Engagement weights (`3/2/1/0`) do not grant points; only first-time correct answers to approved Explore questions grant points; Explore UI shows no counts/indices/progress bars.
