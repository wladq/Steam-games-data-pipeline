-- Fill the core schema from public.games_json.  Runs after 01_schema.sql, same transaction.
-- Every game is one jsonb object in j.data; lists are JSON arrays, so nothing has to be
-- split on commas.  The core.j* helpers in 01_schema.sql absorb keys whose type varies.

-- --- games -----------------------------------------------------------------------

INSERT INTO core.games (
    appid, name, release_date, release_date_raw, estimated_owners, owners_min, owners_max,
    price, discount, dlc_count, required_age, windows, mac, linux,
    metacritic_score, user_score, positive, negative, recommendations, achievements, peak_ccu,
    average_playtime_forever, average_playtime_two_weeks,
    median_playtime_forever, median_playtime_two_weeks
)
SELECT
    j.appid,
    NULLIF(btrim(j.data->>'name'), ''),
    core.parse_release_date(d.raw),
    d.raw,
    j.data->>'estimated_owners',
    CASE WHEN j.data->>'estimated_owners' ~ '^[0-9]+ - [0-9]+$'
         THEN split_part(j.data->>'estimated_owners', ' - ', 1)::bigint END,
    CASE WHEN j.data->>'estimated_owners' ~ '^[0-9]+ - [0-9]+$'
         THEN split_part(j.data->>'estimated_owners', ' - ', 2)::bigint END,
    core.jnum(j.data->'price')::numeric(10, 2),
    core.jnum(j.data->'discount')::integer,
    core.jnum(j.data->'dlc_count')::integer,
    core.jnum(j.data->'required_age')::integer,
    core.jbool(j.data->'windows'),
    core.jbool(j.data->'mac'),
    core.jbool(j.data->'linux'),
    NULLIF(core.jnum(j.data->'metacritic_score')::integer, 0),
    core.jnum(j.data->'user_score')::integer,
    core.jnum(j.data->'positive')::integer,
    core.jnum(j.data->'negative')::integer,
    core.jnum(j.data->'recommendations')::integer,
    core.jnum(j.data->'achievements')::integer,
    core.jnum(j.data->'peak_ccu')::integer,
    core.jnum(j.data->'average_playtime_forever')::integer,
    core.jnum(j.data->'average_playtime_2weeks')::integer,
    core.jnum(j.data->'median_playtime_forever')::integer,
    core.jnum(j.data->'median_playtime_2weeks')::integer
FROM public.games_json AS j
-- 'Oct 21, 2008' in this dataset; newer scrapes nest it as {"date": ..., "coming_soon": ...}
CROSS JOIN LATERAL (
    SELECT NULLIF(btrim(COALESCE(j.data->'release_date'->>'date', j.data->>'release_date')), '')
           AS raw
) AS d;


-- --- tags: {"Indie": 22, "Casual": 9} -> one row per tag, with its votes ----------
-- A game without tags has an empty array instead of an object.  A list of names is
-- accepted too (votes unknown).  jsonb keeps no key order, so position is the rank
-- by votes.

CREATE TEMP TABLE tag_mentions AS
SELECT appid, tag, max(votes) AS votes
FROM (
    SELECT j.appid, btrim(t.key) AS tag, core.jnum(t.value)::integer AS votes
    FROM public.games_json AS j
    CROSS JOIN LATERAL jsonb_each(
        CASE WHEN jsonb_typeof(j.data->'tags') = 'object' THEN j.data->'tags'
             ELSE '{}'::jsonb END) AS t(key, value)
    UNION ALL
    SELECT j.appid, btrim(t.tag), NULL
    FROM public.games_json AS j
    CROSS JOIN LATERAL jsonb_array_elements_text(core.jlist(j.data->'tags')) AS t(tag)
) AS m
WHERE tag <> ''
GROUP BY appid, tag;

INSERT INTO core.tags (name)
SELECT DISTINCT tag FROM tag_mentions ORDER BY 1;

INSERT INTO core.game_tags (appid, tag_id, position, votes)
SELECT m.appid, t.tag_id,
       row_number() OVER (PARTITION BY m.appid ORDER BY m.votes DESC NULLS LAST, m.tag)::integer,
       m.votes
FROM tag_mentions AS m
JOIN core.tags AS t ON t.name = m.tag;

DROP TABLE tag_mentions;


-- --- genres ----------------------------------------------------------------------

INSERT INTO core.genres (name)
SELECT DISTINCT btrim(g.genre)
FROM public.games_json AS j
CROSS JOIN LATERAL jsonb_array_elements_text(core.jlist(j.data->'genres')) AS g(genre)
WHERE btrim(g.genre) <> ''
ORDER BY 1;

INSERT INTO core.game_genres (appid, genre_id, position)
SELECT j.appid, gn.genre_id, min(g.ord)::integer
FROM public.games_json AS j
CROSS JOIN LATERAL jsonb_array_elements_text(core.jlist(j.data->'genres'))
    WITH ORDINALITY AS g(genre, ord)
JOIN core.genres AS gn ON gn.name = btrim(g.genre)
GROUP BY j.appid, gn.genre_id;


-- --- developers ------------------------------------------------------------------
-- The JSON lists each company as one array element, so 'CAPCOM Co., Ltd.' needs no
-- splitting heuristic.  Spellings that differ only in capitalisation ('1UP games',
-- '1UP Games') become one developer, named by the spelling used by the most games.

CREATE TEMP TABLE developer_mentions AS
SELECT j.appid, btrim(d.developer) AS developer, d.ord
FROM public.games_json AS j
CROSS JOIN LATERAL jsonb_array_elements_text(core.jlist(j.data->'developers'))
    WITH ORDINALITY AS d(developer, ord)
WHERE btrim(d.developer) <> '';

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

CREATE TEMP TABLE language_mentions AS
SELECT j.appid, l.language, false AS is_audio
FROM public.games_json AS j
CROSS JOIN LATERAL jsonb_array_elements_text(core.jlist(j.data->'supported_languages')) AS e(entry)
CROSS JOIN LATERAL core.split_languages(e.entry) AS l(language)
UNION ALL
SELECT j.appid, l.language, true
FROM public.games_json AS j
CROSS JOIN LATERAL jsonb_array_elements_text(core.jlist(j.data->'full_audio_languages')) AS e(entry)
CROSS JOIN LATERAL core.split_languages(e.entry) AS l(language);

INSERT INTO core.languages (name)
SELECT DISTINCT language FROM language_mentions ORDER BY 1;

INSERT INTO core.game_languages (appid, language_id, full_audio)
SELECT m.appid, lg.language_id, bool_or(m.is_audio)
FROM language_mentions AS m
JOIN core.languages AS lg ON lg.name = m.language
GROUP BY m.appid, lg.language_id;

DROP TABLE language_mentions;


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
