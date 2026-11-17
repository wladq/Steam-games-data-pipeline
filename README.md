# Steam games: from a messy Kaggle export to a checked PostgreSQL model

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
│                                │                │                                   │
│  get_data/     download, load ─┼─ localhost ───►│  public.games_raw    CSV landing  │
│  model_data/   build the model │    :5433       │  public.games_json   JSON landing │
│  qa_data/      check the model │   encrypted    │  public.game_*       from JSON    │
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
  read from `PGPASSWORD`. User, host and database name live in a git-ignored
  `db_config.json`. Downloads go to a git-ignored `tmp/` folder.

## How to run it

```
pip install -r requirements.txt
kaggle auth login                      # once

python load_db.py                      # games.csv  -> public.games_raw
python load_db.py --json               # games.json -> public.games_json + public.game_* tables
python model_data/build.py             # public.games_raw -> core.* (normalised model)
python qa_data/run_qa.py               # constraints + checks, prints a report
python analyse_data/basic_check.py     # quick look at a table: samples and basic stats
```

Every script takes `--help`. The loader asks before each change to the data, `-y` answers
yes to every question, and `--dry-run` checks a file without touching the database.

## Data model

Two landing tables keep the data as it arrived. The model in the `core` schema is rebuilt
from the CSV landing table by plain SQL files, in one transaction.

| Table | One row per | Notes |
|---|---|---|
| `core.games` | Steam app | typed columns: parsed release date, owner range as two numbers, prices, review counts |
| `core.tags`, `core.genres`, `core.developers`, `core.languages` | distinct name | lookup tables with generated ids |
| `core.game_tags`, `core.game_genres`, `core.game_developers` | game and name | link tables; `position` is the place in the source list |
| `core.game_languages` | game and language | `full_audio` says whether the language is also dubbed |

The lists are split in SQL. Tags and genres use `unnest(string_to_array(tags, ','))`.
Developers get a split function that only splits on commas followed neither by a space nor
by a company suffix, so `CAPCOM Co., Ltd.` and `Name,Inc.` each stay one company. Spellings
that differ only in capitalisation (`1UP games`, `1UP Games`) become one developer under the
most common spelling. Languages arrive as the text of a Python list with markup leftovers
and get their own cleaning function.

The JSON load builds a second, richer set of tables in `public` (`games`, `game_tags` with
vote counts, `game_packages` with prices per edition). They are independent of `core`.

## The data-quality story

Numbers in this section come from the load logs and the first QA run on the snapshot
downloaded in November 2026.

1. **The CSV header is broken.** Two column names are glued together as `DiscountDLC count`,
   so the file has 39 names for 40 columns and every column after that point is shifted by
   one. The loader detects the glued name, splits it, and then proves the repair worked:
   every `discount` must be a number from 0 to 100, which fails immediately if columns are
   still shifted. Result: 125,855 rows, 40 columns, every appid numeric and unique.
2. **The two files on the same Kaggle page do not match.** The CSV holds 125,855 games, the
   JSON 148,909. Every CSV game is also in the JSON, so the CSV is a smaller subset, and the
   core model, built from the CSV, lacks 23,054 games. A check keeps watching for CSV games
   that the JSON does not know.
3. **The CSV loses information.** It is generated from the JSON by joining lists with
   commas. Tag vote counts and per-edition prices are dropped, and a company name with a
   comma in it becomes two developers. The JSON load keeps everything. The CSV model works
   around the comma problem and reports what still slips through.
4. **The JSON changes type from game to game.** `tags` is an object for 83,390 games and an
   empty array for 65,519. `discount` is a string for 103,249 games and a number for 45,660.
   `score_rank` is a string except for 40 numbers. The loader prints this inventory for
   every key, and the SQL that builds the tables handles each shape explicitly.
5. **Some fields are present but empty.** Every game has a `movies` array, yet none holds
   an entry in this snapshot, so `public.game_movies` has 0 rows.
6. **Missing values are disguised.** A Metacritic score of 0 means "no score", so the model
   stores NULL. Owner counts are text ranges such as `20000 - 50000`, stored as two numbers.
   Release dates are text in two formats, `Oct 21, 2008` and `Oct 2008`. Unparseable dates
   become NULL and the raw text is kept next to them.
7. **The CSV reader could erase real names.** pandas treats words such as `None`, `NA` and
   `null` as missing values, so a game with such a name would load as NULL. A check
   compares the CSV load with the JSON load: in this snapshot no name was lost. The one
   game without a name (appid 396420) has no name in the JSON either.
8. **PostgreSQL cannot store NUL characters.** The JSON loader strips them and reports the
   count. This snapshot contained none.
9. **The first QA run caught two bugs in the model's own cleaning.** Some language lists
   are HTML-escaped several times over (`English&amp;amp;amp;amp;lt;strong…`), and some use
   line breaks or semicolons instead of commas (`English\r\nRussian`, `German;`), which left
   14 broken language names. Splitting `Name,Inc.` at the comma produced 13 developers that
   are only a suffix, such as `inc.` with 42 games. After fixing both cleaning functions the
   checks report 1 suffix developer (`LIMITED`) and a couple of odd language names taken
   from the source as written, such as `Japanese (all with full audio support)`. Steam's
   placeholder `#lang_slovakian` is mapped to `Slovak`. The same checks guard against the
   bugs coming back.

### How quality is checked

The same question, "is this data right?", is answered at three points.

| Where | How | What it is good for |
|---|---|---|
| Before loading, in Python | sanity checks in the loader | stops a broken file before it replaces a good table |
| In the database, permanently | `qa_data/01_constraints.sql`: CHECK constraints, plus keys and foreign keys in the schema | rules that must hold for every row, forever; the database refuses bad writes |
| In the database, on demand | `qa_data/02_checks.sql`: 27 queries that return the rows breaking a rule | counting problems instead of refusing them, rules across tables, rules that depend on today's date |

Constraints are added as `NOT VALID` and then validated. A rule that existing rows break is
reported as a finding, and it still protects every future write. A rule that the source data
itself breaks is a warning check instead of a constraint: the one game without a name would
otherwise fail every run, and a run that always fails says nothing. Each check has a severity:
`error` fails the run, `warn` is only reported. `python qa_data/run_qa.py` prints one line
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
  once enough players vote: 65,519 of the 148,909 games in the JSON have none, and 42,502 of
  the 125,855 in the core model. Compare
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

- **The core model is built from the CSV**, so it has the smaller snapshot and no tag votes.
- **Lists can be empty in the source.** 8,449 games have no developer, 8,423 no genre and
  8,391 no language. The samples are mostly editors, dedicated servers and older titles.
- **Developer splitting is a heuristic.** A name with a comma and no space after it is still
  split in two unless a company suffix follows, and merging by capitalisation assumes that
  `1UP games` and `1UP Games` are the same company. `qa_data` reports the leftovers.
- **Dates given as month and year** are stored as the first day of that month.
- **Log anonymisation is pattern based.** It is a safety net, not a guarantee.

## Logs and privacy

Each run writes two files to the git-ignored `logs/` folder: `info.log` with the readable
story of the run, and `debug.log` with debug detail, warnings, errors and tracebacks. Before
anything reaches a file, the operating-system user, home folder, computer name, database
user, host and name, e-mail addresses and IP addresses are replaced with placeholders such as
`<db-user>`. Extra words can be hidden with the `GET_DATA_REDACT` environment variable,
comma separated. The console is not anonymised.

## Project layout

```
get_data/        loader package: prompts, config, Kaggle download, CSV and JSON loading, logging
model_data/      SQL that builds the core schema, and build.py to run it
qa_data/         SQL constraints and checks, and run_qa.py to run them
analyse_data/    basic_check.py: sample rows and basic statistics of a table
tests/           check_cleaning.py: offline check of the SQL files and the cleaning patterns
load_db.py       entry point for the loader
```
