"""Database connection settings: resolve, and optionally save, everything but the password.

Precedence for each field: command-line flag > db_config.json > keyboard prompt
(host and port prompts offer localhost and 5433, so Enter keeps the SSH tunnel defaults).
The password is never written anywhere.  It comes from the PGPASSWORD environment
variable when set, otherwise from a hidden prompt.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .logging_setup import register_connection
from .prompts import ask_text, ask_yes_no

log = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5433


@dataclass
class DbSettings:
    username: str
    database: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def describe(self) -> str:
        return f"{self.username}@{self.host}:{self.port}/{self.database}"


def load_saved(path: Path) -> dict:
    """Return the saved settings, or an empty dict if there are none or the file is unreadable."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable %s: %s", path.name, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("Ignoring %s: expected a JSON object", path.name)
        return {}
    data.pop("password", None)  # never trust or reuse a password from disk
    return data


def resolve_settings(args, config_path: Path) -> tuple[DbSettings, dict]:
    """Build DbSettings from flags, the saved file and prompts.  Returns (settings, saved_dict)."""
    saved = load_saved(config_path)
    if saved:
        log.info("Loaded saved connection settings from %s", config_path.name)

    username = args.user or saved.get("username") or ask_text("DB username", flag_hint="--user")
    database = args.database or saved.get("database") or ask_text("DB name", flag_hint="--database")
    host = args.host or saved.get("host") or ask_text("DB host", default=DEFAULT_HOST)
    port = args.port or saved.get("port") or ask_text("DB port", default=str(DEFAULT_PORT))
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid port: {port!r}") from None

    register_connection(username, host, database)  # keep them out of the log files
    return DbSettings(username=username, database=database, host=host, port=port), saved


def get_password(settings: DbSettings) -> str:
    env = os.environ.get("PGPASSWORD")
    if env:
        log.info("Using the password from the PGPASSWORD environment variable")
        return env
    return getpass.getpass(f"DB password for {settings.describe()}: ")


def offer_to_save(settings: DbSettings, saved: dict, config_path: Path) -> None:
    """After a successful connection, offer to remember the settings (never the password)."""
    current = asdict(settings)
    if {k: saved.get(k) for k in current} == current:
        return  # nothing new to save
    if not ask_yes_no(f"Save {settings.describe()} to {config_path.name} for next time? (password is not saved)",
                      default=False):
        return
    config_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    log.info("Saved connection settings to %s (git-ignored)", config_path)
