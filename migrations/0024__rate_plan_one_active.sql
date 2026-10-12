-- 0024__rate_plan_one_active.sql
-- One active rate plan per unit type.
--
-- The cabinet, the commission lookup and the channel read-API all take "the"
-- active plan of a unit type (the earliest created). The booking price and the
-- availability read joined every active plan instead, and nothing stopped a
-- second one from being created: with two, availability listed each night
-- twice and the hold failed on booking_line's one-row-per-night key (HTTP 500).
-- The API can neither deactivate nor delete a plan, so the room stayed broken.
--
-- Keep the earliest-created active plan per unit type (the one the rest of the
-- code already uses) and deactivate any extras, then make a second active plan
-- a hard error at the write itself. Partial, so inactive plans do not count.

UPDATE rate_plan
   SET active = false
 WHERE id IN (
        SELECT id
          FROM (SELECT id,
                       row_number() OVER (PARTITION BY unit_type_id
                                          ORDER BY created_at, id) AS rn
                  FROM rate_plan
                 WHERE active) ranked
         WHERE rn > 1
       );

CREATE UNIQUE INDEX IF NOT EXISTS rate_plan_one_active_key
    ON rate_plan (unit_type_id)
    WHERE active;

COMMENT ON INDEX rate_plan_one_active_key IS 'A unit type has at most one active rate plan; inactive plans do not count';
