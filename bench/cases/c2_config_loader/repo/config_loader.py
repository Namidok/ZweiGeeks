"""Loads service settings from YAML, stores them in SQLite, and reports the
git commit the service is running from.

Everything here is deliberately the safe variant: yaml.safe_load,
parameterised SQL, and subprocess with an argument list and no shell.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import yaml

REQUIRED_KEYS = ("name", "port")


class ConfigError(ValueError):
    """The settings file is missing something or has the wrong type."""


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError("the top level must be a mapping")
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ConfigError(f"missing keys: {', '.join(missing)}")
    port = data["port"]
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError("port must be an integer between 1 and 65535")
    return data


def save_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def current_commit(repo_dir: str | Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir, capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()
