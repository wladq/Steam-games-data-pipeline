-- Read-only queries behind the dashboard.  Each returns the rows of one section.
--
-- Run : python main.py dashboard   (writes docs/report.md, docs/index.html, docs/data/)
--
-- Header lines read by dashboard/export.py:
--   query:  unique name, used as the key in docs/data/dashboard.json
--   about:  one line describing the result


-- query: totals
-- about: headline numbers of the core model
SELECT
    (SELECT count(*) FROM core.games)                           AS games,
    (SELECT count(*) FROM core.developers)                      AS developers,
    (SELECT count(*) FROM core.tags)                            AS tags,
    (SELECT count(*) FROM core.genres)                          AS genres,
    (SELECT count(*) FROM core.languages)                       AS languages,
    (SELECT count(*) FROM core.games WHERE price = 0)           AS free_games,
    (SELECT count(*) FROM core.games WHERE price > 0)           AS paid_games,
    (SELECT sum(positive + negative) FROM core.games)           AS reviews,
    (SELECT min(release_date) FROM core.games)                  AS first_release,
    (SELECT max(release_date) FROM core.games
      WHERE release_date <= CURRENT_DATE)                       AS last_release;

-- query: releases_per_year
-- about: games released per year (the last year is partial: the data is a snapshot)
SELECT extract(year FROM release_date)::int AS year, count(*) AS games
FROM core.games
WHERE release_date IS NOT NULL AND release_date <= CURRENT_DATE
GROUP BY 1
ORDER BY 1;

-- query: price_bands
-- about: games per price band, in USD at scrape time
WITH bands (sort, band, low, high) AS (
    VALUES (1, 'Free', 0, 0.005), (2, 'Under $5', 0.005, 5), (3, '$5-10', 5, 10),
           (4, '$10-20', 10, 20), (5, '$20-40', 20, 40), (6, '$40-60', 40, 60),
           (7, '$60 and more', 60, 1e9)
)
SELECT b.band, count(g.appid) AS games
FROM bands AS b
LEFT JOIN core.games AS g ON g.price >= b.low AND g.price < b.high
GROUP BY b.sort, b.band
ORDER BY b.sort;

-- query: platforms
-- about: share of games that run on each platform (a game can run on several)
SELECT p.platform, p.games, round(100.0 * p.games / t.total, 1) AS percent
FROM (
    SELECT 1 AS sort, 'Windows' AS platform, count(*) FILTER (WHERE windows) AS games
    FROM core.games
    UNION ALL
    SELECT 2, 'Mac', count(*) FILTER (WHERE mac) FROM core.games
    UNION ALL
    SELECT 3, 'Linux', count(*) FILTER (WHERE linux) FROM core.games
) AS p
CROSS JOIN (SELECT count(*) AS total FROM core.games) AS t
ORDER BY p.sort;

-- query: top_tags
-- about: the 15 user tags with the most votes, summed over all games
SELECT t.name AS tag, sum(gt.votes) AS votes, count(*) AS games
FROM core.game_tags AS gt
JOIN core.tags AS t ON t.tag_id = gt.tag_id
GROUP BY t.name
ORDER BY votes DESC NULLS LAST, t.name
LIMIT 15;

-- query: top_genres
-- about: the 15 genres with the most games
SELECT g.name AS genre, count(*) AS games
FROM core.game_genres AS gg
JOIN core.genres AS g ON g.genre_id = gg.genre_id
GROUP BY g.name
ORDER BY games DESC, g.name
LIMIT 15;

-- query: top_languages
-- about: the 15 languages supported by the most games, and how many of those are dubbed
SELECT l.name AS language, count(*) AS games,
       count(*) FILTER (WHERE gl.full_audio) AS full_audio
FROM core.game_languages AS gl
JOIN core.languages AS l ON l.language_id = gl.language_id
GROUP BY l.name
ORDER BY games DESC, l.name
LIMIT 15;

-- query: review_concentration
-- about: share of all reviews held by the top N percent of games, ranked by review count
WITH ranked AS (
    SELECT positive + negative AS reviews,
           ntile(100) OVER (ORDER BY positive + negative DESC) AS percentile
    FROM core.games
    WHERE positive IS NOT NULL AND negative IS NOT NULL
), per_percentile AS (
    SELECT percentile, sum(reviews) AS reviews
    FROM ranked
    GROUP BY percentile
)
SELECT percentile AS top_percent_of_games,
       round(100.0 * sum(reviews) OVER (ORDER BY percentile)
             / NULLIF(sum(reviews) OVER (), 0), 2) AS percent_of_reviews
FROM per_percentile
ORDER BY percentile;

-- query: constraints
-- about: the CHECK constraints on the core tables and whether existing rows satisfy them
SELECT conrelid::regclass::text AS table_name, conname AS constraint_name,
       convalidated AS valid
FROM pg_constraint
WHERE connamespace = 'core'::regnamespace AND contype = 'c'
ORDER BY conname;
