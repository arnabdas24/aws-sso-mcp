"""Lazy, 12h-threshold account/role directory cache, with single-flight refresh.

The refresh policy is intentionally simple and synchronous (no background
threads, no stale-while-revalidate): the first account-touching operation
for an org each cache-TTL window pays the full refresh cost once, then every
subsequent lookup in that window is a local SQLite read.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..sso.directory import SsoAccount, list_account_roles, list_accounts
from .db import connect

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


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
        self._conn: sqlite3.Connection = connect(db_path)
        self._ttl = timedelta(hours=ttl_hours)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # sqlite3 connections are not safe for concurrent use across threads
        # even with check_same_thread=False (that flag only disables the
        # same-thread *check*, not thread-safety) - every query/write against
        # self._conn goes through this lock.
        self._conn_lock = threading.Lock()

    def _lock_for(self, org: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.setdefault(org, threading.Lock())
        return lock

    def sync_state(self, org: str) -> SyncState:
        with self._conn_lock:
            row = self._conn.execute(
                "SELECT last_full_sync, account_count FROM sync_state WHERE org = ?", (org,)
            ).fetchone()
        if row is None:
            return SyncState(org=org, last_full_sync=None, account_count=0)
        return SyncState(
            org=org,
            last_full_sync=datetime.strptime(row["last_full_sync"], _TIME_FORMAT),
            account_count=row["account_count"],
        )

    def is_stale(self, org: str) -> bool:
        state = self.sync_state(org)
        if state.last_full_sync is None:
            return True
        return datetime.now() - state.last_full_sync > self._ttl

    def ensure_fresh(
        self, org: str, *, region: str, access_token_getter: Callable[[], str], force: bool = False
    ) -> SyncState:
        """Refresh the cached account list for ``org`` if stale (or ``force``d).

        ``access_token_getter`` is called lazily (only if a refresh is
        actually needed) so a fresh cache never triggers an SSO call at all.
        """
        if not force and not self.is_stale(org):
            return self.sync_state(org)

        lock = self._lock_for(org)
        with lock:
            # Re-check inside the lock: another thread may have just refreshed.
            if not force and not self.is_stale(org):
                return self.sync_state(org)

            access_token = access_token_getter()
            accounts = list_accounts(region, access_token)
            now = datetime.now().strftime(_TIME_FORMAT)

            with self._conn_lock:
                self._conn.execute("DELETE FROM accounts WHERE org = ?", (org,))
                self._conn.executemany(
                    "INSERT INTO accounts (org, account_id, account_name, discovered_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(org, a.account_id, a.account_name, now) for a in accounts],
                )
                self._conn.execute(
                    "INSERT INTO sync_state (org, last_full_sync, account_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(org) DO UPDATE SET last_full_sync=excluded.last_full_sync, "
                    "account_count=excluded.account_count",
                    (org, now, len(accounts)),
                )
                self._conn.commit()
            return SyncState(org=org, last_full_sync=datetime.now(), account_count=len(accounts))

    def accounts_for_org(self, org: str) -> list[SsoAccount]:
        with self._conn_lock:
            rows = self._conn.execute(
                "SELECT account_id, account_name FROM accounts WHERE org = ? ORDER BY account_name",
                (org,),
            ).fetchall()
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
    ) -> list[tuple[str, SsoAccount]]:
        """Search the cached directory across one or more orgs. Returns (org, account) pairs."""
        query = "SELECT org, account_id, account_name FROM accounts WHERE 1=1"
        params: list[str] = []
        if orgs:
            query += f" AND org IN ({','.join('?' * len(orgs))})"
            params.extend(orgs)
        if name:
            query += " AND account_name = ?"
            params.append(name)
        if name_contains:
            query += " AND account_name LIKE ?"
            params.append(f"%{name_contains}%")
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        with self._conn_lock:
            rows = self._conn.execute(query, params).fetchall()
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
            with self._conn_lock:
                rows = self._conn.execute(
                    "SELECT role_name FROM account_roles WHERE org = ? AND account_id = ?",
                    (org, account_id),
                ).fetchall()
            if rows:
                return [r["role_name"] for r in rows]

        roles = list_account_roles(region, access_token, account_id)
        now = datetime.now().strftime(_TIME_FORMAT)
        with self._conn_lock:
            self._conn.execute(
                "DELETE FROM account_roles WHERE org = ? AND account_id = ?", (org, account_id)
            )
            self._conn.executemany(
                "INSERT INTO account_roles (org, account_id, role_name, discovered_at) "
                "VALUES (?, ?, ?, ?)",
                [(org, account_id, role, now) for role in roles],
            )
            self._conn.commit()
        return roles

    def invalidate_role(self, org: str, account_id: str, role: str) -> None:
        """Drop one cached role row, e.g. after it fails access-denied at call time."""
        with self._conn_lock:
            self._conn.execute(
                "DELETE FROM account_roles WHERE org = ? AND account_id = ? AND role_name = ?",
                (org, account_id, role),
            )
            self._conn.commit()
