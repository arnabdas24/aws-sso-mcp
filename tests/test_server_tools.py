"""Integration-style tests for server.py tool functions, called directly.

The @app.tool() decorator returns the original function unchanged, so these
tools can be invoked as plain Python callables in tests - no MCP transport
needed. All AWS access goes through Stubber; nothing touches live AWS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aws_sso_mcp import fanout
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

    future = (datetime.now(UTC) + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
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
        future = (datetime.now(UTC) + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
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

    future = (datetime.now(UTC) + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
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


def test_explain_role_selection_does_not_default_to_sole_grant(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["OnlyRole"],
    )
    result = server_module.explain_role_selection("111111111111", "some-account", org="org1")
    assert result["resolved"] is False
    assert result["granted_roles"] == ["OnlyRole"]
    assert "role" not in result


def test_no_tool_lets_the_caller_choose_a_role() -> None:
    """The TOML rules are the only role selector, so no tool may take a role argument."""
    import inspect

    for tool in (
        server_module.aws_query,
        server_module.run_dag,
        server_module.explain_role_selection,
    ):
        assert "role" not in inspect.signature(tool).parameters


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


def test_aws_query_allows_mutating_operation(
    sample_config_path: Path, isolated_env: Path, monkeypatch, flush_audit
) -> None:
    # IAM is the sole boundary: a mutating op is dispatched and AWS decides
    # the outcome. The audit entry is flagged write=True.
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a-dev")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["DevRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_call",
        lambda **kwargs: {"StoppingInstances": [{"InstanceId": "i-123"}]},
    )

    result = server_module.aws_query(
        service="ec2",
        operation="StopInstances",
        region="us-east-1",
        params={"InstanceIds": ["i-123"]},
        org="org1",
    )
    assert result["target_count"] == 1
    assert result["results"][0]["ok"] is True

    flush_audit(state.audit_log_path)
    import json

    entry = json.loads(state.audit_log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["operation"] == "StopInstances"
    assert entry["write"] is True


def test_aws_query_dispatches_secret_read_without_local_gate(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """Nothing is refused locally - a secret read is dispatched and IAM decides."""
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a-dev")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["DevRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_call",
        lambda **kwargs: {"Name": "prod/db", "SecretString": "s3kr3t"},
    )

    result = server_module.aws_query(
        service="secretsmanager",
        operation="GetSecretValue",
        region="us-east-1",
        params={"SecretId": "prod/db"},
        org="org1",
    )
    assert result["results"][0]["ok"] is True


def test_aws_query_multi_account_write_needs_no_confirmation(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """A mutating call spanning many accounts is dispatched; IAM is the only boundary."""
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [
            server_module_sso_account("111111111111", "acct-a-dev"),
            server_module_sso_account("222222222222", "acct-b-dev"),
        ],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["DevRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_call",
        lambda **kwargs: {"TerminatingInstances": [{"InstanceId": "i-123"}]},
    )

    result = server_module.aws_query(
        service="ec2",
        operation="TerminateInstances",
        region="us-east-1",
        params={"InstanceIds": ["i-123"]},
        org="org1",
    )
    assert result["target_count"] == 2
    assert result["ok_count"] == 2


def test_aws_query_success_across_targets(
    sample_config_path: Path, isolated_env: Path, monkeypatch, flush_audit
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [
            server_module_sso_account("111111111111", "acct-a-dev"),
            server_module_sso_account("222222222222", "acct-b-dev"),
        ],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["DevRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_call",
        lambda **kwargs: {"Reservations": [{"Instances": [{"InstanceId": "i-123"}]}]},
    )

    result = server_module.aws_query(
        service="ec2", operation="DescribeInstances", region="us-east-1", org="org1"
    )
    assert result["target_count"] == 2
    assert all(r["ok"] for r in result["results"])
    assert all(r["role"] == "DevRole" for r in result["results"])

    flush_audit(state.audit_log_path)
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
    sample_config_path: Path, isolated_env: Path, monkeypatch, flush_audit
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a-dev")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["DevRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    monkeypatch.setattr(
        "aws_sso_mcp.server.execute_call",
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
    flush_audit(state.audit_log_path)
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
    assert fanout.progress_reporter(None, "queried") is None


def test_progress_reporter_returns_none_outside_worker_thread() -> None:
    """No AnyIO worker-thread token is available in a plain pytest call - must not raise."""

    class _FakeCtx:
        pass

    assert fanout.progress_reporter(_FakeCtx(), "queried") is None


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
        "aws_sso_mcp.server.execute_call", lambda **kwargs: {"Vpcs": [{"VpcId": "vpc-1"}]}
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


# ------------------------------------------------- browser-free query guarantee


def _forbid_login(monkeypatch) -> list[str]:
    """Make any attempt to start an interactive sign-in a hard test failure.

    ``login()`` documents itself as "the only tool that will ever open a
    browser". These tests enforce that globally by replacing the device-auth
    entry point with a tripwire.
    """
    tripped: list[str] = []

    def _tripwire(state_dir, portal, **kwargs):
        tripped.append(portal.name)
        raise AssertionError(f"login() was started for {portal.name} outside the login tool")

    monkeypatch.setattr("aws_sso_mcp.sso.device_auth.login", _tripwire)
    return tripped


def test_aws_query_without_a_token_never_starts_a_browser_login(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """A query with no cached token must fail fast, not open a browser.

    Regression test for an ordering bug: ``run_fanout`` used to plan targets
    *before* the token pre-flight. Planning refreshes each org's account
    directory, that refresh needed a token, and the getter it used had
    auto-login enabled - so the common "stale cache, expired token" path
    opened a browser mid-query and blocked the tool call until the user
    noticed it. Nothing here is seeded, so the cache is stale and there is no
    token: precisely that path.
    """
    _fresh_state(sample_config_path)
    tripped = _forbid_login(monkeypatch)

    result = server_module.aws_query(
        service="ec2", operation="DescribeInstances", region="us-east-1", org="org1"
    )

    assert tripped == []
    assert "error" in result
    assert 'login(org="org1")' in result["error"]


def test_aws_query_preflight_runs_before_any_directory_refresh(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """The token check must happen before the cache is touched at all.

    Asserting on the exact pre-flight message matters as much as asserting
    that nothing was refreshed: it proves the query stopped at the token
    check rather than stumbling into the login tripwire further downstream.
    """
    _fresh_state(sample_config_path)
    _forbid_login(monkeypatch)
    refreshed: list[str] = []

    def _tracking_list_accounts(region, token):
        refreshed.append(region)
        return []

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", _tracking_list_accounts)

    result = server_module.aws_query(
        service="ec2", operation="DescribeInstances", region="us-east-1", org="org1"
    )

    assert "No valid SSO session for" in result["error"]
    assert refreshed == [], "the directory was refreshed despite there being no token"


def test_run_dag_without_a_token_never_starts_a_browser_login(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    _fresh_state(sample_config_path)
    tripped = _forbid_login(monkeypatch)

    result = server_module.run_dag(
        steps=[{"id": "a", "service": "ec2", "operation": "DescribeVpcs"}],
        region="us-east-1",
        org="org1",
    )

    assert tripped == []
    assert "error" in result
    assert "login" in result["error"]


def test_preflight_reports_an_unknown_org_as_unknown_not_as_needing_login(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """Sending a user to login() for an org that does not exist helps nobody."""
    _fresh_state(sample_config_path)
    _forbid_login(monkeypatch)

    result = server_module.aws_query(
        service="ec2", operation="DescribeInstances", region="us-east-1", org="nosuchorg"
    )

    assert "error" in result
    assert "nosuchorg" in result["error"]
    assert "login(" not in result["error"]


def test_multi_org_preflight_names_every_org_that_needs_a_login(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    _forbid_login(monkeypatch)

    result = server_module.aws_query(
        service="ec2",
        operation="DescribeInstances",
        region="us-east-1",
        orgs=["org1", "org2"],
    )

    assert "error" in result
    # org1 has a valid token, so only org2 should be called out.
    assert "org2" in result["error"]


# ------------------------------------------------------- deterministic output


def test_results_are_ordered_by_org_then_account_name(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """Results come back sorted, regardless of which account finished first.

    The executor yields outcomes in *completion* order, which varies with
    network timing, so the response used to be non-deterministic. Here the
    accounts are deliberately cached out of alphabetical order and each call
    sleeps for a duration that makes completion order the exact reverse of
    name order - so an unsorted response could not accidentally pass.
    """
    import time

    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    accounts = [
        server_module_sso_account("333333333333", "zeta"),
        server_module_sso_account("111111111111", "alpha"),
        server_module_sso_account("222222222222", "mid"),
    ]
    delay_by_account = {
        "333333333333": 0.0,
        "222222222222": 0.05,
        "111111111111": 0.1,
    }

    monkeypatch.setattr("aws_sso_mcp.cache.accounts.list_accounts", lambda region, token: accounts)
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["OnlyRole"],
    )
    # The broker's session stub carries the account id so the fake AWS call
    # can vary its duration per account.
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: kwargs["account_id"])

    def _slow_call(**kwargs):
        time.sleep(delay_by_account[kwargs["session"]])
        return {"account": kwargs["session"]}

    monkeypatch.setattr("aws_sso_mcp.server.execute_call", _slow_call)

    first = server_module.aws_query(
        service="ec2", operation="DescribeVpcs", region="us-east-1", org="org1"
    )
    second = server_module.aws_query(
        service="ec2", operation="DescribeVpcs", region="us-east-1", org="org1"
    )

    names = [r["account_name"] for r in first["results"]]
    assert names == ["alpha", "mid", "zeta"]
    assert names == [r["account_name"] for r in second["results"]]


# -------------------------------------------------------- max_items threading


def test_aws_query_max_items_overrides_the_configured_ceiling(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    """The truncation message tells users to raise max_items, so it must exist."""
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a-dev")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["DevRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    seen: list[int] = []

    def _capture(**kwargs):
        seen.append(kwargs["max_items"])
        return {"ok": True}

    monkeypatch.setattr("aws_sso_mcp.server.execute_call", _capture)

    server_module.aws_query(service="ec2", operation="DescribeVpcs", region="us-east-1", org="org1")
    assert seen == [state.config.performance.max_items_per_account]

    seen.clear()
    server_module.aws_query(
        service="ec2",
        operation="DescribeVpcs",
        region="us-east-1",
        org="org1",
        max_items=25,
    )
    assert seen == [25]


def test_run_dag_max_items_reaches_every_step(
    sample_config_path: Path, isolated_env: Path, monkeypatch
) -> None:
    state = _fresh_state(sample_config_path)
    _seed_token(state, "org1")
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_accounts",
        lambda region, token: [server_module_sso_account("111111111111", "acct-a-dev")],
    )
    monkeypatch.setattr(
        "aws_sso_mcp.cache.accounts.list_account_roles",
        lambda region, token, account_id: ["DevRole"],
    )
    monkeypatch.setattr(state.broker, "get_session", lambda **kwargs: object())
    seen: list[int] = []

    def _capture(**kwargs):
        seen.append(kwargs["max_items"])
        return {"ok": True}

    monkeypatch.setattr("aws_sso_mcp.server.execute_call", _capture)

    server_module.run_dag(
        steps=[
            {"id": "a", "service": "ec2", "operation": "DescribeVpcs"},
            {"id": "b", "service": "ec2", "operation": "DescribeSubnets"},
        ],
        region="us-east-1",
        org="org1",
        max_items=7,
    )
    assert seen == [7, 7]
