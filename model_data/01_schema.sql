-- Normalised model of the Steam games dataset, in its own schema: core.
--
-- Source : public.games_json, the jsonb landing table loaded by `python main.py get json`
--          (one row per game: appid, data).
-- Run    : python main.py model   (or python model_data/build.py)
--          or  psql -1 -v ON_ERROR_STOP=1 -f 01_schema.sql -f 02_populate.sql
-- Safe to re-run: the whole schema is dropped and rebuilt in one transaction.
--
-- Only structural rules live here (keys, NOT NULL on keys).  Business rules such
-- as "price is not negative" are data-quality checks and live in qa_data/.

DROP SCHEMA IF EXISTS core CASCADE;
CREATE SCHEMA core;

COMMENT ON SCHEMA core IS 'Normalised Steam games model, rebuilt from public.games_json by model_data/';


-- --- helpers: reading JSON values ------------------------------------------------
-- The same key can hold different JSON types from game to game: discount is a string
-- in some games and a number in others, tags an object or an empty array.  These
-- helpers turn every shape into one SQL type, or NULL / empty when it does not fit.

-- numbers as they are, numeric strings ('50') converted, anything else NULL
CREATE FUNCTION core.jnum(j jsonb) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN jsonb_typeof(j) = 'number' THEN (j #>> '{}')::numeric
        WHEN jsonb_typeof(j) = 'string' AND (j #>> '{}') ~ '^\s*-?[0-9]+(\.[0-9]+)?\s*$'
            THEN btrim(j #>> '{}')::numeric
    END
$$;

-- booleans as they are, the strings 'true' / 'false' converted, anything else NULL
CREATE FUNCTION core.jbool(j jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN jsonb_typeof(j) = 'boolean' THEN (j #>> '{}')::boolean
        WHEN lower(j #>> '{}') IN ('true', 'false') THEN (j #>> '{}')::boolean
    END
$$;

-- a list field as a jsonb array: arrays as they are, a lone string as a one-element
-- array (older scrapes store languages as one comma-separated string), else empty
CREATE FUNCTION core.jlist(j jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE jsonb_typeof(j)
        WHEN 'array' THEN j
        WHEN 'string' THEN jsonb_build_array(j)
        ELSE '[]'::jsonb
    END
$$;


-- --- helpers: dates and languages --------------------------------------------------

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

-- 'Oct 21, 2008', '21 Oct, 2008' or 'Oct 2008' (stored as the first of the month)
CREATE FUNCTION core.parse_release_date(txt text) RETURNS date
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN txt ~ '^[A-Za-z]{3} [0-9]{1,2}, [0-9]{4}$' THEN core.try_date(txt, 'Mon DD, YYYY')
        WHEN txt ~ '^[0-9]{1,2} [A-Za-z]{3}, [0-9]{4}$' THEN core.try_date(txt, 'DD Mon, YYYY')
        WHEN txt ~ '^[A-Za-z]{3} [0-9]{4}$'             THEN core.try_date(txt, 'Mon YYYY')
    END
$$;

-- One entry of a language list can carry leftovers of Steam markup ([b], <strong>,
-- &amp;), HTML escaping stacked several times ('English&amp;amp;amp;lt;strong...'),
-- several languages divided by line breaks or semicolons ('English\r\nRussian',
-- 'German;'), and Steam's untranslated placeholder '#lang_slovakian', which becomes
-- 'Slovak', the name every other game uses.  Returns one clean language name per row.
CREATE FUNCTION core.split_languages(entry text) RETURNS SETOF text
LANGUAGE sql IMMUTABLE AS $$
    WITH decoded AS (
        -- undo the escaping before splitting, because every entity ends in ';'
        SELECT regexp_replace(
                   replace(replace(regexp_replace(entry, '&(amp;)+', '&', 'g'),
                                   '&lt;', '<'), '&gt;', '>'),
                   '&#?[a-z0-9]+;', ' ', 'gi') AS txt
    )
    SELECT DISTINCT CASE c.lang WHEN 'lang_slovakian' THEN 'Slovak' ELSE c.lang END
    FROM decoded
    CROSS JOIN LATERAL regexp_split_to_table(
        decoded.txt, ',|;|(\r|\n)+|(\\[rn])+|<br\s*/?>', 'i') AS p(part)
    CROSS JOIN LATERAL (
        SELECT btrim(
                   regexp_replace(
                       regexp_replace(p.part, '\\[rnt]|<[^>]*>|\[/?[a-z]+\]|[*#]', ' ', 'gi'),
                       '\s+', ' ', 'g'),
                   ' ''".') AS lang
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

COMMENT ON TABLE core.games IS 'One row per Steam app. Long text and URLs stay in public.games_json.';


-- --- lookup tables and their links to games --------------------------------------
-- position = place of the value in the source list (1 = first).

CREATE TABLE core.tags (
    tag_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name    text NOT NULL UNIQUE
);

CREATE TABLE core.game_tags (
    appid     bigint  NOT NULL REFERENCES core.games (appid) ON DELETE CASCADE,
    tag_id    integer NOT NULL REFERENCES core.tags (tag_id),
    position  integer NOT NULL,   -- rank by votes, 1 = most voted (jsonb keeps no key order)
    votes     integer,            -- players who applied the tag
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
    name          text NOT NULL
);

-- one row per name whatever the capitalisation; 02_populate.sql keeps the most common spelling
CREATE UNIQUE INDEX developers_name_lower_key ON core.developers (lower(name));

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
