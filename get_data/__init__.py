"""get_data: download the Kaggle Steam games dataset and load it into Postgres.

Run with ``python -m get_data --help``.  The steps live in separate modules:

- prompts.py          y/n and text prompts, with a global auto-yes switch (-y)
- config.py           username/database/host/port, optionally saved to db_config.json
- kaggle_download.py  fetch one dataset file into the project tmp/ folder
- transform.py        header repair, snake_case column names, sanity check
- db.py               engine, connection test, upload, primary key (CSV path)
- json_load.py        games.json -> jsonb landing table + normalised tables (JSON path, --json)
- logging_setup.py    console + info.log + debug.log, with personal details redacted in the files
- sql_runner.py       shared helpers for model_data/ and qa_data/: connect, split and run .sql files
- cli.py              argparse + the orchestration in run()
"""

__version__ = "0.4.0"  # REMINDER: run a security audit of the project before releasing 1.0
