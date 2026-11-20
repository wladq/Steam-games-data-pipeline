"""Load games.json into a jsonb landing table, then build normalised tables in SQL.

games.json is one object keyed by appid::

    {"20200": {"name": "Galactic Bowling", "tags": {"Indie": 22, ...}, ...}, ...}

Landing table (default ``games_json``)::

    appid      bigint primary key
    data       jsonb  not null          -- the game object, untouched apart from NUL bytes
    loaded_at  timestamptz default now()
    + GIN index on data, so ->, ->>, @>, ?, jsonpath (@?, @@) all use the index

Normalised tables in schema ``staging``, rebuilt from the landing table with plain
SQL (see NORMALISED_TABLES): ``staging.games`` (one typed row per game) and
``staging.game_*`` child tables for every list or object field.  They are a quick,
wide view of the JSON; the cleaned model is the ``core`` schema built by model_data/.
Keys that are not mapped to a column stay queryable in ``games_json.data``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from sqlalchemy import Engine

log = logging.getLogger(__name__)


def text(sql: str):
    """sqlalchemy.text, imported on first use: SQLAlchemy takes half a second to load,
    which `python main.py --help` should not pay just because this module is imported."""
    from sqlalchemy import text as sa_text  # pylint: disable=import-outside-toplevel
    return sa_text(sql)

LANDING_TABLE = "games_json"
STAGING_SCHEMA = "staging"
LEGACY_SCHEMA = "public"  # where versions before 0.7 built the same tables
NORMALISED_TABLES = ("games", "game_developers", "game_publishers", "game_genres", "game_categories",
                     "game_tags", "game_languages", "game_screenshots", "game_movies",
                     "game_packages", "game_package_subs")


NO_GAMES = "The file contains no games (the top level must be an object keyed by appid)"


class JsonSanityError(ValueError):
    """The JSON file is malformed or does not look like the Steam games dataset."""


# --- reading -------------------------------------------------------------------

def iter_games(path: Path) -> Iterator[tuple[str, Any]]:
    """Stream (appid_key, game) pairs one game at a time, so memory stays flat for any file size.

    Uses ijson.  Numbers arrive as int/float (use_float=True), not Decimal, so they
    serialise back to JSON and are typed correctly in the inventory.
    """
    try:
        import ijson
    except ImportError:
        raise JsonSanityError("Streaming JSON needs the 'ijson' package. Run: pip install ijson") from None

    backend = getattr(ijson, "backend_name", None) or getattr(ijson, "backend", "?")
    log.info("Streaming %s (%.0f MB) with ijson, backend %s", path.name, path.stat().st_size / 1e6, backend)
    if str(backend) == "python":
        log.warning("ijson is using its slow pure-Python backend; a large file can take many minutes")

    with path.open("rb") as fh:
        try:
            yield from ijson.kvitems(fh, "", use_float=True)
        except ijson.JSONError as exc:
            raise JsonSanityError(f"{path.name} is not valid JSON: {exc}") from None


def strip_nul(obj: Any) -> tuple[Any, int]:
    """Postgres cannot store U+0000 in text or jsonb.  Remove it, counting replacements."""
    if isinstance(obj, str):
        n = obj.count("\x00")
        return (obj.replace("\x00", "") if n else obj), n
    if isinstance(obj, dict):
        total, out = 0, {}
        for k, v in obj.items():
            k2, n1 = strip_nul(k)
            v2, n2 = strip_nul(v)
            out[k2] = v2
            total += n1 + n2
        return out, total
    if isinstance(obj, list):
        total, out = 0, []
        for v in obj:
            v2, n = strip_nul(v)
            out.append(v2)
            total += n
        return out, total
    return obj, 0


# --- inventory / sanity --------------------------------------------------------

def _jtype(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


@dataclass
class Inventory:
    """What the file contains: counts per key and JSON type, plus problems found."""
    games: int = 0
    nul_bytes: int = 0
    key_types: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    _seen: set[int] = field(default_factory=set, repr=False)

    def check(self, key: str, game: Any) -> int:
        """Validate one entry; return the integer appid.  Raises JsonSanityError."""
        try:
            appid = int(key)
        except ValueError:
            raise JsonSanityError(f"Top-level key {key!r} is not a numeric appid") from None
        if appid in self._seen:
            raise JsonSanityError(f"appid {appid} appears twice in the file")
        if not isinstance(game, dict):
            raise JsonSanityError(f"Entry {appid} is a {_jtype(game)}, expected an object")
        self._seen.add(appid)
        self.games += 1
        for k, v in game.items():
            self.key_types[k][_jtype(v)] += 1
        return appid

    def mixed_type_keys(self) -> dict[str, Counter]:
        return {k: c for k, c in self.key_types.items() if len(c) > 1}

    def log_summary(self) -> None:
        log.info("Sanity check passed: %d games, every appid numeric and unique, every entry an object",
                 self.games)
        if self.nul_bytes:
            log.warning("Removed %d NUL (\\u0000) characters, which Postgres cannot store", self.nul_bytes)
        log.info("%d distinct keys (present %% of games, JSON types):", len(self.key_types))
        width = max(len(k) for k in self.key_types) if self.key_types else 10
        for key, types in sorted(self.key_types.items()):
            present = sum(types.values()) / self.games * 100
            kinds = ", ".join(f"{t} x{n}" if len(types) > 1 else t for t, n in types.most_common())
            log.info("  %-*s %5.1f%%  %s", width, key, present, kinds)
        mixed = self.mixed_type_keys()
        if mixed:
            log.info("Keys with more than one type (handled by the normalisation SQL): %s",
                     ", ".join(sorted(mixed)))


def scan(path: Path) -> Inventory:
    """Dry run: validate the file and build the inventory without a database."""
    inv = Inventory()
    for key, game in iter_games(path):
        inv.check(key, game)
        _, n = strip_nul(game)
        inv.nul_bytes += n
        if inv.games % 10_000 == 0:
            log.info("  %d games checked ...", inv.games)
    if not inv.games:
        raise JsonSanityError(NO_GAMES)
    return inv


# --- landing table -------------------------------------------------------------

def load_landing(path: Path, engine: Engine, table: str = LANDING_TABLE, chunksize: int = 500) -> Inventory:
    """Validate and insert in ONE pass and ONE transaction.

    Any sanity problem raises and rolls back, leaving a previous table untouched.
    """
    q = engine.dialect.identifier_preparer.quote
    t = q(table)
    insert = text(f"INSERT INTO {t} (appid, data) VALUES (:appid, CAST(:data AS jsonb))")
    inv = Inventory()
    batch: list[dict] = []

    with engine.begin() as conn:
        log.info("Creating landing table %s (replacing it if it exists) ...", table)
        conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
        conn.execute(text(f"CREATE TABLE {t} (appid bigint PRIMARY KEY, data jsonb NOT NULL, "
                          f"loaded_at timestamptz NOT NULL DEFAULT now())"))

        for key, game in iter_games(path):
            appid = inv.check(key, game)
            game, n = strip_nul(game)
            inv.nul_bytes += n
            batch.append({"appid": appid, "data": json.dumps(game, ensure_ascii=False)})
            if len(batch) >= chunksize:
                conn.execute(insert, batch)
                batch.clear()
                if inv.games % 10_000 == 0:
                    log.info("  %d games inserted ...", inv.games)
        if batch:
            conn.execute(insert, batch)
        if not inv.games:
            raise JsonSanityError(NO_GAMES)

        log.info("Indexing %s.data (GIN) ...", table)
        conn.execute(text(f"CREATE INDEX {q(table + '_data_gin')} ON {t} USING gin (data)"))
    log.info("Committed: %s holds %d games as jsonb", table, inv.games)
    return inv


# --- normalised tables ---------------------------------------------------------

def _s(key: str) -> str:
    return f"data->>'{key}'"


def _num(key: str) -> str:
    """numeric from a JSON number, or from a string that looks like one; else NULL."""
    return (f"CASE WHEN jsonb_typeof(data->'{key}') = 'number' THEN ({_s(key)})::numeric "
            f"WHEN {_s(key)} ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN ({_s(key)})::numeric END")


def _int(key: str) -> str:
    return f"trunc({_num(key)})::bigint"


def _bool(key: str) -> str:
    return (f"CASE WHEN jsonb_typeof(data->'{key}') = 'boolean' THEN ({_s(key)})::boolean "
            f"WHEN lower({_s(key)}) IN ('true', 'false') THEN ({_s(key)})::boolean END")


def _arr(key: str) -> str:
    """The key as a jsonb array, or an empty array when absent or not an array."""
    return f"CASE WHEN jsonb_typeof(data->'{key}') = 'array' THEN data->'{key}' ELSE '[]'::jsonb END"


def _obj(key: str) -> str:
    return f"CASE WHEN jsonb_typeof(data->'{key}') = 'object' THEN data->'{key}' ELSE '{{}}'::jsonb END"


# release_date is "Oct 21, 2008" in the Kaggle file; newer scraper versions use {"date": ..., "coming_soon": ...}
_RELEASE_RAW = "COALESCE(data->'release_date'->>'date', data->>'release_date')"

SAFE_DATE_FN = """
CREATE OR REPLACE FUNCTION pg_temp.safe_date(txt text) RETURNS date
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF txt IS NULL THEN RETURN NULL; END IF;
    IF txt ~ '^[A-Za-z]{3} [0-9]{1,2}, [0-9]{4}$' THEN RETURN to_date(txt, 'Mon DD, YYYY'); END IF;
    IF txt ~ '^[0-9]{1,2} [A-Za-z]{3}, [0-9]{4}$' THEN RETURN to_date(txt, 'DD Mon, YYYY'); END IF;
    IF txt ~ '^[A-Za-z]{3} [0-9]{4}$' THEN RETURN to_date(txt, 'Mon YYYY'); END IF;
    RETURN NULL;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END $$
"""

# (column, SQL expression) for the one-row-per-game table
GAME_COLUMNS: list[tuple[str, str]] = [
    ("appid", "appid"),
    ("name", _s("name")),
    ("release_date_raw", _RELEASE_RAW),
    ("release_date", f"pg_temp.safe_date({_RELEASE_RAW})"),
    ("coming_soon", "CASE WHEN jsonb_typeof(data->'release_date'->'coming_soon') = 'boolean' "
                    "THEN (data->'release_date'->>'coming_soon')::boolean END"),
    ("required_age", _int("required_age")),
    ("is_free", _bool("is_free")),
    ("price", _num("price")),
    ("discount", _num("discount")),
    ("dlc_count", _int("dlc_count")),
    ("detailed_description", _s("detailed_description")),
    ("about_the_game", _s("about_the_game")),
    ("short_description", _s("short_description")),
    ("reviews", _s("reviews")),
    ("header_image", _s("header_image")),
    ("website", _s("website")),
    ("support_url", _s("support_url")),
    ("support_email", _s("support_email")),
    ("windows", _bool("windows")),
    ("mac", _bool("mac")),
    ("linux", _bool("linux")),
    ("metacritic_score", _int("metacritic_score")),
    ("metacritic_url", _s("metacritic_url")),
    ("user_score", _num("user_score")),
    ("score_rank", _s("score_rank")),
    ("achievements", _int("achievements")),
    ("recommendations", _int("recommendations")),
    ("notes", _s("notes")),
    ("estimated_owners", _s("estimated_owners")),
    ("owners_min", "CASE WHEN data->>'estimated_owners' ~ '^[0-9]+ - [0-9]+$' "
                   "THEN split_part(data->>'estimated_owners', ' - ', 1)::bigint END"),
    ("owners_max", "CASE WHEN data->>'estimated_owners' ~ '^[0-9]+ - [0-9]+$' "
                   "THEN split_part(data->>'estimated_owners', ' - ', 2)::bigint END"),
    ("positive", _int("positive")),
    ("negative", _int("negative")),
    ("average_playtime_forever", _int("average_playtime_forever")),
    ("average_playtime_2weeks", _int("average_playtime_2weeks")),
    ("median_playtime_forever", _int("median_playtime_forever")),
    ("median_playtime_2weeks", _int("median_playtime_2weeks")),
    ("peak_ccu", _int("peak_ccu")),
]


def _st(name: str) -> str:
    """A normalised table's name, qualified with the staging schema."""
    return f"{STAGING_SCHEMA}.{name}"


GAMES = _st("games")


def _list_table(name: str, key: str, column: str, landing: str, index: bool = True) -> list[str]:
    """One row per array element, keeping the position; keyed by (appid, position)."""
    table = _st(name)
    stmts = [
        f"CREATE TABLE {table} AS "
        f"SELECT appid, ord::int AS position, value AS {column} "
        f"FROM {landing}, jsonb_array_elements_text({_arr(key)}) WITH ORDINALITY AS t(value, ord)",
        f"ALTER TABLE {table} ADD PRIMARY KEY (appid, position)",
        f"ALTER TABLE {table} ADD FOREIGN KEY (appid) REFERENCES {GAMES}(appid) ON DELETE CASCADE",
    ]
    if index:
        stmts.append(f"CREATE INDEX ON {table} ({column})")
    return stmts


def normalise_statements(landing: str) -> list[str]:
    """All SQL to rebuild the staging tables from `landing` (already quoted)."""
    cols = ",\n       ".join(f"{expr} AS {col}" for col, expr in GAME_COLUMNS)
    stmts = [f"CREATE SCHEMA IF NOT EXISTS {STAGING_SCHEMA}"]
    stmts += [f"DROP TABLE IF EXISTS {_st(t)} CASCADE" for t in reversed(NORMALISED_TABLES)]
    stmts += [
        f"CREATE TABLE {GAMES} AS\nSELECT {cols}\nFROM {landing}",
        f"ALTER TABLE {GAMES} ADD PRIMARY KEY (appid)",
        f"CREATE INDEX ON {GAMES} (release_date)",
        f"CREATE INDEX ON {GAMES} (name)",
    ]
    stmts += _list_table("game_developers", "developers", "developer", landing)
    stmts += _list_table("game_publishers", "publishers", "publisher", landing)
    stmts += _list_table("game_genres", "genres", "genre", landing)
    stmts += _list_table("game_categories", "categories", "category", landing)
    stmts += _list_table("game_screenshots", "screenshots", "url", landing, index=False)
    stmts += _list_table("game_movies", "movies", "url", landing, index=False)
    stmts += [
        # tags: {"Indie": 22, ...} in most games, but [] when a game has none
        f"""CREATE TABLE {_st("game_tags")} AS
SELECT appid, tag, max(votes) AS votes FROM (
    SELECT appid, key AS tag,
           CASE WHEN jsonb_typeof(value) = 'number' THEN trunc((value #>> '{{}}')::numeric)::int END AS votes
    FROM {landing}, jsonb_each({_obj("tags")})
    UNION ALL
    SELECT appid, value, NULL FROM {landing}, jsonb_array_elements_text({_arr("tags")})
) t GROUP BY appid, tag""",
        f"ALTER TABLE {_st('game_tags')} ADD PRIMARY KEY (appid, tag)",
        f"ALTER TABLE {_st('game_tags')} ADD FOREIGN KEY (appid) REFERENCES {GAMES}(appid) "
        "ON DELETE CASCADE",
        f"CREATE INDEX ON {_st('game_tags')} (tag)",
        # languages: a list in the Kaggle file, a comma-separated string with HTML in older scrapes
        f"""CREATE TABLE {_st("game_languages")} AS
WITH supported AS (
    SELECT DISTINCT appid, trim(regexp_replace(value, '<[^>]+>|\\*', '', 'g')) AS language
    FROM {landing}, jsonb_array_elements_text({_arr("supported_languages")})
    UNION
    SELECT DISTINCT appid, trim(regexp_replace(part, '<[^>]+>|\\*', '', 'g'))
    FROM {landing}, regexp_split_to_table(data->>'supported_languages', ',') AS part
    WHERE jsonb_typeof(data->'supported_languages') = 'string'
), audio AS (
    SELECT DISTINCT appid, trim(value) AS language
    FROM {landing}, jsonb_array_elements_text({_arr("full_audio_languages")})
)
SELECT s.appid, s.language, (a.language IS NOT NULL) AS full_audio
FROM supported s LEFT JOIN audio a USING (appid, language)
WHERE s.language <> ''""",
        f"ALTER TABLE {_st('game_languages')} ADD PRIMARY KEY (appid, language)",
        f"ALTER TABLE {_st('game_languages')} ADD FOREIGN KEY (appid) REFERENCES {GAMES}(appid) "
        "ON DELETE CASCADE",
        f"CREATE INDEX ON {_st('game_languages')} (language)",
        # packages and their purchase options
        f"""CREATE TABLE {_st("game_packages")} AS
SELECT appid, p.ord::int AS package_no, p.pkg->>'title' AS title, p.pkg->>'description' AS description
FROM {landing}, jsonb_array_elements({_arr("packages")}) WITH ORDINALITY AS p(pkg, ord)
WHERE jsonb_typeof(p.pkg) = 'object'""",
        f"ALTER TABLE {_st('game_packages')} ADD PRIMARY KEY (appid, package_no)",
        f"ALTER TABLE {_st('game_packages')} ADD FOREIGN KEY (appid) REFERENCES {GAMES}(appid) "
        "ON DELETE CASCADE",
        f"""CREATE TABLE {_st("game_package_subs")} AS
SELECT appid, p.ord::int AS package_no, s.ord::int AS sub_no,
       s.sub->>'text' AS text, s.sub->>'description' AS description,
       CASE WHEN jsonb_typeof(s.sub->'price') = 'number' THEN (s.sub->>'price')::numeric END AS price
FROM {landing},
     jsonb_array_elements({_arr("packages")}) WITH ORDINALITY AS p(pkg, ord),
     jsonb_array_elements(CASE WHEN jsonb_typeof(p.pkg->'subs') = 'array' THEN p.pkg->'subs' ELSE '[]'::jsonb END)
         WITH ORDINALITY AS s(sub, ord)
WHERE jsonb_typeof(s.sub) = 'object'""",
        f"ALTER TABLE {_st('game_package_subs')} ADD PRIMARY KEY (appid, package_no, sub_no)",
        f"ALTER TABLE {_st('game_package_subs')} ADD FOREIGN KEY (appid, package_no) "
        f"REFERENCES {_st('game_packages')}(appid, package_no) ON DELETE CASCADE",
    ]
    return stmts


def normalise(engine: Engine, landing_table: str = LANDING_TABLE) -> dict[str, int]:
    """Rebuild every normalised table from the landing table in ONE transaction.  Returns row counts."""
    q = engine.dialect.identifier_preparer.quote
    landing = q(landing_table)
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": landing}).scalar_one()
        if not exists:
            raise JsonSanityError(f"Landing table {landing_table} does not exist; load the JSON first")
        conn.execute(text(SAFE_DATE_FN))  # session-local helper; needs plpgsql, which is installed by default
        log.info("Building %d tables in schema %s from %s ...", len(NORMALISED_TABLES),
                 STAGING_SCHEMA, landing_table)
        for stmt in normalise_statements(landing):
            log.debug("SQL: %s", stmt)
            conn.execute(text(stmt))
        for name in NORMALISED_TABLES:
            counts[name] = conn.execute(text(f"SELECT count(*) FROM {_st(name)}")).scalar_one()
    width = max(len(_st(n)) for n in counts)
    for name, n in counts.items():
        log.info("  %-*s %10d rows", width, _st(name), n)
    log.info("Committed: schema %s rebuilt", STAGING_SCHEMA)
    return counts


def legacy_tables(engine: Engine) -> list[str]:
    """Normalised tables that earlier versions built in public and that still exist."""
    with engine.connect() as conn:
        return [name for name in NORMALISED_TABLES if conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"),
            {"t": f"{LEGACY_SCHEMA}.{name}"}).scalar_one()]


def drop_legacy(engine: Engine, names: list[str]) -> None:
    """Drop the old copies in public, children first, in ONE transaction."""
    with engine.begin() as conn:
        for name in reversed(NORMALISED_TABLES):
            if name in names:
                conn.execute(text(f"DROP TABLE IF EXISTS {LEGACY_SCHEMA}.{name} CASCADE"))
    log.info("Dropped the old copies in %s: %s", LEGACY_SCHEMA, ", ".join(names))
