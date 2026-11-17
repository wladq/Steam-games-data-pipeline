-- Data-quality checks as queries.
--
-- Convention: every query returns the rows that BREAK the rule, so an empty result
-- means the check passes.  Run one by hand to see the offending rows, or run them
-- all with:  python qa_data/run_qa.py
--
-- Header lines read by the runner:
--   check:     unique name
--   severity:  error = must be zero (the run fails), warn = reported only
--   requires:  optional table; the check is skipped when it does not exist
--   about:     one line shown in the report


-- === 1. Reconciliation: did everything arrive? ====================================

-- check: landing_and_core_row_counts_match
-- severity: error
-- about: core.games must have exactly as many rows as public.games_raw
SELECT r.n AS landing_rows, g.n AS core_rows
FROM (SELECT count(*) AS n FROM public.games_raw) AS r
CROSS JOIN (SELECT count(*) AS n FROM core.games) AS g
WHERE r.n <> g.n;

-- check: games_with_tags_all_linked
-- severity: error
-- about: a game whose raw tags text is not empty must have rows in core.game_tags
SELECT r.appid, r.tags
FROM public.games_raw AS r
WHERE btrim(coalesce(r.tags, '')) <> ''
  AND NOT EXISTS (SELECT 1 FROM core.game_tags AS gt WHERE gt.appid = r.appid);

-- check: csv_games_missing_from_json
-- severity: warn
-- requires: public.games_json
-- about: games in the CSV snapshot that the JSON snapshot does not contain
SELECT g.appid, g.name
FROM core.games AS g
WHERE NOT EXISTS (SELECT 1 FROM public.games_json AS j WHERE j.appid = g.appid);

-- check: names_lost_by_csv_reader
-- severity: warn
-- requires: public.games_json
-- about: name is NULL in the CSV load although the JSON has one (pandas reads 'None', 'NA', 'null' as missing)
SELECT g.appid, j.data->>'name' AS json_name
FROM core.games AS g
JOIN public.games_json AS j ON j.appid = g.appid
WHERE g.name IS NULL
  AND btrim(coalesce(j.data->>'name', '')) <> '';


-- === 2. Completeness: what is missing? =============================================

-- check: game_name_missing
-- severity: warn
-- about: games without a name (a warning, not a constraint: the source has one such game)
SELECT appid FROM core.games WHERE name IS NULL;

-- check: release_date_not_parsed
-- severity: warn
-- about: release date text that is neither 'Mon DD, YYYY' nor 'Mon YYYY'
SELECT appid, name, release_date_raw
FROM core.games
WHERE release_date IS NULL
  AND btrim(coalesce(release_date_raw, '')) <> '';

-- check: owner_bucket_not_parsed
-- severity: warn
-- about: estimated_owners text that is not a 'low - high' range
SELECT appid, name, estimated_owners
FROM core.games
WHERE owners_min IS NULL;

-- check: game_without_developer
-- severity: warn
-- about: games with no developer
SELECT g.appid, g.name
FROM core.games AS g
WHERE NOT EXISTS (SELECT 1 FROM core.game_developers AS x WHERE x.appid = g.appid);

-- check: game_without_genre
-- severity: warn
-- about: games with no genre
SELECT g.appid, g.name
FROM core.games AS g
WHERE NOT EXISTS (SELECT 1 FROM core.game_genres AS x WHERE x.appid = g.appid);

-- check: game_without_tags
-- severity: warn
-- about: games with no user tags (tags only appear once enough players vote)
SELECT g.appid, g.name
FROM core.games AS g
WHERE NOT EXISTS (SELECT 1 FROM core.game_tags AS x WHERE x.appid = g.appid);

-- check: game_without_language
-- severity: warn
-- about: games with no supported language
SELECT g.appid, g.name
FROM core.games AS g
WHERE NOT EXISTS (SELECT 1 FROM core.game_languages AS x WHERE x.appid = g.appid);


-- === 3. Validity: are single values possible? ======================================

-- check: price_negative
-- severity: error
-- about: prices below zero
SELECT appid, name, price FROM core.games WHERE price < 0;

-- check: discount_out_of_range
-- severity: error
-- about: discounts outside 0-100 percent (a sign of shifted columns)
SELECT appid, name, discount FROM core.games WHERE discount NOT BETWEEN 0 AND 100;

-- check: owner_range_reversed
-- severity: error
-- about: owner buckets whose lower edge is above the upper edge
SELECT appid, name, estimated_owners FROM core.games WHERE owners_min > owners_max;

-- check: review_counts_negative
-- severity: error
-- about: negative review or recommendation counts
SELECT appid, name, positive, negative, recommendations
FROM core.games
WHERE positive < 0 OR negative < 0 OR recommendations < 0;

-- check: metacritic_out_of_range
-- severity: error
-- about: Metacritic scores outside 1-100
SELECT appid, name, metacritic_score FROM core.games WHERE metacritic_score NOT BETWEEN 1 AND 100;

-- check: required_age_out_of_range
-- severity: warn
-- about: age ratings outside 0-21
SELECT appid, name, required_age FROM core.games WHERE required_age NOT BETWEEN 0 AND 21;

-- check: release_date_in_future
-- severity: warn
-- about: release dates after today (unreleased games, or a wrong date)
SELECT appid, name, release_date FROM core.games WHERE release_date > CURRENT_DATE;

-- check: no_platform
-- severity: warn
-- about: games flagged for none of Windows, Mac, Linux
SELECT appid, name FROM core.games WHERE NOT (windows OR mac OR linux);


-- === 4. Consistency: do values agree with each other? ==============================

-- check: free_game_with_discount
-- severity: warn
-- about: price is 0 but a discount is set
SELECT appid, name, price, discount FROM core.games WHERE price = 0 AND discount > 0;

-- check: two_week_playtime_too_long
-- severity: warn
-- about: average playtime in the last two weeks above 20160 minutes, which is two full weeks
SELECT appid, name, average_playtime_two_weeks
FROM core.games
WHERE average_playtime_two_weeks > 20160;

-- check: reviews_but_zero_owner_bucket
-- severity: warn
-- about: games with reviews whose owner estimate is the '0 - 0' bucket (owner counts are estimates)
SELECT appid, name, positive + negative AS reviews, estimated_owners
FROM core.games
WHERE owners_max = 0 AND positive + negative > 0;

-- check: more_reviews_than_max_owners
-- severity: warn
-- about: more reviews than the upper edge of the owner estimate allows
SELECT appid, name, positive + negative AS reviews, estimated_owners
FROM core.games
WHERE owners_max > 0 AND positive + negative > owners_max;


-- === 5. Lookup hygiene: are the split lists clean? =================================

-- check: tag_names_differ_only_by_case
-- severity: warn
-- about: tags that are the same word in different capitalisation
SELECT lower(name) AS tag, count(*) AS variants, string_agg(name, ' | ') AS names
FROM core.tags
GROUP BY lower(name)
HAVING count(*) > 1;

-- check: developer_names_differ_only_by_case
-- severity: warn
-- about: developers that are the same name in different capitalisation (the model merges them)
SELECT lower(name) AS developer, count(*) AS variants, string_agg(name, ' | ') AS names
FROM core.developers
GROUP BY lower(name)
HAVING count(*) > 1;

-- check: developer_name_is_company_suffix
-- severity: warn
-- about: developer rows that are only a company suffix, left over from splitting 'Name,Inc.'
SELECT d.developer_id, d.name, count(gd.appid) AS games
FROM core.developers AS d
LEFT JOIN core.game_developers AS gd ON gd.developer_id = d.developer_id
WHERE d.name ~* '^(inc|incorporated|ltd|limited|llc|l\.l\.c|co|corp|corporation|gmbh|s\.a|s\.r\.o|srl|pty|plc)\.?$'
GROUP BY d.developer_id, d.name;

-- check: language_name_suspicious
-- severity: warn
-- about: language names that still contain markup, digits or are very long
-- (letters, spaces, brackets, hyphens and apostrophes are fine: 'Spanish - Spain', 'K''iche')
SELECT l.language_id, l.name, count(gl.appid) AS games
FROM core.languages AS l
LEFT JOIN core.game_languages AS gl ON gl.language_id = l.language_id
WHERE l.name !~ '^[A-Za-z][A-Za-z ()''-]*$' OR length(l.name) > 30
GROUP BY l.language_id, l.name;
