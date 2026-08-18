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


def _seed_token(state: AppState, org: str) -> None:
    import json

    from aws_sso_mcp.sso import device_auth

    future = (datetime.now() + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
    token_path = device_auth.token_cache_path(state.state_dir, org)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps({"access_token": "tok", "expires_at": future}), encoding="utf-8"
    )
    token_path.chmod(0o600)


def test_list_roles(sample_config_path: Path, isolated_env: Path, monkeypatch) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["ReadOnly", "AdminRole"],
    )
    result = server_module.list_roles("111111111111", org="org1")
    assert result["roles"] == ["ReadOnly", "AdminRole"]


def test_explain_role_selection_sole_grant(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["OnlyRole"],
    )
    result = server_module.explain_role_selection("111111111111", "some-account", org="org1")
    assert result["resolved"] is True
    assert result["role"] == "OnlyRole"
    assert result["source"] == "sole-grant"


def test_explain_role_selection_ambiguous_returns_reason(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["RoleA", "RoleB"],
    )
    result = server_module.explain_role_selection("111111111111", "some-account", org="org1")
    assert result["resolved"] is False
    assert "RoleA" in result["reason"] or result["granted_roles"] == ["RoleA", "RoleB"]


def test_aws_query_denies_mutating_operation(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["OnlyRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())

    result = server_module.aws_query(
        service="ec2", operation="StopInstances", region="us-east-1", org="org1"
    )
    assert "error" in result
    assert "not a recognized read-only operation" in result["error"]


def test_aws_query_success_across_targets(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [
            server_module_sso_account("111111111111", "acct-a"),
            server_module_sso_account("222222222222", "acct-b"),
        ],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["OnlyRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_read",
        lambda **kwargs: {"Reservations": [{"Instances": [{"InstanceId": "i-123"}]}]},
    )

    result = server_module.aws_query(
        service="ec2", operation="DescribeInstances", region="us-east-1", org="org1"
    )
    assert result["target_count"] == 2
    assert all(r["ok"] for r in result["results"])
    assert all(r["role"] == "OnlyRole" for r in result["results"])

    audit_lines = state.audit_log_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 2


def test_aws_query_no_targets_returns_warning(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", lambda region, token: [])

    result = server_module.aws_query(
        service="ec2", operation="DescribeInstances", region="us-east-1", org="org1"
    )
    assert result["target_count"] == 0
    assert "warning" in result


def test_run_dag_success_single_step(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["OnlyRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_read",
        lambda **kwargs: {"Vpcs": [{"VpcId": "vpc-1"}]},
    )

    result = server_module.run_dag(
        steps=[{"id": "a", "service": "ec2", "operation": "DescribeVpcs"}],
        region="us-east-1",
        org="org1",
    )
    assert result["target_count"] == 1
    assert result["results"][0]["ok"] is True
    assert result["results"][0]["data"]["steps"]["a"]["extracted"] == {"Vpcs": [{"VpcId": "vpc-1"}]}
    audit_lines = state.audit_log_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1


def test_run_dag_missing_field_returns_clean_error(
    sample_config_path: Path, isolated_env: Path
) -> None:
    _fresh_state(sample_config_path)
    result = server_module.run_dag(
        steps=[{"id": "a", "service": "ec2"}], region="us-east-1", org="org1"
    )
    assert "error" in result


def test_run_dag_no_targets_returns_warning(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", lambda region, token: [])

    result = server_module.run_dag(
        steps=[{"id": "a", "service": "ec2", "operation": "DescribeVpcs"}],
        region="us-east-1",
        org="org1",
    )
    assert result["target_count"] == 0
    assert "warning" in result


def test_progress_reporter_returns_none_without_context() -> None:
    assert server_module._progress_reporter(None, "queried") is None


def test_progress_reporter_returns_none_outside_worker_thread() -> None:
    """No AnyIO worker-thread token is available in a plain pytest call - must not raise."""

    class _FakeCtx:
        pass

    reporter = server_module._progress_reporter(_FakeCtx(), "queried")  # type: ignore[arg-type]
    assert reporter is None


def test_aws_query_reports_progress_via_context(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """When a real AnyIO worker-thread token IS available, progress is reported per target."""
    import anyio
    import anyio.to_thread

    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda account_id, region, token: ["Role1"],
    )
    monkeypatch.setattr("aws_sso_mcp.server.get_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(state.broker, "get_session", lambda **k: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_read", lambda **kwargs: {"Vpcs": [{"VpcId": "vpc-1"}]}
    )

    reported: list[tuple[float, float, str | None]] = []

    class _FakeCtx:
        async def report_progress(
            self, progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            reported.append((progress, total or 0.0, message))

    def _run_in_worker() -> dict:
        return server_module.aws_query(
            service="ec2", operation="DescribeVpcs", region="us-east-1", org="org1", ctx=_FakeCtx()
        )

    # aws_query is a plain sync function; call it from inside an AnyIO worker
    # thread (anyio.to_thread.run_sync), exactly how MCPServer invokes it, so a
    # real event-loop token exists for anyio.from_thread.current_token().
    async def _drive() -> dict:
        return await anyio.to_thread.run_sync(_run_in_worker)

    result = anyio.run(_drive)

    assert result["target_count"] == 1
    assert reported == [(1.0, 1.0, "queried 1/1 accounts")]
