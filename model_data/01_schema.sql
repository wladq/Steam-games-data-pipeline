-- Normalised model of the Steam games dataset, in its own schema: core.
--
-- Source : public.games_raw, the CSV landing table loaded by `python load_db.py`
--          (column names tidied to snake_case).
-- Run    : python model_data/build.py
--          or  psql -1 -v ON_ERROR_STOP=1 -f 01_schema.sql -f 02_populate.sql
-- Safe to re-run: the whole schema is dropped and rebuilt in one transaction.
--
-- Only structural rules live here (keys, NOT NULL on keys).  Business rules such
-- as "price is not negative" are data-quality checks and live in qa_data/.

DROP SCHEMA IF EXISTS core CASCADE;
CREATE SCHEMA core;

COMMENT ON SCHEMA core IS 'Normalised Steam games model, rebuilt from public.games_raw by model_data/';


-- --- helpers ---------------------------------------------------------------------

-- to_date() raises on impossible dates; a failed parse should give NULL instead,
-- so that one bad row cannot abort the build.  The raw text is kept beside the date.
CREATE FUNCTION core.try_date(txt text, fmt text) RETURNS date
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN to_date(txt, fmt);
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

-- The CSV stores languages as the text of a Python list, e.g. ['English', 'French'],
-- sometimes with leftovers of Steam markup inside ([b], <br>, &amp;, \r\n).
-- Returns one clean language name per row.
CREATE FUNCTION core.split_languages(list_text text) RETURNS SETOF text
LANGUAGE sql IMMUTABLE AS $$
    SELECT DISTINCT c.lang
    FROM regexp_split_to_table(btrim(list_text, '[]'), ',') AS p(part)
    CROSS JOIN LATERAL (
        SELECT btrim(
                   regexp_replace(
                       regexp_replace(p.part, '\\[rnt]|<[^>]*>|\[/?[a-z]+\]|&[a-z]+;|[*#]', ' ', 'gi'),
                       '\s+', ' ', 'g'),
                   ' ''"') AS lang
    ) AS c
    WHERE c.lang <> ''
$$;


-- --- games: one row per Steam app ------------------------------------------------

CREATE TABLE core.games (
    appid                       bigint PRIMARY KEY,
    name                        text,            -- can be NULL in the source, see qa_data
    release_date                date,            -- NULL when the raw text cannot be parsed
    release_date_raw            text,            -- as published: 'Oct 21, 2008' or 'Oct 2008'
    estimated_owners            text,            -- SteamSpy bucket as published: '20000 - 50000'
    owners_min                  bigint,          -- lower edge of the bucket
    owners_max                  bigint,          -- upper edge of the bucket
    price                       numeric(10, 2),  -- USD at scrape time, 0 = free
    discount                    integer,         -- percent at scrape time
    dlc_count                   integer,
    required_age                integer,
    windows                     boolean,
    mac                         boolean,
    linux                       boolean,
    metacritic_score            integer,         -- NULL = no score (the source stores 0)
    user_score                  integer,
    positive                    integer,         -- positive user reviews
    negative                    integer,         -- negative user reviews
    recommendations             integer,
    achievements                integer,
    peak_ccu                    integer,         -- peak concurrent users on the scrape day
    average_playtime_forever    integer,         -- minutes
    average_playtime_two_weeks  integer,         -- minutes
    median_playtime_forever     integer,         -- minutes
    median_playtime_two_weeks   integer          -- minutes
);

COMMENT ON TABLE core.games IS 'One row per Steam app. Long text and URLs stay in public.games_raw.';


-- --- lookup tables and their links to games --------------------------------------
-- position = place of the value in the source list (1 = first).

CREATE TABLE core.tags (
    tag_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name    text NOT NULL UNIQUE
);

CREATE TABLE core.game_tags (
    appid     bigint  NOT NULL REFERENCES core.games (appid) ON DELETE CASCADE,
    tag_id    integer NOT NULL REFERENCES core.tags (tag_id),
    position  integer NOT NULL,
    PRIMARY KEY (appid, tag_id)
);

CREATE TABLE core.genres (
    genre_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      text NOT NULL UNIQUE
);

CREATE TABLE core.game_genres (
    appid     bigint  NOT NULL REFERENCES core.games (appid) ON DELETE CASCADE,
    genre_id  integer NOT NULL REFERENCES core.genres (genre_id),
    position  integer NOT NULL,
    PRIMARY KEY (appid, genre_id)
);

CREATE TABLE core.developers (
    developer_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL UNIQUE
);

CREATE TABLE core.game_developers (
    appid         bigint  NOT NULL REFERENCES core.games (appid) ON DELETE CASCADE,
    developer_id  integer NOT NULL REFERENCES core.developers (developer_id),
    position      integer NOT NULL,
    PRIMARY KEY (appid, developer_id)
);

CREATE TABLE core.languages (
    language_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL UNIQUE
);

CREATE TABLE core.game_languages (
    appid        bigint  NOT NULL REFERENCES core.games (appid) ON DELETE CASCADE,
    language_id  integer NOT NULL REFERENCES core.languages (language_id),
    full_audio   boolean NOT NULL,   -- true when the language is also in the full-audio list
    PRIMARY KEY (appid, language_id)
);
