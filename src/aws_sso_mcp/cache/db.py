"""SQLite schema and connection management for the account/role directory cache.

This database holds only non-sensitive discovery data (account ids/names,
role names, sync timestamps) - never tokens or credentials. It is a lookup
accelerator, never an authorization decision: every real AWS call is still
authorized by AWS itself at call time, so a stale or even corrupted cache
can only cause an unnecessary miss/refresh, never grant unauthorized access.

Concurrency model: WAL journal + per-thread connections. WAL lets many
readers run concurrently while one writer is in flight, and per-thread
connections mean a fan-out doesn't serialize every SQLite operation through
one lock. Writes are still coordinated by SQLite itself.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 2

# Tables holding cached directory data. Listed explicitly so a schema upgrade
# can drop exactly these and nothing else.
_CACHE_TABLES = ("accounts", "account_roles", "sync_state")

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
CREATE INDEX IF NOT EXISTS idx_accounts_org ON accounts (org);

CREATE TABLE IF NOT EXISTS account_roles (
    org TEXT NOT NULL,
    account_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (org, account_id, role_name)
);
CREATE INDEX IF NOT EXISTS idx_account_roles_org_acct ON account_roles (org, account_id);

CREATE TABLE IF NOT EXISTS sync_state (
    org TEXT PRIMARY KEY,
    last_full_sync TEXT NOT NULL,
    account_count INTEGER NOT NULL
);
"""

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    # Both of the sizes below are *per connection*, and this pool hands out one
    # connection per thread across pools that can total a few hundred threads.
    # The cached directory is a few thousand small rows, so the previous
    # 128 MiB mmap / 64 MiB page cache reserved orders of magnitude more
    # address space and memory than the data could ever occupy.
    "PRAGMA mmap_size=16777216",  # 16 MiB - comfortably larger than the whole DB
    "PRAGMA cache_size=-4096",  # ~4 MiB page cache per connection
    "PRAGMA busy_timeout=5000",
)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    for stmt in _PRAGMAS:
        with contextlib.suppress(sqlite3.DatabaseError):
            # A tmpfs or restricted FS may reject a pragma (e.g. no WAL on
            # network mounts); we treat it as best-effort - correctness is
            # unaffected, only performance.
            conn.execute(stmt)


def _open(db_path: Path) -> sqlite3.Connection:
    """Open one connection with the standard row factory and pragmas applied."""
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def _upgrade_if_needed(conn: sqlite3.Connection) -> None:
    """Rebuild the cached tables if they were written by a different schema version.

    Everything in this database is derived state that can be re-fetched from
    SSO, so an upgrade is simply a discard: dropping the tables costs one
    directory refresh and is immune to the migration bugs that a hand-written
    ALTER path would invite.

    Without this check the stored version was written once and never read
    again, which meant a change to any table - or to the format of a stored
    timestamp - would surface as a parse error or a wrong answer against a
    pre-existing cache file instead of a clean rebuild.
    """
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is not None and row["version"] == SCHEMA_VERSION:
        return

    if row is not None:
        for table in _CACHE_TABLES:
            # Table names come from the fixed _CACHE_TABLES literal, never
            # from input, so this f-string cannot carry injected SQL.
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(_SCHEMA)

    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the cache database, with the schema applied.

    Also rebuilds the cached tables when the file on disk was written by a
    different :data:`SCHEMA_VERSION`.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(db_path)
    conn.executescript(_SCHEMA)
    _upgrade_if_needed(conn)
    return conn


class ConnectionPool:
    """Per-thread SQLite connections against one database path.

    ``sqlite3.Connection`` objects are cheap to hold and expensive to open;
    reusing one per thread is the standard Python pattern for fan-out
    workloads that hit the same DB. Every thread gets a fresh connection
    the first time it asks, and reuses it for the process lifetime.

    All connections share WAL journal + the pragmas from :data:`_PRAGMAS`,
    so concurrent readers never block each other and only writers serialize
    (via SQLite's own locking, not a Python lock).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        # Open one connection up-front - guarantees the schema exists and
        # the first thread doesn't take the cost of ``connect(...)`` inside
        # a request path.
        self._local.conn = connect(db_path)

    def get(self) -> sqlite3.Connection:
        """Return this thread's connection, opening one on first use.

        Only the connection opened in :meth:`__init__` applies the schema;
        every later one attaches to a database that already has it.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _open(self._db_path)
            self._local.conn = conn
        return conn

    def with_write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run a write inside an IMMEDIATE transaction on the caller's connection."""
        conn = self.get()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result
