"""SQLite schema and connection management for the account/role directory cache.

This database holds only non-sensitive discovery data (account ids/names,
role names, sync timestamps) - never tokens or credentials. It is a lookup
accelerator, never an authorization decision: every real AWS call is still
authorized by AWS itself at call time, so a stale or even corrupted cache
can only cause an unnecessary miss/refresh, never grant unauthorized access.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    org TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (org, account_id)
);
CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts (account_name);

CREATE TABLE IF NOT EXISTS account_roles (
    org TEXT NOT NULL,
    account_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (org, account_id, role_name)
);

CREATE TABLE IF NOT EXISTS sync_state (
    org TEXT PRIMARY KEY,
    last_full_sync TEXT NOT NULL,
    account_count INTEGER NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the cache database, with the schema applied."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return conn
