"""Lazy, 12h-threshold account/role directory cache, with single-flight refresh.

The refresh policy is intentionally simple and synchronous (no background
threads, no stale-while-revalidate): the first account-touching operation
for an org each cache-TTL window pays the full refresh cost once, then every
subsequent lookup in that window is a local SQLite read.

Backed by a :class:`~.db.ConnectionPool` giving each thread its own
connection over a WAL-mode database - concurrent readers never block each
other, so a fan-out that reads roles for 500 accounts scales linearly.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..logging_setup import get_logger
from ..sso.directory import SsoAccount, list_account_roles, list_accounts
from .db import ConnectionPool

logger = get_logger(__name__)

# UTC-explicit on purpose: the stamp is produced from an aware UTC datetime,
# and writing the "Z" makes the stored value self-describing rather than
# relying on every reader remembering to attach the timezone. Matches the
# format used for SSO token expiry in ``sso.device_auth``.
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_stamp(raw: str) -> datetime:
    return datetime.strptime(raw, _TIME_FORMAT).replace(tzinfo=UTC)


def _read_generation(conn: sqlite3.Connection, org: str) -> str | None:
    """Return the org's ``last_full_sync`` stamp, which identifies a directory generation.

    Any :meth:`AccountCache.ensure_fresh` refresh rewrites this stamp and wipes the
    org's cached role grants, so a change here means previously fetched grants
    belong to a superseded generation.
    """
    row = conn.execute("SELECT last_full_sync FROM sync_state WHERE org = ?", (org,)).fetchone()
    return None if row is None else str(row["last_full_sync"])


@dataclass(frozen=True, slots=True)
class SyncState:
    org: str
    last_full_sync: datetime | None
    account_count: int


class AccountCache:
    """Per-org account directory, backed by SQLite, refreshed on a TTL threshold.

    One instance is shared process-wide; a per-org lock ensures concurrent
    callers never trigger duplicate refreshes for the same org (single-flight).
    """

    def __init__(self, db_path: Path, ttl_hours: float) -> None:
        self._pool = ConnectionPool(db_path)
        self._ttl = timedelta(hours=ttl_hours)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Private and lock-guarded: a bare ``+= 1`` is a read-modify-write and
        # loses updates under a fan-out. Read them via :meth:`stats`.
        self._role_hits = 0
        self._role_misses = 0
        self._counter_lock = threading.Lock()

    def _lock_for(self, org: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(org, threading.Lock())

    def sync_state(self, org: str) -> SyncState:
        row = (
            self._pool.get()
            .execute("SELECT last_full_sync, account_count FROM sync_state WHERE org = ?", (org,))
            .fetchone()
        )
        if row is None:
            return SyncState(org=org, last_full_sync=None, account_count=0)
        return SyncState(
            org=org,
            last_full_sync=_parse_stamp(row["last_full_sync"]),
            account_count=row["account_count"],
        )

    def is_stale(self, org: str) -> bool:
        """Return whether ``org``'s directory is past its TTL (or never synced)."""
        return self.state_is_stale(self.sync_state(org))

    def state_is_stale(self, state: SyncState) -> bool:
        """Return whether an already-read :class:`SyncState` is past its TTL.

        Callers that need both the state and its staleness - which is most of
        them, since every status surface reports "last synced" alongside
        "stale" - should read the row once and pass it here, rather than
        calling :meth:`is_stale` and :meth:`sync_state` and hitting the
        database twice for the same row. The two reads were also not atomic,
        so a refresh landing between them could report a fresh timestamp
        alongside ``stale: true``.
        """
        if state.last_full_sync is None:
            return True
        return _now_utc() - state.last_full_sync > self._ttl

    def ensure_fresh(
        self, org: str, *, region: str, access_token_getter: Callable[[], str], force: bool = False
    ) -> SyncState:
        """Refresh the cached account list for ``org`` if stale (or ``force``d).

        ``access_token_getter`` is called lazily (only if a refresh is
        actually needed) so a fresh cache never triggers an SSO call at all.
        """
        state = self.sync_state(org)
        if not force and not self.state_is_stale(state):
            return state

        lock = self._lock_for(org)
        with lock:
            # Re-check inside the lock: another thread may have just refreshed.
            state = self.sync_state(org)
            if not force and not self.state_is_stale(state):
                return state

            access_token = access_token_getter()
            logger.info("Refreshing account directory for org=%s", org)
            accounts = list_accounts(region, access_token)
            now = _now_utc().strftime(_TIME_FORMAT)

            def _write(conn: sqlite3.Connection) -> None:
                conn.execute("DELETE FROM accounts WHERE org = ?", (org,))
                # Role grants share the org-level TTL: drop them with the
                # accounts so grants added or revoked since the last sync
                # are re-fetched instead of living forever.
                conn.execute("DELETE FROM account_roles WHERE org = ?", (org,))
                conn.executemany(
                    "INSERT INTO accounts (org, account_id, account_name, discovered_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(org, a.account_id, a.account_name, now) for a in accounts],
                )
                conn.execute(
                    "INSERT INTO sync_state (org, last_full_sync, account_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(org) DO UPDATE SET last_full_sync=excluded.last_full_sync, "
                    "account_count=excluded.account_count",
                    (org, now, len(accounts)),
                )

            self._pool.with_write(_write)
            logger.info("Refreshed org=%s: %d accounts", org, len(accounts))
            return SyncState(org=org, last_full_sync=_now_utc(), account_count=len(accounts))

    def accounts_for_org(self, org: str) -> list[SsoAccount]:
        rows = (
            self._pool.get()
            .execute(
                "SELECT account_id, account_name FROM accounts WHERE org = ? ORDER BY account_name",
                (org,),
            )
            .fetchall()
        )
        return [
            SsoAccount(account_id=r["account_id"], account_name=r["account_name"]) for r in rows
        ]

    def search_accounts(
        self,
        *,
        orgs: list[str] | None = None,
        name: str | None = None,
        name_contains: str | None = None,
        account_id: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[str, SsoAccount]]:
        """Search the cached directory across one or more orgs.

        Args:
            orgs: Restrict to these org aliases. ``None`` searches every org.
            name: Exact, case-sensitive account-name match.
            name_contains: Case-insensitive substring match on account name.
            account_id: Exact account-id match.
            limit: Maximum rows to return. A directory can hold thousands of
                accounts, and an unfiltered search would otherwise dump all of
                them into model context.

        Returns:
            Matching ``(org, account)`` pairs, ordered by org then account name.

        Note:
            The substring match uses ``instr(lower(...), lower(...))`` rather
            than SQL ``LIKE``. ``LIKE`` would treat ``%`` and ``_`` in the
            user's search text as wildcards, and its case-sensitivity rules
            differ from Python's - so the same ``name_contains`` value could
            select different accounts here than in
            :func:`aws_sso_mcp.planner.resolve_targets`. ``instr`` makes both
            paths behave identically: plain, case-insensitive substring.
        """
        query = "SELECT org, account_id, account_name FROM accounts WHERE 1=1"
        params: list[object] = []
        if orgs:
            query += f" AND org IN ({','.join('?' * len(orgs))})"
            params.extend(orgs)
        if name:
            query += " AND account_name = ?"
            params.append(name)
        if name_contains:
            query += " AND instr(lower(account_name), lower(?)) > 0"
            params.append(name_contains)
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        query += " ORDER BY org, account_name"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = self._pool.get().execute(query, params).fetchall()
        return [
            (r["org"], SsoAccount(account_id=r["account_id"], account_name=r["account_name"]))
            for r in rows
        ]

    def roles_for_account(
        self, org: str, account_id: str, *, region: str, access_token: str, force: bool = False
    ) -> list[str]:
        """Return cached roles for one account, fetching+caching lazily on first use.

        Roles share the same org-level TTL: if the account itself was just
        refreshed there's no separate roles fetch needed for callers that
        pass ``force=False`` and already have a cached row.
        """
        if not force:
            rows = (
                self._pool.get()
                .execute(
                    "SELECT role_name FROM account_roles WHERE org = ? AND account_id = ?",
                    (org, account_id),
                )
                .fetchall()
            )
            if rows:
                with self._counter_lock:
                    self._role_hits += 1
                return [r["role_name"] for r in rows]

        with self._counter_lock:
            self._role_misses += 1

        # Snapshot the directory generation before the network call. A refresh
        # landing while this fetch is in flight wipes the org's role rows, and
        # writing these results afterwards would resurrect pre-refresh grants
        # over that clean slate - keeping a revoked role alive for a whole TTL.
        generation = _read_generation(self._pool.get(), org)
        roles = list_account_roles(region, access_token, account_id)
        now = _now_utc().strftime(_TIME_FORMAT)

        def _write(conn: sqlite3.Connection) -> None:
            if _read_generation(conn, org) != generation:
                logger.debug(
                    "Discarding roles for org=%s account=%s: directory refreshed mid-fetch",
                    org,
                    account_id,
                )
                return
            conn.execute(
                "DELETE FROM account_roles WHERE org = ? AND account_id = ?", (org, account_id)
            )
            conn.executemany(
                "INSERT INTO account_roles (org, account_id, role_name, discovered_at) "
                "VALUES (?, ?, ?, ?)",
                [(org, account_id, role, now) for role in roles],
            )

        self._pool.with_write(_write)
        return roles

    def invalidate_role(self, org: str, account_id: str, role: str) -> None:
        """Drop one cached role row, e.g. after it fails access-denied at call time."""

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM account_roles WHERE org = ? AND account_id = ? AND role_name = ?",
                (org, account_id, role),
            )

        self._pool.with_write(_write)

    def stats(self) -> dict[str, int]:
        """Return a consistent snapshot of per-instance counters for perf tooling.

        Read under the counter lock so the two values describe the same moment
        rather than straddling a concurrent update.
        """
        with self._counter_lock:
            return {"role_hits": self._role_hits, "role_misses": self._role_misses}
