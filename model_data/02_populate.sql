-- Fill the core schema from public.games_raw.  Runs after 01_schema.sql, same transaction.

-- --- games -----------------------------------------------------------------------

INSERT INTO core.games (
    appid, name, release_date, release_date_raw, estimated_owners, owners_min, owners_max,
    price, discount, dlc_count, required_age, windows, mac, linux,
    metacritic_score, user_score, positive, negative, recommendations, achievements, peak_ccu,
    average_playtime_forever, average_playtime_two_weeks,
    median_playtime_forever, median_playtime_two_weeks
)
SELECT
    r.appid::bigint,
    NULLIF(btrim(r.name), ''),
    CASE
        WHEN r.release_date ~ '^[A-Za-z]{3} [0-9]{1,2}, [0-9]{4}$' THEN core.try_date(r.release_date, 'Mon DD, YYYY')
        WHEN r.release_date ~ '^[A-Za-z]{3} [0-9]{4}$'             THEN core.try_date(r.release_date, 'Mon YYYY')
    END,
    r.release_date,
    r.estimated_owners,
    CASE WHEN r.estimated_owners ~ '^[0-9]+ - [0-9]+$' THEN split_part(r.estimated_owners, ' - ', 1)::bigint END,
    CASE WHEN r.estimated_owners ~ '^[0-9]+ - [0-9]+$' THEN split_part(r.estimated_owners, ' - ', 2)::bigint END,
    r.price::numeric(10, 2),
    r.discount::integer,
    r.dlc_count::integer,
    r.required_age::integer,
    r.windows::boolean,
    r.mac::boolean,
    r.linux::boolean,
    NULLIF(r.metacritic_score::integer, 0),
    r.user_score::integer,
    r.positive::integer,
    r.negative::integer,
    r.recommendations::integer,
    r.achievements::integer,
    r.peak_ccu::integer,
    r.average_playtime_forever::integer,
    r.average_playtime_two_weeks::integer,
    r.median_playtime_forever::integer,
    r.median_playtime_two_weeks::integer
FROM public.games_raw AS r;


-- --- tags: 'Indie,Casual,Sports' -> one row per tag ------------------------------

INSERT INTO core.tags (name)
SELECT DISTINCT btrim(t.tag)
FROM public.games_raw AS r
CROSS JOIN LATERAL unnest(string_to_array(r.tags, ',')) AS t(tag)
WHERE btrim(t.tag) <> ''
ORDER BY 1;

INSERT INTO core.game_tags (appid, tag_id, position)
SELECT r.appid, tg.tag_id, min(t.ord)::integer
FROM public.games_raw AS r
CROSS JOIN LATERAL unnest(string_to_array(r.tags, ',')) WITH ORDINALITY AS t(tag, ord)
JOIN core.tags AS tg ON tg.name = btrim(t.tag)
GROUP BY r.appid, tg.tag_id;


-- --- genres ----------------------------------------------------------------------

INSERT INTO core.genres (name)
SELECT DISTINCT btrim(g.genre)
FROM public.games_raw AS r
CROSS JOIN LATERAL unnest(string_to_array(r.genres, ',')) AS g(genre)
WHERE btrim(g.genre) <> ''
ORDER BY 1;

INSERT INTO core.game_genres (appid, genre_id, position)
SELECT r.appid, gn.genre_id, min(g.ord)::integer
FROM public.games_raw AS r
CROSS JOIN LATERAL unnest(string_to_array(r.genres, ',')) WITH ORDINALITY AS g(genre, ord)
JOIN core.genres AS gn ON gn.name = btrim(g.genre)
GROUP BY r.appid, gn.genre_id;


-- --- developers ------------------------------------------------------------------
-- core.split_developers keeps company names whole (see 01_schema.sql).  It is a
-- heuristic: qa_data reports the fragments that still slip through.  Spellings that
-- differ only in capitalisation ('1UP games', '1UP Games') become one developer,
-- named by the spelling used by the most games.

CREATE TEMP TABLE developer_mentions AS
SELECT r.appid, d.developer, d.ord
FROM public.games_raw AS r
CROSS JOIN LATERAL core.split_developers(r.developers) AS d;

INSERT INTO core.developers (name)
SELECT DISTINCT ON (lower(developer)) developer
FROM developer_mentions
GROUP BY developer
ORDER BY lower(developer), count(*) DESC, developer;

INSERT INTO core.game_developers (appid, developer_id, position)
SELECT m.appid, dv.developer_id, min(m.ord)::integer
FROM developer_mentions AS m
JOIN core.developers AS dv ON lower(dv.name) = lower(m.developer)
GROUP BY m.appid, dv.developer_id;

DROP TABLE developer_mentions;


-- --- languages: interface/subtitle list plus the full-audio list ------------------

INSERT INTO core.languages (name)
SELECT l.language
FROM public.games_raw AS r
CROSS JOIN LATERAL core.split_languages(r.supported_languages) AS l(language)
UNION
SELECT l.language
FROM public.games_raw AS r
CROSS JOIN LATERAL core.split_languages(r.full_audio_languages) AS l(language)
ORDER BY 1;

INSERT INTO core.game_languages (appid, language_id, full_audio)
SELECT u.appid, lg.language_id, bool_or(u.is_audio)
FROM (
    SELECT r.appid, l.language, false AS is_audio
    FROM public.games_raw AS r
    CROSS JOIN LATERAL core.split_languages(r.supported_languages) AS l(language)
    UNION ALL
    SELECT r.appid, l.language, true
    FROM public.games_raw AS r
    CROSS JOIN LATERAL core.split_languages(r.full_audio_languages) AS l(language)
) AS u
JOIN core.languages AS lg ON lg.name = u.language
GROUP BY u.appid, lg.language_id;


-- --- indexes for the reverse lookups, then fresh planner statistics ---------------

CREATE INDEX game_tags_tag_id_idx             ON core.game_tags (tag_id);
CREATE INDEX game_genres_genre_id_idx         ON core.game_genres (genre_id);
CREATE INDEX game_developers_developer_id_idx ON core.game_developers (developer_id);
CREATE INDEX game_languages_language_id_idx   ON core.game_languages (language_id);
CREATE INDEX games_release_date_idx           ON core.games (release_date);

ANALYZE core.games;
ANALYZE core.tags;
ANALYZE core.game_tags;
ANALYZE core.genres;
ANALYZE core.game_genres;
ANALYZE core.developers;
ANALYZE core.game_developers;
ANALYZE core.languages;
ANALYZE core.game_languages;
