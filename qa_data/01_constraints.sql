-- Data-quality rules as CHECK constraints on the core schema.
--
-- Run : python qa_data/run_qa.py       (reports each constraint as valid or VIOLATED)
--       or statement by statement in psql / DBeaver
--
-- Every rule is three statements:
--   1. DROP ... IF EXISTS   so the file can be re-run
--   2. ADD ... NOT VALID    the rule now guards every future insert and update,
--                           without failing on rows that are already there
--   3. VALIDATE             checks the existing rows; an error here is a data-quality
--                           finding, and the rule stays in place as NOT VALID
--
-- A CHECK passes when its expression is NULL, so 'price >= 0' accepts a missing
-- price.  Missing values are counted separately in 02_checks.sql.
-- Rules that depend on today's date or on other tables cannot be CHECK
-- constraints; those are queries in 02_checks.sql too.
-- A rule that the source data itself breaks is a warning in 02_checks.sql, not a
-- constraint here.  Example: one game has no name in either Kaggle file, and a
-- name constraint would make every run fail, so the failure would say nothing.

-- --- core.games ------------------------------------------------------------------

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_price_not_negative;
ALTER TABLE core.games ADD CONSTRAINT ck_games_price_not_negative
    CHECK (price >= 0) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_price_not_negative;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_discount_is_percent;
ALTER TABLE core.games ADD CONSTRAINT ck_games_discount_is_percent
    CHECK (discount BETWEEN 0 AND 100) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_discount_is_percent;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_owner_range_ordered;
ALTER TABLE core.games ADD CONSTRAINT ck_games_owner_range_ordered
    CHECK (owners_min >= 0 AND owners_min <= owners_max) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_owner_range_ordered;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_reviews_not_negative;
ALTER TABLE core.games ADD CONSTRAINT ck_games_reviews_not_negative
    CHECK (positive >= 0 AND negative >= 0 AND recommendations >= 0) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_reviews_not_negative;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_counts_not_negative;
ALTER TABLE core.games ADD CONSTRAINT ck_games_counts_not_negative
    CHECK (dlc_count >= 0 AND achievements >= 0 AND peak_ccu >= 0) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_counts_not_negative;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_playtime_not_negative;
ALTER TABLE core.games ADD CONSTRAINT ck_games_playtime_not_negative
    CHECK (average_playtime_forever >= 0 AND average_playtime_two_weeks >= 0
           AND median_playtime_forever >= 0 AND median_playtime_two_weeks >= 0) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_playtime_not_negative;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_metacritic_range;
ALTER TABLE core.games ADD CONSTRAINT ck_games_metacritic_range
    CHECK (metacritic_score BETWEEN 1 AND 100) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_metacritic_range;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_required_age_range;
ALTER TABLE core.games ADD CONSTRAINT ck_games_required_age_range
    CHECK (required_age BETWEEN 0 AND 21) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_required_age_range;

ALTER TABLE core.games DROP CONSTRAINT IF EXISTS ck_games_has_platform;
ALTER TABLE core.games ADD CONSTRAINT ck_games_has_platform
    CHECK (windows OR mac OR linux) NOT VALID;
ALTER TABLE core.games VALIDATE CONSTRAINT ck_games_has_platform;

-- --- lookup tables: names are trimmed and not empty --------------------------------

ALTER TABLE core.tags DROP CONSTRAINT IF EXISTS ck_tags_name_clean;
ALTER TABLE core.tags ADD CONSTRAINT ck_tags_name_clean
    CHECK (name <> '' AND name = btrim(name)) NOT VALID;
ALTER TABLE core.tags VALIDATE CONSTRAINT ck_tags_name_clean;

ALTER TABLE core.genres DROP CONSTRAINT IF EXISTS ck_genres_name_clean;
ALTER TABLE core.genres ADD CONSTRAINT ck_genres_name_clean
    CHECK (name <> '' AND name = btrim(name)) NOT VALID;
ALTER TABLE core.genres VALIDATE CONSTRAINT ck_genres_name_clean;

ALTER TABLE core.developers DROP CONSTRAINT IF EXISTS ck_developers_name_clean;
ALTER TABLE core.developers ADD CONSTRAINT ck_developers_name_clean
    CHECK (name <> '' AND name = btrim(name)) NOT VALID;
ALTER TABLE core.developers VALIDATE CONSTRAINT ck_developers_name_clean;

ALTER TABLE core.languages DROP CONSTRAINT IF EXISTS ck_languages_name_clean;
ALTER TABLE core.languages ADD CONSTRAINT ck_languages_name_clean
    CHECK (name <> '' AND name = btrim(name)) NOT VALID;
ALTER TABLE core.languages VALIDATE CONSTRAINT ck_languages_name_clean;

-- --- link tables -------------------------------------------------------------------

ALTER TABLE core.game_tags DROP CONSTRAINT IF EXISTS ck_game_tags_position_positive;
ALTER TABLE core.game_tags ADD CONSTRAINT ck_game_tags_position_positive
    CHECK (position >= 1) NOT VALID;
ALTER TABLE core.game_tags VALIDATE CONSTRAINT ck_game_tags_position_positive;
