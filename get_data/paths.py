"""Where the project keeps its own files: saved settings, logs, downloads, dashboard output.

From a clone of the repository (also after pip install -e .) they stay in the repository
folder, as the README describes: db_config.json, logs/, tmp/ and docs/.

Installed with a regular pip install, the code lives in site-packages, which is no place
for user data.  The same files then go to the user's local data folder:

    Windows  %LOCALAPPDATA%\\Steam-games-data-pipeline   (local, never the roaming profile)
    macOS    ~/Library/Application Support/Steam-games-data-pipeline
    Linux    $XDG_DATA_HOME/Steam-games-data-pipeline, by default ~/.local/share/...
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from . import PROJECT_NAME

CODE_ROOT = Path(__file__).resolve().parent.parent  # the clone, or site-packages


def is_clone(root: Path = CODE_ROOT) -> bool:
    """True when `root` is a checkout of the repository: pyproject.toml sits next to the
    packages there, and never in site-packages."""
    return (root / "pyproject.toml").is_file()


def user_data_dir(platform: str = sys.platform, env: Mapping[str, str] | None = None,
                  home: Path | None = None) -> Path:
    """This operating system's per-user folder for the project's data."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if platform == "win32":
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / PROJECT_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / PROJECT_NAME
    base = env.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / PROJECT_NAME


DATA_ROOT = CODE_ROOT if is_clone() else user_data_dir()
CONFIG_PATH = DATA_ROOT / "db_config.json"
LOG_DIR = DATA_ROOT / "logs"
TMP_DIR = DATA_ROOT / "tmp"
DASHBOARD_DIR = DATA_ROOT / "docs"
