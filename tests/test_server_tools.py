"""Integration-style tests for server.py tool functions, called directly.

The @app.tool() decorator returns the original function unchanged, so these
tools can be invoked as plain Python callables in tests - no MCP transport
needed. All AWS access goes through Stubber; nothing touches live AWS.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from aws_sso_mcp import server as server_module
from aws_sso_mcp.config import load_config
from aws_sso_mcp.state import AppState


def _fresh_state(sample_config_path: Path) -> AppState:
    state = AppState.build(load_config(sample_config_path))
    server_module._state = state
    return state


def test_orgs_list(sample_config_path: Path, isolated_env: Path) -> None:
    _fresh_state(sample_config_path)
    result = server_module.orgs_list()
    assert result["active_org"] == "org1"
    names = {o["name"] for o in result["orgs"]}
    assert names == {"org1", "org2"}


def test_use_org_and_current_org(sample_config_path: Path, isolated_env: Path) -> None:
    _fresh_state(sample_config_path)
    result = server_module.use_org("org2")
    assert result["active_org"] == "org2"
    assert server_module.current_org()["active_org"] == "org2"


def test_use_org_unknown_returns_clean_error(sample_config_path: Path, isolated_env: Path) -> None:
    _fresh_state(sample_config_path)
    result = server_module.use_org("nope")
    assert "error" in result
    assert "nope" in result["error"]


def test_config_show_and_validate(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AWS_SSO_MCP_CONFIG", str(sample_config_path))
    _fresh_state(sample_config_path)
    shown = server_module.config_show()
    assert shown["default_org"] == "org1"
    assert set(shown["orgs"]) == {"org1", "org2"}

    validated = server_module.config_validate()
    assert validated["valid"] is True


def test_token_status_no_login_yet(sample_config_path: Path, isolated_env: Path) -> None:
    _fresh_state(sample_config_path)
    result = server_module.token_status("org1")
    assert result["valid"] is False


def test_search_accounts_triggers_refresh_and_finds(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)

    # Pretend a valid token is already cached so search_accounts doesn't try
    # to open a browser.
    from aws_sso_mcp.sso import device_auth

    future = (datetime.now() + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
    token_path = device_auth.token_cache_path(state.state_dir, "org1")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    token_path.write_text(
        json.dumps({"access_token": "tok", "expires_at": future}), encoding="utf-8"
    )
    token_path.chmod(0o600)

    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [
            server_module_sso_account("111", "payments-dev"),
        ],
    )

    result = server_module.search_accounts(name="payments-dev", org="org1")
    assert result["matches"][0]["account_id"] == "111"
    assert result["matches"][0]["org"] == "org1"


def server_module_sso_account(account_id: str, account_name: str):
    from aws_sso_mcp.sso.directory import SsoAccount

    return SsoAccount(account_id=account_id, account_name=account_name)


def test_refresh_accounts_all_orgs(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)

    from aws_sso_mcp.sso import device_auth

    for org in ("org1", "org2"):
        future = (datetime.now() + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
        token_path = device_auth.token_cache_path(state.state_dir, org)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        token_path.write_text(
            json.dumps({"access_token": "tok", "expires_at": future}), encoding="utf-8"
        )

    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("1", "a")],
    )

    result = server_module.refresh_accounts(all_orgs=True)
    orgs_refreshed = {r["org"] for r in result["refreshed"]}
    assert orgs_refreshed == {"org1", "org2"}


def test_cache_status(sample_config_path: Path, isolated_env: Path) -> None:
    _fresh_state(sample_config_path)
    result = server_module.cache_status()
    assert {o["org"] for o in result["orgs"]} == {"org1", "org2"}
    assert all(o["stale"] for o in result["orgs"])
