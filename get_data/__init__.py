"""get_data: download the Kaggle Steam games dataset and load it into Postgres.

Run with ``python -m get_data --help``.  The steps live in separate modules:

- prompts.py          y/n and text prompts, with a global auto-yes switch (-y)
- config.py           username/database/host/port, optionally saved to db_config.json
- kaggle_download.py  fetch one dataset file into the project tmp/ folder
- transform.py        header repair, snake_case column names, sanity check
- db.py               engine, connection test, upload, primary key
- cli.py              argparse + logging + the orchestration in run()
"""

__version__ = "0.2.0"
