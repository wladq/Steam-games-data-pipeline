"""Export a snapshot of the core model to docs/: a Markdown report and a chart page.

Run from the project root:
    python main.py dashboard                 (or python dashboard/export.py, which needs
                                              the project installed: pip install -e .)

Read-only.  Runs the SELECTs in queries.sql and the QA checks, then writes:
    docs/report.md             tables and a pie chart that GitHub renders in the repo
    docs/index.html            interactive charts (Plotly.js from its CDN), one file
    docs/data/dashboard.json   the same numbers as data
No connection details, paths or user names are written to any of them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import logging
from pathlib import Path

from get_data import PROJECT_NAME, __version__
from get_data.formatting import percent
from get_data.paths import DASHBOARD_DIR
from get_data.sql_runner import (
    add_common_args,
    autocommit,
    execute,
    read_statements,
    run_as_script,
    run_step,
)
from qa_data import run_qa

HERE = Path(__file__).resolve().parent
log = logging.getLogger("dashboard")

QUERIES_FILE = HERE / "queries.sql"
TEMPLATE_FILE = HERE / "template.html"
DEFAULT_OUT = DASHBOARD_DIR  # docs/ in the clone, or in the user's data folder when installed
DATA_PLACEHOLDER = "__DASHBOARD_DATA__"
STATUS_ICON = {"pass": "✅ pass", "warn": "⚠️ warn", "FAIL": "❌ FAIL",
               "ERROR": "❌ ERROR", "skip": "⏭️ skip"}
CHECK_FIELDS = ("check", "severity", "status", "failed", "about")


# --- collecting ----------------------------------------------------------------------

def jsonable(value):
    """Database values as JSON-friendly Python: Decimal -> int/float, date -> ISO text."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def run_queries(conn) -> dict[str, list[dict]]:
    """Every query in queries.sql, as {name: [row dict, ...]}."""
    results = {}
    for stmt in read_statements(QUERIES_FILE):
        name = stmt.meta.get("query")
        if not name:
            raise ValueError(f"{QUERIES_FILE.name} line {stmt.line} has no '-- query:' header")
        log.info("  %s", name)
        rows = execute(conn, stmt.sql).mappings().all()
        results[name] = [{k: jsonable(v) for k, v in row.items()} for row in rows]
    return results


def qa_summary(constraints: list[dict], checks: list[dict]) -> dict:
    """Overall data-quality state from the constraint rows and the check results."""
    tally = {s: sum(c["status"] == s for c in checks) for s in STATUS_ICON}
    invalid = sum(not c["valid"] for c in constraints)
    ok = bool(constraints) and not invalid and not tally["FAIL"] and not tally["ERROR"]
    return {"ok": ok, "constraints": len(constraints), "constraints_valid":
            len(constraints) - invalid, "checks": tally}


def collect(engine) -> dict:
    """Run every query and the QA checks; return the whole snapshot as one dict."""
    # autocommit: a failing check must not abort the queries that follow it
    with autocommit(engine) as conn:
        if not execute(conn, "SELECT to_regclass('core.games') IS NOT NULL").scalar_one():
            raise RuntimeError("core.games does not exist. Build the model first: "
                               "python main.py model")
        log.info("Running the dashboard queries ...")
        data = run_queries(conn)
        log.info("Running the QA checks (read-only) ...")
        checks = [{k: c[k] for k in CHECK_FIELDS} | {"detail": c["detail"]}
                  for c in run_qa.run_checks(conn)]
    data["checks"] = checks
    data["qa"] = qa_summary(data["constraints"], checks)
    data["generated"] = dt.date.today().isoformat()
    data["project"] = PROJECT_NAME
    data["version"] = __version__
    return data


# --- Markdown report -------------------------------------------------------------------

def num(value) -> str:
    """1234567 -> '1,234,567'; floats keep one decimal; None -> ''."""
    if value is None:
        return ""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def md_escape(text) -> str:
    """Make a value safe inside a Markdown table cell."""
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def text_bar(value, maximum, width: int = 24) -> str:
    """A bar of block characters proportional to value / maximum."""
    if not maximum or not value:
        return ""
    return "█" * max(1, round(width * value / maximum))


def md_table(headers: list[str], rows: list[list], align: str = "") -> str:
    """A Markdown table; align holds one 'l' or 'r' per column (default: left)."""
    align = align or "l" * len(headers)
    marks = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(marks) + " |"]
    lines += ["| " + " | ".join(md_escape(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def bar_table(rows: list[dict], label: str, value: str, title: str, extra=()) -> str:
    """Table of label, value, a text bar, and optional extra (header, key) columns."""
    top = max((r[value] or 0 for r in rows), default=0)
    headers = [title, value.replace("_", " ").capitalize(), ""] + [h for h, _ in extra]
    body = [[r[label], num(r[value]), text_bar(r[value], top)] + [num(r[k]) for _, k in extra]
            for r in rows]
    return md_table(headers, body, "lrl" + "r" * len(extra))


def mermaid_pie(title: str, parts: dict[str, int]) -> str:
    """A Mermaid pie chart, which GitHub draws inside Markdown."""
    lines = ["```mermaid", "pie showData", f"    title {title}"]
    lines += [f'    "{name.replace(chr(34), "")}" : {value}' for name, value in parts.items()]
    return "\n".join(lines + ["```"])


def share_at(concentration: list[dict], top: int):
    """Percent of all reviews held by the top `top` percent of games."""
    for row in concentration:
        if row["top_percent_of_games"] == top:
            return row["percent_of_reviews"]
    return None


def render_markdown(data: dict) -> str:  # pylint: disable=too-many-locals
    """The whole report as Markdown."""
    t = data["totals"][0]
    qa = data["qa"]
    checks = qa["checks"]
    years = data["releases_per_year"]
    lines = [
        f"# {data['project']}: data snapshot",
        "",
        (f"Generated {data['generated']} from the `core` model ({data['project']} "
         f"{data['version']}). "
         "Interactive charts: [index.html](index.html), opened from a local clone or GitHub "
         "Pages. Refresh with `python main.py dashboard`."),
        "",
        "## Headline numbers",
        "",
        md_table(["", ""], [
            ["Games", num(t["games"])], ["Developers", num(t["developers"])],
            ["Tags", num(t["tags"])], ["Genres", num(t["genres"])],
            ["Languages", num(t["languages"])], ["Reviews", num(t["reviews"])],
            ["Releases", f"{t['first_release']} to {t['last_release']}"],
        ], "lr"),
        "",
        "## Data quality",
        "",
        (f"**{'✅ OK' if qa['ok'] else '❌ PROBLEMS FOUND'}**: "
         f"{qa['constraints_valid']} of {qa['constraints']} constraints valid; checks: "
         f"{checks['pass']} passed, {checks['warn']} warnings, {checks['FAIL']} failed, "
         f"{checks['ERROR']} could not run, {checks['skip']} skipped."),
    ]
    if not qa["constraints"]:
        lines += ["", "No constraints found: run `python main.py qa` after each model build."]
    lines += [
        "",
        "<details><summary>Every check</summary>",
        "",
        md_table(["Status", "Severity", "Rows", "Check", "About"],
                 [[STATUS_ICON.get(c["status"], c["status"]), c["severity"],
                   num(c["failed"]), c["check"], c["detail"] or c["about"]]
                  for c in data["checks"]], "llrll"),
        "",
        "</details>",
        "",
        "## Free and paid games",
        "",
        mermaid_pie("Free vs paid games",
                    {"Free": t["free_games"], "Paid": t["paid_games"]}),
        "",
        "## Price bands",
        "",
        bar_table(data["price_bands"], "band", "games", "Price"),
        "",
        "## Releases per year",
        "",
        (f"The last year, {years[-1]['year'] if years else '-'}, is partial: the data is a "
         "snapshot."),
        "",
        bar_table(years, "year", "games", "Year"),
        "",
        "## Platforms",
        "",
        "A game can run on several platforms, so the shares add up to more than 100 %.",
        "",
        md_table(["Platform", "Games", "Share"],
                 [[p["platform"], num(p["games"]), percent(p["games"], t["games"])]
                  for p in data["platforms"]], "lrr"),
        "",
        "## Top tags by votes",
        "",
        bar_table(data["top_tags"], "tag", "votes", "Tag", [("Games", "games")]),
        "",
        "## Top genres",
        "",
        bar_table(data["top_genres"], "genre", "games", "Genre"),
        "",
        "## Top languages",
        "",
        bar_table(data["top_languages"], "language", "games", "Language",
                  [("Full audio", "full_audio")]),
        "",
        "## Review concentration",
        "",
        ("Reviews pile up on a few hits, so statistics weighted by reviews mostly describe "
         "blockbusters."),
        "",
        md_table(["Top games", "Share of all reviews"],
                 [[f"{p} %", f"{share_at(data['review_concentration'], p)} %"]
                  for p in (1, 5, 10, 25, 50)], "lr"),
        "",
    ]
    return "\n".join(lines)


# --- chart page ------------------------------------------------------------------------

def render_html(data: dict, template: str) -> str:
    """The template with the snapshot embedded as JSON in a <script> data block."""
    payload = (json.dumps(data, ensure_ascii=False)
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    if DATA_PLACEHOLDER not in template:
        raise ValueError(f"{TEMPLATE_FILE.name} has no {DATA_PLACEHOLDER} placeholder")
    return template.replace(DATA_PLACEHOLDER, payload)


def write_outputs(data: dict, out_dir: Path) -> list[Path]:
    """Write report.md, index.html and data/dashboard.json; return their paths."""
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    files = {
        out_dir / "report.md": render_markdown(data),
        out_dir / "index.html": render_html(data, TEMPLATE_FILE.read_text(encoding="utf-8")),
        out_dir / "data" / "dashboard.json": json.dumps(data, indent=2, ensure_ascii=False),
    }
    for path, text in files.items():
        # LF on every OS, matching .gitattributes (Windows would otherwise write CRLF)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return list(files)


# --- command line ----------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags of this step; main.py uses it for `python main.py dashboard`."""
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="folder for report.md, index.html and data/ "
                             "(default: %(default)s)")
    add_common_args(parser)


def run(args: argparse.Namespace) -> int:
    """Collect the snapshot and write the files; return the exit code."""

    def work(engine):
        data = collect(engine)
        for path in write_outputs(data, args.out):
            log.info("Wrote %s", path)
        log.info("Done. Data quality: %s", "OK" if data["qa"]["ok"] else "PROBLEMS FOUND")

    return run_step(args, work, log)


if __name__ == "__main__":
    raise SystemExit(run_as_script(__doc__, add_arguments, run))
