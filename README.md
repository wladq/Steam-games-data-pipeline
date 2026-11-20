# Steam-games-data-pipeline

From a messy Kaggle export to a checked PostgreSQL model.

This project loads the [Steam Games Dataset](https://www.kaggle.com/datasets/fronkongames/steam-games-dataset)
from Kaggle into PostgreSQL, turns it into a normalised model, and checks the result with
SQL constraints and queries. The interesting part is not the loading. It is everything that
was wrong with the data on the way in, and how each problem is now caught automatically.

## Architecture

```
  kaggle.com
      │  HTTPS (Kaggle API)
      ▼
┌─ Windows workstation ──────────┐                ┌─ DebianServ (Debian 13 server) ───┐
│ Python (conda environment)     │   SSH tunnel   │ PostgreSQL 17                     │
│  main.py       runs every part │                │                                   │
│  get_data/     download, load ─┼─ localhost ───►│  public.games_raw    CSV landing  │
│  model_data/   build the model │    :5433       │  public.games_json   JSON landing │
│  qa_data/      check the model │   encrypted    │  staging.game_*      from JSON    │
│  analyse_data/ look at a table │                │  core.*              the model    │
└────────────────────────────────┘                └───────────────────────────────────┘
```

- **DebianServ** is a home server running Debian 13 and PostgreSQL 17. The scripts never
  connect to it directly, only through the tunnel.
- **The tunnel** is plain SSH port forwarding. Local port 5433 leads to PostgreSQL on
  DebianServ, so every script simply connects to `localhost:5433` and the traffic is encrypted.
  5432 below is the PostgreSQL default; change it if the server uses another port.

  ```
  ssh -N -L 5433:localhost:5432 <user>@DebianServ
  ```

- **Nothing sensitive is stored in the repository.** The password is typed at a prompt or
  read from `PGPASSWORD`. A rejected login is retried up to three times with a fresh prompt
  (once with `PGPASSWORD`), and no password is asked at all when nothing answers at the
  host and port, for example when the tunnel is down. User, host and database name live in a git-ignored
  `db_config.json`. Downloads go to a git-ignored `tmp/` folder.

## How to run it

Needs Python 3.10 or newer (developed on 3.15) and PostgreSQL 17.

```
pip install -r requirements.txt
kaggle auth login                      # once

python main.py                         # asks which part to run, then everything else
python main.py get                     # games.csv  -> public.games_raw
python main.py get json                # games.json -> public.games_json + staging.* tables
python main.py model                   # public.games_json -> core.* (normalised model)
python main.py qa                      # constraints + checks, prints a report
python main.py analyse                 # profile every table of core: keys, columns, stats, samples
python main.py dashboard               # snapshot to docs/: report.md and index.html
python main.py debug                   # unit tests in tests/, no database needed
```

The model is built from the JSON, so `get json` has to run before `model`. The CSV load is
optional: it feeds the checks that compare the two files.

`main.py` is the one entry point. Run it without arguments for a numbered menu of the parts
(type the number or the name), then answer that part's questions. `-m` (`--manual`) opens one
part's questions straight away, for example `python main.py analyse -m`; flags given next
to it are kept. With arguments, the part comes first, then its parameters. For `get` the order
is what (`csv` or `json`), where from (`-k OWNER/NAME` or `-l FILE`), where to (`-U -d -H -p`),
place (`--table`), and behaviour (`-y`, `-v`, `--log-dir`, `--dry-run`):

```
python main.py get csv -k fronkongames/steam-games-dataset -U <user> -d <database> --table games_raw -y
```

Anything left out comes from the saved `db_config.json` or is asked. `python main.py -h` and
`python main.py PART -h` list every option. The loader asks before each change to the data,
`-y` answers yes to every question, and `--dry-run` checks a file without touching the
database. The per-folder scripts (`python -m get_data`, `python model_data/build.py`,
`python qa_data/run_qa.py`, `python analyse_data/basic_check.py`) still work on their own.

`analyse` is the companion of `qa`: where `qa` checks rules, `analyse` describes what is
there. For every table of a schema (`core` by default, `--schema public` for the landing
tables, or one table with `--table core.games`) it logs the row count, size, keys, and per
column the share of filled rows, the number of distinct values and a summary that fits the
type (min, median, mean and max; yes and no shares; first and last date; text lengths),
followed by 3 random rows (`-n` changes the number). `--table` and `--sample-from` take
comma-separated lists, so `--table games,tags --sample-from games` profiles two tables and
shows rows from one. Run interactively (`python main.py`, then `analyse`), it first lists
the schemas and then the tables with their approximate size, and accepts names or numbers.
It only reads, computes everything in the database, and writes the same anonymised logs
as `qa`.

## Data model

Two landing tables keep the data as it arrived. The model in the `core` schema is rebuilt
from the JSON landing table `public.games_json` by plain SQL files, in one transaction.

| Table | One row per | Notes |
|---|---|---|
| `core.games` | Steam app | typed columns: parsed release date, owner range as two numbers, prices, review counts |
| `core.tags`, `core.genres`, `core.developers`, `core.languages` | distinct name | lookup tables with generated ids |
| `core.game_tags` | game and tag | `votes` = players who applied the tag; `position` = rank by votes |
| `core.game_genres`, `core.game_developers` | game and name | link tables; `position` is the place in the source list |
| `core.game_languages` | game and language | `full_audio` says whether the language is also dubbed |

Lists come from JSON arrays (`jsonb_array_elements_text ... WITH ORDINALITY`), so nothing is
split on commas and `CAPCOM Co., Ltd.` arrives as one company. Tags come from the
`{"Indie": 22, ...}` object with their vote counts. Because jsonb keeps no key order, a
tag's position is its rank by votes. Keys whose JSON type varies between games, such as
`discount` (string or number) or `tags` (object or empty array), go through small helper
functions that turn every shape into one SQL type. Spellings of a developer that differ only
in capitalisation (`1UP games`, `1UP Games`) become one developer under the most common
spelling. Language entries carry markup leftovers and get their own cleaning function.

The JSON load also builds a wider set of plain tables in the `staging` schema
(`staging.games`, `staging.game_tags`, `staging.game_packages` with prices per edition, and
more). They are a quick, uncleaned view of the JSON, independent of `core`, and their own
schema keeps `staging.games` and `core.games` from being confused. Version 0.6.0 and
earlier built them in `public`; the loader offers to drop those old copies after the next
build.

## Dashboard

[`docs/report.md`](docs/report.md) is a snapshot of the model that GitHub renders in the repo:
headline numbers, the data-quality state with every check, and tables for prices, release
years, platforms, tags, genres, languages and review concentration.
[`docs/index.html`](docs/index.html) shows the same numbers as interactive charts. Open it from
a local clone, or through GitHub Pages once the repository is public.

`python main.py dashboard` rebuilds both. It only reads: it runs the SELECTs in
`dashboard/queries.sql` and the QA checks, and writes `docs/report.md`, `docs/index.html` and
`docs/data/dashboard.json`. The database stays behind the tunnel, so the pages are a snapshot
that is refreshed by rerunning the command and committing the result. No connection details
are written to them.

## The data-quality story

Numbers in this section come from the load logs and the QA runs on the snapshot downloaded
in November 2026: the first run on a model built from the CSV, the current one on the JSON.

1. **The CSV header is broken.** Two column names are glued together as `DiscountDLC count`,
   so the file has 39 names for 40 columns and every column after that point is shifted by
   one. The loader detects the glued name, splits it, and then proves the repair worked:
   every `discount` must be a number from 0 to 100, which fails immediately if columns are
   still shifted. Result: 125,855 rows, 40 columns, every appid numeric and unique.
2. **The two files on the same Kaggle page do not match.** The CSV holds 125,855 games, the
   JSON 148,909. Every CSV game is also in the JSON, so the CSV is a smaller subset. The core
   model is built from the JSON and covers all 148,909 games. A check keeps watching for CSV
   games that the JSON does not know.
3. **The CSV loses information.** It is generated from the JSON by joining lists with
   commas. Tag vote counts and per-edition prices are dropped, and a company name with a
   comma in it becomes two developers. This is why the core model is built from the JSON:
   tag votes are kept and every company arrives as its own list entry.
4. **The JSON changes type from game to game.** `tags` is an object for 83,390 games and an
   empty array for 65,519. `discount` is a string for 103,249 games and a number for 45,660.
   `score_rank` is a string except for 40 numbers. The loader prints this inventory for
   every key, and the SQL that builds the tables handles each shape explicitly.
5. **Some fields are present but empty.** Every game has a `movies` array, yet none holds
   an entry in this snapshot, so `staging.game_movies` has 0 rows.
6. **Missing values are disguised.** A Metacritic score of 0 means "no score", so the model
   stores NULL. Owner counts are text ranges such as `20000 - 50000`, stored as two numbers.
   Release dates are text in two formats, `Oct 21, 2008` and `Oct 2008`. Unparseable dates
   become NULL and the raw text is kept next to them.
7. **The CSV reader could erase real names.** By default pandas treats words such as
   `None`, `NA` and `null` as missing values, so a game with such a name would load as
   NULL. The loader now counts only empty cells as missing, and a check compares the CSV
   load with the JSON load to catch any name that still goes missing. In this snapshot no
   name was lost. The one game without a name (appid 396420) has no name in the JSON either.
8. **PostgreSQL cannot store NUL characters.** The JSON loader strips them and reports the
   count. This snapshot contained none.
9. **The first QA run caught two bugs in the model's own cleaning.** The first model was
   built from the CSV. Some language lists are HTML-escaped several times over
   (`English&amp;amp;amp;amp;lt;strong…`), and some use line breaks or semicolons instead of
   commas (`English\r\nRussian`, `German;`), which left 14 broken language names. Splitting
   `Name,Inc.` at the comma produced 13 developers that are only a suffix, such as `inc.`
   with 42 games. The language cleaning was fixed and now runs on each entry of the JSON
   lists, which carry the same junk. The developer problem disappeared with the move to the
   JSON. Steam's placeholder `#lang_slovakian` is mapped to `Slovak`.
10. **Some errors stay in the data on purpose.** The JSON lists `LIMITED` as one game's only
    developer, and two language entries read `Japanese (all with full audio support)` and
    `Traditional Chinese (text only)`. Nine games, all only in the JSON, list none of
    Windows, Mac and Linux. All of them are kept as published. The QA checks report them as
    warnings, and the unit tests use the names as known errors that the checks must keep
    flagging.
11. **The JSON model reconciles with an independent build.** `core.game_tags` has 1,180,708
    rows, exactly as many as `staging.game_tags`, which the JSON loader builds with
    different SQL. Every game with tags or developers in the JSON has them linked, and every
    tag kept its vote count.

### How quality is checked

The same question, "is this data right?", is answered at four points.

| Where | How | What it is good for |
|---|---|---|
| Offline, without a database | `tests/`: unit tests, run with `python main.py debug` | the command line, the loader steps, the SQL files, the dashboard export, and the QA rules tested against known errors |
| Before loading, in Python | sanity checks in the loader | stops a broken file before it replaces a good table |
| In the database, permanently | `qa_data/01_constraints.sql`: CHECK constraints, plus keys and foreign keys in the schema | rules that must hold for every row, forever; the database refuses bad writes |
| In the database, on demand | `qa_data/02_checks.sql`: 29 queries that return the rows breaking a rule | counting problems instead of refusing them, rules across tables, rules that depend on today's date |

Constraints are added as `NOT VALID` and then validated. A rule that existing rows break is
reported as a finding, and it still protects every future write. The runner recognises a
violation by its error code (SQLSTATE 23514), not by the message text, so it works whatever
language the server writes its messages in. A rule that the source data
itself breaks is a warning check instead of a constraint: the one game without a name and the
9 games without a platform would otherwise fail every run, and a run that always fails says
nothing. Each check has a severity:
`error` fails the run, `warn` is only reported. `python main.py qa` prints one line
per constraint and per check and exits with code 1 if anything of severity `error` is wrong.

## Known limitations

- **Owner counts are estimates.** `estimated_owners` comes from SteamSpy, not from Valve. It
  is published as a wide range, and the model keeps both edges as `owners_min` and
  `owners_max`. Do not read the midpoint as a sales figure. The checks find 11 games with
  more reviews than the upper edge of their range allows, for example Call of Duty®: Black
  Ops III with 62,347 reviews in the `0 - 20000` range.
- **The data is a snapshot.** Prices, discounts, peak concurrent users and two-week playtime
  are the values on the day of scraping. There is no history, so nothing here can show a
  trend over time, and a rebuild from a newer download can change every number. 39 games
  show a price of 0 together with a discount, mostly 100 %: they were scraped while
  temporarily free.
- **Popular games are over-represented in reviews.** Review counts are extremely skewed: a
  small number of hits collect most of the reviews, so any statistic weighted by reviews
  mostly describes blockbusters. Tags behave the same way, because a game only gets tags
  once enough players vote: 65,519 of the 148,909 games in the JSON have none. Compare
  medians or group games by size before drawing conclusions. This query measures the skew:

  ```sql
  WITH ranked AS (
      SELECT positive + negative AS reviews,
             ntile(100) OVER (ORDER BY positive + negative DESC) AS percentile
      FROM core.games
  )
  SELECT round(100.0 * sum(reviews) FILTER (WHERE percentile = 1) / sum(reviews), 1)
             AS percent_of_all_reviews_held_by_top_1_percent
  FROM ranked;
  ```

- **Lists can be empty in the source.** 8,457 games have no developer, 8,437 no genre and
  8,402 no language. The samples are older titles and playtests.
- **Developers are merged by capitalisation.** The model assumes that `1UP games` and
  `1UP Games` are the same company.
- **Tag positions are ranks by votes**, not the order Steam shows, because jsonb does not
  keep the key order of an object.
- **Dates given as month and year** are stored as the first day of that month.
- **Log anonymisation is pattern based.** It is a safety net, not a guarantee.

## Logs and privacy

Each run writes two files to the git-ignored `logs/` folder: `info.log` with the readable
story of the run, and `debug.log` with debug detail, warnings, errors and tracebacks. Before
anything reaches a file, the operating-system user, home folder, computer name, database
user, host and name, e-mail addresses and IP addresses are replaced with placeholders such as
`<db-user>`. Extra words can be hidden with the `GET_DATA_REDACT` environment variable,
comma separated. The console is not anonymised.

## Licence

Copyright (C) 2026 Wladyslaw.

This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License version 3 as published by the Free Software Foundation. It is
distributed in the hope that it will be useful, but without any warranty; see
[LICENSE](LICENSE) for the full text.

The licence covers the code in this repository. The data comes from the
[Steam Games Dataset](https://www.kaggle.com/datasets/fronkongames/steam-games-dataset) by
FronkonGames on Kaggle, published under the MIT License, which is compatible with the GPL.
The dataset is not stored in this repository; `docs/` only publishes numbers derived from it.

## Project layout

```
get_data/        loader package: prompts, config, Kaggle download, CSV and JSON loading, logging
model_data/      SQL that builds the core schema, and build.py to run it
qa_data/         SQL constraints and checks, and run_qa.py to run them
analyse_data/    basic_check.py: profile a schema's tables (keys, fill, distinct values, stats, samples)
dashboard/       queries.sql, export.py and the chart template; output goes to docs/
docs/            the generated report.md, index.html and data/dashboard.json
tests/           unit tests (main.py, loader, SQL files, QA rules), run with python main.py debug
main.py          one entry point: get, model, qa, analyse, dashboard, debug
pyproject.toml   name, version (read from get_data/__init__.py), dependencies, licence
LICENSE          GNU General Public License v3
```
