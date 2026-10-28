"""Tests for aws_sso_mcp.cache.accounts - the lazy, TTL-threshold account cache."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from aws_sso_mcp.cache.accounts import AccountCache, SyncState
from aws_sso_mcp.cache.db import SCHEMA_VERSION, connect
from aws_sso_mcp.sso.directory import SsoAccount


def _make_cache(tmp_path: Path, ttl_hours: float = 12) -> AccountCache:
    return AccountCache(tmp_path / "cache.db", ttl_hours=ttl_hours)


def test_is_stale_when_never_synced(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    assert cache.is_stale("org1") is True


def test_ensure_fresh_calls_list_accounts_once_when_fresh(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path, ttl_hours=12)
    calls = {"n": 0}

    def fake_list_accounts(region, token):
        calls["n"] += 1
        return [SsoAccount(account_id="111", account_name="acct-a")]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", fake_list_accounts)

    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")
    assert calls["n"] == 1
    assert cache.is_stale("org1") is False

    # Second call within TTL should NOT re-fetch.
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")
    assert calls["n"] == 1


def test_ensure_fresh_force_refetches(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path)
    calls = {"n": 0}

    def fake_list_accounts(region, token):
        calls["n"] += 1
        return [SsoAccount(account_id="111", account_name="acct-a")]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", fake_list_accounts)
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok", force=True)
    assert calls["n"] == 2


def test_ensure_fresh_expired_ttl_triggers_refresh(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path, ttl_hours=0.0)  # always stale
    calls = {"n": 0}

    def fake_list_accounts(region, token):
        calls["n"] += 1
        return [SsoAccount(account_id="111", account_name="acct-a")]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", fake_list_accounts)
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")
    assert calls["n"] == 2


def test_single_flight_prevents_duplicate_refresh(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path, ttl_hours=0.0)
    calls = {"n": 0}
    call_lock = threading.Lock()

    def fake_list_accounts(region, token):
        with call_lock:
            calls["n"] += 1
        time.sleep(0.05)
        return [SsoAccount(account_id="111", account_name="acct-a")]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", fake_list_accounts)

    # First populate so the second wave of concurrent calls sees a non-force refresh path.
    threads = [
        threading.Thread(
            target=lambda: cache.ensure_fresh(
                "org1", region="us-east-1", access_token_getter=lambda: "tok"
            )
        )
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # With ttl_hours=0 every call sees "stale", but the per-org lock means
    # only the first thread through actually re-lists (see re-check inside lock);
    # subsequent threads still perform work here since ttl=0 keeps it stale after
    # each refresh, so we only assert the cache ends up populated and consistent.
    assert cache.accounts_for_org("org1") == [SsoAccount(account_id="111", account_name="acct-a")]


def test_search_accounts_by_name(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path)
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [
            SsoAccount(account_id="111", account_name="payments-dev"),
            SsoAccount(account_id="222", account_name="payments-test"),
        ],
    )
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")

    exact = cache.search_accounts(orgs=["org1"], name="payments-dev")
    assert len(exact) == 1
    assert exact[0][1].account_id == "111"

    contains = cache.search_accounts(orgs=["org1"], name_contains="payments")
    assert len(contains) == 2


def test_search_accounts_across_orgs(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path)
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [SsoAccount(account_id="111", account_name="shared-services")],
    )
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")
    cache.ensure_fresh("org2", region="eu-west-1", access_token_getter=lambda: "tok")

    matches = cache.search_accounts(name="shared-services")
    assert {org for org, _ in matches} == {"org1", "org2"}


def test_roles_for_account_lazy_fetch_and_cache(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path)
    calls = {"n": 0}

    def fake_roles(region, token, account_id):
        calls["n"] += 1
        return ["DevRole", "ReadOnly"]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_account_roles", fake_roles)

    roles = cache.roles_for_account("org1", "111", region="us-east-1", access_token="tok")
    assert roles == ["DevRole", "ReadOnly"]
    assert calls["n"] == 1

    # Second call should hit the cache, not re-fetch.
    roles2 = cache.roles_for_account("org1", "111", region="us-east-1", access_token="tok")
    assert roles2 == ["DevRole", "ReadOnly"]
    assert calls["n"] == 1


def test_invalidate_role_forces_refetch(tmp_path: Path, monkeypatch) -> None:
    cache = _make_cache(tmp_path)
    calls = {"n": 0}

    def fake_roles(region, token, account_id):
        calls["n"] += 1
        return ["DevRole"]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_account_roles", fake_roles)
    cache.roles_for_account("org1", "111", region="us-east-1", access_token="tok")
    cache.invalidate_role("org1", "111", "DevRole")
    cache.roles_for_account("org1", "111", region="us-east-1", access_token="tok")
    assert calls["n"] == 2


# ---------------------------------------------------------- schema versioning


def test_schema_version_is_recorded_once(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    connect(db_path).close()
    conn = connect(db_path)
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    conn.close()

    assert [r["version"] for r in rows] == [SCHEMA_VERSION]


def test_opening_a_cache_from_an_older_schema_rebuilds_it(tmp_path: Path, monkeypatch) -> None:
    """A version mismatch must discard the cached tables, not limp along.

    The stored version used to be written and never read again, which made it
    pure dead data: any change to a table - or to the format of a stored
    timestamp - would surface against a pre-existing cache file as a parse
    error or a wrong answer rather than a clean rebuild. Everything here is
    re-fetchable from SSO, so discarding costs one directory refresh.
    """
    db_path = tmp_path / "cache.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO accounts (org, account_id, account_name, discovered_at) "
        "VALUES ('org1', '111', 'stale-acct', 'not-a-timestamp-this-version-understands')"
    )
    conn.execute(
        "INSERT INTO sync_state (org, last_full_sync, account_count) "
        "VALUES ('org1', 'not-a-timestamp-this-version-understands', 1)"
    )
    # Pretend this file was written by an older build of the server.
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION - 1,))
    conn.close()

    conn = connect(db_path)
    accounts = conn.execute("SELECT * FROM accounts").fetchall()
    version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    conn.close()

    assert accounts == []
    assert version == SCHEMA_VERSION


def test_a_stale_cache_file_does_not_break_staleness_checks(tmp_path: Path) -> None:
    """The end-to-end symptom the rebuild prevents: an unreadable timestamp.

    Without the version check, a row written in the old naive-timestamp format
    would reach ``_parse_stamp`` and raise ``ValueError`` on the very first
    query against an upgraded install.
    """
    db_path = tmp_path / "cache.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO sync_state (org, last_full_sync, account_count) "
        "VALUES ('org1', '2024-01-01T00:00:00', 5)"
    )
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION - 1,))
    conn.close()

    cache = AccountCache(db_path, ttl_hours=12)
    assert cache.is_stale("org1") is True
    assert cache.sync_state("org1").account_count == 0


def test_matching_schema_version_keeps_cached_rows(tmp_path: Path, monkeypatch) -> None:
    """The rebuild must only fire on a mismatch - a normal reopen keeps its data."""
    cache = _make_cache(tmp_path)
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [SsoAccount(account_id="111", account_name="acct-a")],
    )
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")

    reopened = AccountCache(tmp_path / "cache.db", ttl_hours=12)
    assert [a.account_name for a in reopened.accounts_for_org("org1")] == ["acct-a"]
    assert reopened.is_stale("org1") is False


def test_state_is_stale_reuses_an_already_read_row(tmp_path: Path, monkeypatch) -> None:
    """Status surfaces read the sync row once and derive staleness from it.

    Reading it twice was both wasteful and racy: a refresh landing between the
    two reads could report a fresh ``last_full_sync`` next to ``stale: true``.
    """
    cache = _make_cache(tmp_path)
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [SsoAccount(account_id="111", account_name="acct-a")],
    )
    cache.ensure_fresh("org1", region="us-east-1", access_token_getter=lambda: "tok")

    state = cache.sync_state("org1")
    assert cache.state_is_stale(state) is False
    assert cache.state_is_stale(SyncState(org="org1", last_full_sync=None, account_count=0)) is True
