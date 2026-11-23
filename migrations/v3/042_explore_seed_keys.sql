-- Phase 4: give every seed question a stable identity.
--
-- The Explore seed bank used to be generated at insert time (ten questions
-- repeated with a "(پرسش N)" suffix), so nothing in the row said "this came
-- from the bank" and a second seed run inserted a second copy of everything.
--
-- ``seed_key`` is that identity. It is NULL for every question a user or an
-- admin created, so user-published content is untouched by the seeder, and the
-- partial unique index makes a duplicate seed row impossible even if two
-- seeding processes raced.
--
-- Purely additive: one nullable column plus one index. No row is rewritten, no
-- question is deleted, and the existing ``explore_questions`` rows stay valid.

ALTER TABLE explore_questions ADD COLUMN seed_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_explore_questions_seed_key
    ON explore_questions(seed_key)
    WHERE seed_key IS NOT NULL;
