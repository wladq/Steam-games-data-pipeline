"""Logging: plain console output plus two rotating, anonymised log files.

    logs/info.log    INFO only: the readable story of each run
    logs/debug.log   everything else: DEBUG detail, WARNING, ERROR, CRITICAL and tracebacks

Everything written to the files passes through REDACTOR, which replaces
personal details with placeholders:

    OS user name, home folder        ->  <user>, ~
    computer name                    ->  <computer>
    database user / host / name      ->  <db-user>, <db-host>, <db-name>
    e-mail, IPv4/IPv6/MAC, *.ts.net  ->  <email>, <ip>, <addr>, <tailnet-host>
    words in GET_DATA_REDACT=a,b,c   ->  <redacted>

The console is NOT redacted: it is your own screen and you need to see which
database you are connecting to.  Redaction is pattern based, so treat it as a
safety net, not a guarantee, before sharing a log file.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import os
import platform
import re
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

INFO_LOG = "info.log"
DEBUG_LOG = "debug.log"
MAX_BYTES = 5_000_000
BACKUPS = 3

# never treated as secrets, even if they happen to be a user or database name
_SAFE_WORDS = {"localhost", "postgres", "public", "steam", "games", "user", "users",
               "admin", "root", "none"}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TAILNET = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.ts\.net\b", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
# full or partly written IPv6 and MAC addresses; '::' short forms are left alone
# on purpose, because they are indistinguishable from SQL casts such as added::date
_COLON_ADDR = re.compile(r"(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![\w:])", re.IGNORECASE)
_USER_DIR = re.compile(r"(?:\b[A-Za-z]:[\\/]+Users[\\/]+|/home/|/Users/)[^\\/\s'\":;,<>|]+",
                       re.IGNORECASE)


def _ipv4(match: re.Match) -> str:
    octets = [int(g) for g in match.groups()]
    if any(o > 255 for o in octets) or octets[0] in (0, 127):
        return match.group(0)  # not an address, or loopback: harmless and useful
    return "<ip>"


class Redactor:
    """Replaces registered words and address-like patterns in a piece of text."""

    def __init__(self) -> None:
        self._words: dict[str, str] = {}
        self._pattern: re.Pattern | None = None

    def add(self, value, placeholder: str) -> None:
        """Register a word to hide.  Short or generic values are ignored to avoid mangling logs."""
        word = str(value).strip().lower() if value else ""
        if len(word) < 3 or word in _SAFE_WORDS:
            return
        self._words[word] = placeholder
        self._pattern = None

    def __call__(self, text: str) -> str:
        text = _EMAIL.sub("<email>", text)
        text = _TAILNET.sub("<tailnet-host>", text)
        text = _IPV4.sub(_ipv4, text)
        text = _COLON_ADDR.sub("<addr>", text)
        text = _USER_DIR.sub("~", text)
        if self._words:
            if self._pattern is None:
                longest_first = sorted(self._words, key=len, reverse=True)
                words = "|".join(map(re.escape, longest_first))
                self._pattern = re.compile(
                    r"(?<![A-Za-z0-9_])(?:" + words + r")(?![A-Za-z0-9_])", re.IGNORECASE)
            text = self._pattern.sub(lambda m: self._words[m.group(0).lower()], text)
        return text


REDACTOR = Redactor()


def register_connection(user=None, host=None, database=None) -> None:
    """Hide the database user, host and name in the log files from now on."""
    REDACTOR.add(user, "<db-user>")
    REDACTOR.add(host, "<db-host>")
    REDACTOR.add(database, "<db-name>")


def _register_machine() -> None:
    users = {os.environ.get("USERNAME"), os.environ.get("USER"), Path.home().name}
    # no user database, e.g. in a container: getuser raises OSError since Python 3.13,
    # and KeyError or ImportError (no pwd module) before that
    with contextlib.suppress(OSError, KeyError, ImportError):
        users.add(getpass.getuser())
    for name in users:
        REDACTOR.add(name, "<user>")
    for name in (os.environ.get("COMPUTERNAME"), platform.node(), socket.gethostname()):
        REDACTOR.add(name, "<computer>")
    for word in os.environ.get("GET_DATA_REDACT", "").split(","):
        REDACTOR.add(word, "<redacted>")


class RedactingFormatter(logging.Formatter):
    """Redacts the finished line, so messages, arguments and tracebacks are all covered."""

    def format(self, record: logging.LogRecord) -> str:
        return REDACTOR(super().format(record))


class _ConsoleFormatter(logging.Formatter):
    """Plain messages for INFO, a level prefix for everything else."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return msg if record.levelno == logging.INFO else f"{record.levelname}: {msg}"


def setup_logging(verbose: bool = False, log_dir: Path | None = None) -> None:
    """Console at INFO (DEBUG with verbose); with log_dir, also info.log and debug.log."""
    _register_machine()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_ConsoleFormatter("%(message)s"))
    root.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_format = RedactingFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        for file_name, keep in ((INFO_LOG, lambda r: r.levelno == logging.INFO),
                                (DEBUG_LOG, lambda r: r.levelno != logging.INFO)):
            handler = RotatingFileHandler(log_dir / file_name, maxBytes=MAX_BYTES,
                                          backupCount=BACKUPS, encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.addFilter(keep)
            handler.setFormatter(file_format)
            root.addHandler(handler)

    for noisy in ("urllib3", "requests", "kagglesdk", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
