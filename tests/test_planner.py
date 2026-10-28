"""Tests for aws_sso_mcp.planner - account selector resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from aws_sso_mcp.cache.accounts import AccountCache
from aws_sso_mcp.config import Config, EnvironmentRule, PortalConfig
from aws_sso_mcp.errors import AccountNotFoundError, AmbiguousAccountError
from aws_sso_mcp.planner import resolve_targets
from aws_sso_mcp.sso.directory import SsoAccount


def _config(tmp_path: Path) -> Config:
    return Config(
        portals={
            "org1": PortalConfig(
                name="org1",
                start_url="https://example.awsapps.com/start",
                sso_region="us-east-1",
                environments={
                    "dev": EnvironmentRule(name="dev", match="endswith", patterns=("-dev",)),
                },
            ),
            "org2": PortalConfig(
                name="org2",
                start_url="https://example2.awsapps.com/start",
                sso_region="eu-west-1",
            ),
        },
        default_org="org1",
        cache_ttl_hours=12,
        log_level="INFO",
        source_path=tmp_path / "config.toml",
    )


def _seeded_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AccountCache:
    cache = AccountCache(tmp_path / "cache.db", ttl_hours=12)
    accounts_by_org = {
        "org1": [
            SsoAccount(account_id="111111111111", account_name="billing-dev"),
            SsoAccount(account_id="222222222222", account_name="billing-prd"),
        ],
        "org2": [
            SsoAccount(account_id="333333333333", account_name="platform-dev"),
        ],
    }

    def fake_list_accounts(region: str, token: str) -> list[SsoAccount]:
        # region uniquely identifies the org in this fixture (us-east-1 -> org1).
        org = "org1" if region == "us-east-1" else "org2"
        return accounts_by_org[org]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", fake_list_accounts)
    return cache


def test_resolve_all_accounts_in_one_org(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    targets = resolve_targets(
        config=config, cache=cache, access_token_getter=lambda org: "tok", org="org1"
    )
    assert {t.account_name for t in targets} == {"billing-dev", "billing-prd"}
    assert all(t.org == "org1" for t in targets)


def test_resolve_cross_org_search_by_name_contains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    targets = resolve_targets(
        config=config, cache=cache, access_token_getter=lambda org: "tok", name_contains="dev"
    )
    assert {(t.org, t.account_name) for t in targets} == {
        ("org1", "billing-dev"),
        ("org2", "platform-dev"),
    }


def test_resolve_by_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    targets = resolve_targets(
        config=config,
        cache=cache,
        access_token_getter=lambda org: "tok",
        org="org1",
        environment="dev",
    )
    assert [t.account_name for t in targets] == ["billing-dev"]


def test_resolve_by_multiple_account_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    targets = resolve_targets(
        config=config,
        cache=cache,
        access_token_getter=lambda org: "tok",
        account_names=["billing-dev", "platform-dev"],
    )
    assert {t.account_name for t in targets} == {"billing-dev", "platform-dev"}


def test_resolve_excludes_account_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    targets = resolve_targets(
        config=config,
        cache=cache,
        access_token_getter=lambda org: "tok",
        org="org1",
        exclude_account_ids=["222222222222"],
    )
    assert [t.account_id for t in targets] == ["111111111111"]


def test_resolve_not_found_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    with pytest.raises(AccountNotFoundError):
        resolve_targets(
            config=config,
            cache=cache,
            access_token_getter=lambda org: "tok",
            account_name="does-not-exist",
        )


def test_resolve_ambiguous_name_across_orgs_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    # Force a name collision across orgs to exercise ambiguity detection.
    accounts_by_org_dupe = {
        "org1": [SsoAccount(account_id="111111111111", account_name="shared-name")],
        "org2": [SsoAccount(account_id="333333333333", account_name="shared-name")],
    }

    def fake_list_accounts(region: str, token: str) -> list[SsoAccount]:
        org = "org1" if region == "us-east-1" else "org2"
        return accounts_by_org_dupe[org]

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", fake_list_accounts)

    with pytest.raises(AmbiguousAccountError):
        resolve_targets(
            config=config,
            cache=cache,
            access_token_getter=lambda org: "tok",
            account_name="shared-name",
        )


def test_resolve_force_refresh_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    cache = _seeded_cache(tmp_path, monkeypatch)
    calls = {"n": 0}
    orig_ensure_fresh = cache.ensure_fresh

    def counting_ensure_fresh(*args, **kwargs):
        calls["n"] += 1
        return orig_ensure_fresh(*args, **kwargs)

    monkeypatch.setattr(cache, "ensure_fresh", counting_ensure_fresh)
    resolve_targets(
        config=config,
        cache=cache,
        access_token_getter=lambda org: "tok",
        org="org1",
        force_refresh=True,
    )
    assert calls["n"] == 1
