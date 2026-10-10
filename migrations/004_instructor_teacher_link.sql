-- OptiSked AI migration 004
-- Links instructors to their registered teacher account so the Program Head
-- selects teachers from a dropdown and course qualifications can be reused.
-- app.py also performs these compatibility changes automatically at startup.

ALTER TABLE instructors ADD COLUMN user_id INTEGER;

UPDATE instructors SET user_id=(
    SELECT t.user_id FROM teachers t
    WHERE lower(t.full_name)=lower(instructors.name) AND t.user_id IS NOT NULL
    LIMIT 1)
WHERE user_id IS NULL;
