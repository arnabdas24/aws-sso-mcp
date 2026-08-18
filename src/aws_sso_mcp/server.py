"""aws-sso-mcp entry point: MCP server exposing SSO org/account tools.

Standalone - no dependency on aws-sso-orchestrator. Session tools (org
switching, login/token status, account search, cache management) plus the
generic AWS query tool (``aws_query``) that fans a read-only AWS API call
out across every account a selector resolves to, using deterministic role
resolution and a two-lane concurrent executor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer

from .audit import append as audit_append
from .audit import new_entry as audit_new_entry
from .config import PortalConfig, load_config
from .engine.catalog import execute_read
from .engine.dag import DagError, Step
from .engine.dag import run_dag as execute_dag
from .errors import McpError, RoleResolutionError
from .hardening import cap_value
from .planner import resolve_targets
from .redact import redact, redact_value
from .roles import resolve_role
from .sso.device_auth import get_access_token
from .sso.device_auth import login as sso_login
from .sso.device_auth import token_status as sso_token_status
from .state import AppState

app = MCPServer("aws-sso-mcp")
_state: AppState | None = None


def _get_state() -> AppState:
    global _state
    if _state is None:
        _state = AppState.build(load_config())
    return _state


def _token_getter(state: AppState, portal: PortalConfig) -> Callable[[], str]:
    """Build a lazily-invoked access-token getter bound to one portal."""

    def _get() -> str:
        return get_access_token(state.state_dir, portal)

    return _get


def _org_token_getter(state: AppState) -> Callable[[str], str]:
    """Build a token getter keyed by org name, for planner.resolve_targets."""

    def _get(org_name: str) -> str:
        return get_access_token(state.state_dir, state.require_portal(org_name))

    return _get


def _safe[T](fn: Callable[[], T]) -> T | dict[str, str]:
    """Run a tool body, converting any McpError into a clean dict, never a raw traceback."""
    try:
        return fn()
    except McpError as exc:
        return {"error": redact(str(exc))}


@app.tool()
def orgs_list() -> dict[str, Any]:
    """List every org configured in the aws-sso-mcp TOML config."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        active = state.get_active_org()
        return {
            "active_org": active,
            "orgs": [
                {"name": name, "start_url": portal.start_url, "sso_region": portal.sso_region}
                for name, portal in sorted(state.config.portals.items())
            ],
        }

    return _safe(_run)


@app.tool()
def use_org(org: str) -> dict[str, Any]:
    """Set the active org for the rest of this session (does not affect one-off org= overrides)."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.set_active_org(org)
        status = sso_token_status(state.state_dir, portal.name)
        sync = state.cache.sync_state(portal.name)
        return {
            "active_org": portal.name,
            "token_valid": status.valid,
            "token_expires_at": status.expires_at,
            "cached_accounts": sync.account_count,
            "last_full_sync": sync.last_full_sync.isoformat() if sync.last_full_sync else None,
        }

    return _safe(_run)


@app.tool()
def current_org() -> dict[str, Any]:
    """Report the currently active org, if any."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        return {"active_org": state.get_active_org()}

    return _safe(_run)


@app.tool()
def org_status(org: str | None = None) -> dict[str, Any]:
    """Report token validity and cache freshness for an org (defaults to the active org)."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        status = sso_token_status(state.state_dir, portal.name)
        sync = state.cache.sync_state(portal.name)
        return {
            "org": portal.name,
            "token_valid": status.valid,
            "token_expires_at": status.expires_at,
            "token_reason": status.reason,
            "cached_accounts": sync.account_count,
            "last_full_sync": sync.last_full_sync.isoformat() if sync.last_full_sync else None,
            "cache_stale": state.cache.is_stale(portal.name),
        }

    return _safe(_run)


@app.tool()
def token_status(org: str | None = None) -> dict[str, Any]:
    """Check cached SSO token validity WITHOUT ever opening a browser."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        status = sso_token_status(state.state_dir, portal.name)
        return asdict(status)

    return _safe(_run)


@app.tool()
def login(org: str | None = None) -> dict[str, Any]:
    """Authenticate to an org via SSO device authorization (opens a browser for approval)."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        status = sso_login(state.state_dir, portal)
        return asdict(status)

    return _safe(_run)


@app.tool()
def refresh_accounts(org: str | None = None, all_orgs: bool = False) -> dict[str, Any]:
    """Force a refresh of the cached account directory for one org, or every configured org."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        targets = list(state.config.portals) if all_orgs else [state.resolve_org(org).name]
        results = []
        for name in targets:
            portal = state.require_portal(name)
            sync = state.cache.ensure_fresh(
                name,
                region=portal.sso_region,
                access_token_getter=_token_getter(state, portal),
                force=True,
            )
            results.append(
                {
                    "org": name,
                    "account_count": sync.account_count,
                    "last_full_sync": sync.last_full_sync.isoformat()
                    if sync.last_full_sync
                    else None,
                }
            )
        return {"refreshed": results}

    return _safe(_run)


@app.tool()
def cache_status(org: str | None = None) -> dict[str, Any]:
    """Report cache freshness for one org, or every configured org."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        targets = [state.resolve_org(org).name] if org is not None else list(state.config.portals)
        out = []
        for name in targets:
            sync = state.cache.sync_state(name)
            out.append(
                {
                    "org": name,
                    "account_count": sync.account_count,
                    "last_full_sync": sync.last_full_sync.isoformat()
                    if sync.last_full_sync
                    else None,
                    "stale": state.cache.is_stale(name),
                }
            )
        return {"orgs": out}

    return _safe(_run)


@app.tool()
def config_show() -> dict[str, Any]:
    """Show the resolved, loaded configuration (no secrets are ever stored in it)."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        cfg = state.config
        return {
            "source_path": str(cfg.source_path),
            "default_org": cfg.default_org,
            "cache_ttl_hours": cfg.cache_ttl_hours,
            "allow_secret_values": cfg.allow_secret_values,
            "log_level": cfg.log_level,
            "orgs": {
                name: {
                    "start_url": portal.start_url,
                    "sso_region": portal.sso_region,
                    "environments": list(portal.environments),
                }
                for name, portal in cfg.portals.items()
            },
        }

    return _safe(_run)


@app.tool()
def config_validate() -> dict[str, Any]:
    """Re-load and validate the config file, reporting any errors without applying them."""

    def _run() -> dict[str, Any]:
        cfg = load_config()
        return {"valid": True, "source_path": str(cfg.source_path), "orgs": sorted(cfg.portals)}

    return _safe(_run)


@app.tool()
def search_accounts(
    name: str | None = None,
    name_contains: str | None = None,
    account_id: str | None = None,
    org: str | None = None,
    all_orgs: bool = False,
) -> dict[str, Any]:
    """Search the cached account directory by exact name, substring, or account id.

    Refreshes the cache automatically (per-org, 12h threshold) before
    searching if it is missing or stale.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        targets = list(state.config.portals) if all_orgs else [state.resolve_org(org).name]

        for target in targets:
            portal = state.require_portal(target)
            state.cache.ensure_fresh(
                target,
                region=portal.sso_region,
                access_token_getter=_token_getter(state, portal),
            )

        matches = state.cache.search_accounts(
            orgs=targets, name=name, name_contains=name_contains, account_id=account_id
        )
        return {
            "matches": [
                {"org": org_name, "account_id": acct.account_id, "account_name": acct.account_name}
                for org_name, acct in matches
            ]
        }

    return _safe(_run)


@app.tool()
def list_roles(
    account_id: str, org: str | None = None, force_refresh: bool = False
) -> dict[str, Any]:
    """List the IAM roles granted to the caller on one account, via the account's SSO portal."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        token = get_access_token(state.state_dir, portal)
        roles = state.cache.roles_for_account(
            portal.name,
            account_id,
            region=portal.sso_region,
            access_token=token,
            force=force_refresh,
        )
        return {"org": portal.name, "account_id": account_id, "roles": roles}

    return _safe(_run)


@app.tool()
def explain_role_selection(
    account_id: str, account_name: str, org: str | None = None, role: str | None = None
) -> dict[str, Any]:
    """Dry-run deterministic role resolution for one account (no service call is made).

    Shows exactly which role would be assumed and why - explicit override,
    TOML environment mapping, or the sole granted role - without ever
    letting the model itself choose.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        token = get_access_token(state.state_dir, portal)
        granted = state.cache.roles_for_account(
            portal.name, account_id, region=portal.sso_region, access_token=token
        )
        try:
            resolution = resolve_role(
                portal=portal,
                account_id=account_id,
                account_name=account_name,
                granted_roles=granted,
                explicit_role=role,
            )
        except RoleResolutionError as exc:
            return {"resolved": False, "reason": str(exc), "granted_roles": granted}
        return {
            "resolved": True,
            "role": resolution.role,
            "source": resolution.source,
            "environment": resolution.environment,
            "granted_roles": granted,
        }

    return _safe(_run)


@app.tool()
def aws_query(
    service: str,
    operation: str,
    region: str,
    params: dict[str, Any] | None = None,
    org: str | None = None,
    orgs: list[str] | None = None,
    account_name: str | None = None,
    account_names: list[str] | None = None,
    account_id: str | None = None,
    account_ids: list[str] | None = None,
    name_contains: str | None = None,
    environment: str | None = None,
    exclude_account_ids: list[str] | None = None,
    exclude_account_names: list[str] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Run one read-only AWS API call across every account a selector resolves to.

    ``service``/``operation``/``params`` follow the AWS CLI/botocore shape,
    e.g. ``service="ec2"``, ``operation="DescribeInstances"``,
    ``params={"Filters": [{"Name": "instance-state-name", "Values": ["stopped"]}]}``.
    Only ``Describe*``/``List*``/``Get*``/``Head*`` (plus a small explicit
    allowlist) are permitted; value-returning secret reads additionally
    require ``allow_secret_values = true`` in config. ``region`` is always
    required and never defaulted. Account selectors (``org``/``orgs``/
    ``account_name``/``account_names``/``account_id``/``account_ids``/
    ``name_contains``/``environment``/exclusions) resolve which accounts
    are touched; if none are given, every account in the active org is
    used. The model never chooses which IAM role to assume - pass
    ``role=`` to override the deterministic TOML/sole-grant resolution.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        call_params = params or {}

        no_selectors_given = not any(
            [
                org,
                orgs,
                account_name,
                account_names,
                account_id,
                account_ids,
                name_contains,
                environment,
            ]
        )
        # With zero selectors, default to the active org only - never silently
        # fan out across every configured org (hundreds of accounts) by accident.
        effective_org = state.resolve_org(None).name if no_selectors_given else org

        targets = resolve_targets(
            config=state.config,
            cache=state.cache,
            access_token_getter=_org_token_getter(state),
            org=effective_org,
            orgs=orgs,
            account_name=account_name,
            account_names=account_names,
            account_id=account_id,
            account_ids=account_ids,
            name_contains=name_contains,
            environment=environment,
            exclude_account_ids=exclude_account_ids,
            exclude_account_names=exclude_account_names,
        )
        if not targets:
            return {
                "target_count": 0,
                "results": [],
                "warning": "no accounts matched the given selectors",
            }

        resolution_by_key: dict[tuple[str, str], Any] = {}

        def credential_fn(t_org: str, t_account_id: str, t_account_name: str) -> Any:
            portal = state.require_portal(t_org)
            token = get_access_token(state.state_dir, portal)
            granted = state.cache.roles_for_account(
                t_org, t_account_id, region=portal.sso_region, access_token=token
            )
            resolution = resolve_role(
                portal=portal,
                account_id=t_account_id,
                account_name=t_account_name,
                granted_roles=granted,
                explicit_role=role,
            )
            resolution_by_key[(t_org, t_account_id)] = resolution
            return state.broker.get_session(
                org=t_org,
                region=portal.sso_region,
                account_id=t_account_id,
                role=resolution.role,
                access_token=token,
            )

        def service_fn(
            t_org: str, t_account_id: str, t_account_name: str, session: Any
        ) -> dict[str, Any]:
            return execute_read(
                session=session,
                region=region,
                service=service,
                operation=operation,
                params=call_params,
                allow_secret_values=state.config.allow_secret_values,
            )

        fanout_targets = [(t.org, t.account_id, t.account_name) for t in targets]
        outcomes = state.executor.fan_out(
            fanout_targets, credential_fn=credential_fn, service_fn=service_fn
        )

        results: list[dict[str, Any]] = []
        for outcome in outcomes:
            resolution = resolution_by_key.get((outcome.org, outcome.account_id))
            audit_append(
                state.audit_log_path,
                audit_new_entry(
                    org=outcome.org,
                    account_id=outcome.account_id,
                    account_name=outcome.account_name,
                    role=resolution.role if resolution else None,
                    role_source=resolution.source if resolution else None,
                    region=region,
                    service=service,
                    operation=operation,
                    ok=outcome.ok,
                    error=outcome.error,
                ),
            )
            if outcome.ok:
                results.append(
                    {
                        "org": outcome.org,
                        "account_id": outcome.account_id,
                        "account_name": outcome.account_name,
                        "role": resolution.role if resolution else None,
                        "ok": True,
                        "data": cap_value(redact_value(outcome.value)),
                    }
                )
            else:
                results.append(
                    {
                        "org": outcome.org,
                        "account_id": outcome.account_id,
                        "account_name": outcome.account_name,
                        "ok": False,
                        "error": redact(outcome.error or "unknown error"),
                    }
                )
        return {"target_count": len(results), "results": results}

    return _safe(_run)


@app.tool()
def run_dag(
    steps: list[dict[str, Any]],
    region: str,
    org: str | None = None,
    orgs: list[str] | None = None,
    account_name: str | None = None,
    account_names: list[str] | None = None,
    account_id: str | None = None,
    account_ids: list[str] | None = None,
    name_contains: str | None = None,
    environment: str | None = None,
    exclude_account_ids: list[str] | None = None,
    exclude_account_names: list[str] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Run a small declarative multi-step query DAG against every resolved account.

    Each entry in ``steps`` is ``{"id": ..., "service": ..., "operation": ...,
    "params": {...}, "extract": "...", "depends_on": [...]}``.
    A later step's ``params`` may reference an earlier step's extracted
    output anywhere via ``{"$ref": "<step_id>"}`` - there is no expression
    language and nothing is ever evaluated as code. Each resolved account
    runs its own independent copy of the whole DAG (one credential mint per
    account, not per step). Same read-only/secret-value rules as
    ``aws_query`` apply to every step.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        try:
            parsed_steps = [
                Step(
                    id=s["id"],
                    service=s["service"],
                    operation=s["operation"],
                    params=s.get("params", {}),
                    extract=s.get("extract"),
                    depends_on=tuple(s.get("depends_on", ())),
                )
                for s in steps
            ]
        except KeyError as exc:
            return {"error": f"each step requires an 'id'/'service'/'operation': missing {exc}"}

        no_selectors_given = not any(
            [
                org,
                orgs,
                account_name,
                account_names,
                account_id,
                account_ids,
                name_contains,
                environment,
            ]
        )
        effective_org = state.resolve_org(None).name if no_selectors_given else org

        targets = resolve_targets(
            config=state.config,
            cache=state.cache,
            access_token_getter=_org_token_getter(state),
            org=effective_org,
            orgs=orgs,
            account_name=account_name,
            account_names=account_names,
            account_id=account_id,
            account_ids=account_ids,
            name_contains=name_contains,
            environment=environment,
            exclude_account_ids=exclude_account_ids,
            exclude_account_names=exclude_account_names,
        )
        if not targets:
            return {
                "target_count": 0,
                "results": [],
                "warning": "no accounts matched the given selectors",
            }

        resolution_by_key: dict[tuple[str, str], Any] = {}

        def credential_fn(t_org: str, t_account_id: str, t_account_name: str) -> Any:
            portal = state.require_portal(t_org)
            token = get_access_token(state.state_dir, portal)
            granted = state.cache.roles_for_account(
                t_org, t_account_id, region=portal.sso_region, access_token=token
            )
            resolution = resolve_role(
                portal=portal,
                account_id=t_account_id,
                account_name=t_account_name,
                granted_roles=granted,
                explicit_role=role,
            )
            resolution_by_key[(t_org, t_account_id)] = resolution
            return state.broker.get_session(
                org=t_org,
                region=portal.sso_region,
                account_id=t_account_id,
                role=resolution.role,
                access_token=token,
            )

        def service_fn(
            t_org: str, t_account_id: str, t_account_name: str, session: Any
        ) -> dict[str, Any]:
            def call(step: Step, resolved_params: dict[str, Any]) -> dict[str, Any]:
                response = execute_read(
                    session=session,
                    region=region,
                    service=step.service,
                    operation=step.operation,
                    params=resolved_params,
                    allow_secret_values=state.config.allow_secret_values,
                )
                audit_append(
                    state.audit_log_path,
                    audit_new_entry(
                        org=t_org,
                        account_id=t_account_id,
                        account_name=t_account_name,
                        role=resolution_by_key[(t_org, t_account_id)].role,
                        role_source=resolution_by_key[(t_org, t_account_id)].source,
                        region=region,
                        service=step.service,
                        operation=step.operation,
                        ok=True,
                    ),
                )
                return response

            try:
                return execute_dag(parsed_steps, call=call)
            except DagError as exc:
                return {"dag_error": str(exc)}

        fanout_targets = [(t.org, t.account_id, t.account_name) for t in targets]
        outcomes = state.executor.fan_out(
            fanout_targets, credential_fn=credential_fn, service_fn=service_fn
        )

        results: list[dict[str, Any]] = []
        for outcome in outcomes:
            if outcome.ok:
                results.append(
                    {
                        "org": outcome.org,
                        "account_id": outcome.account_id,
                        "account_name": outcome.account_name,
                        "ok": True,
                        "data": cap_value(redact_value(outcome.value)),
                    }
                )
            else:
                audit_append(
                    state.audit_log_path,
                    audit_new_entry(
                        org=outcome.org,
                        account_id=outcome.account_id,
                        account_name=outcome.account_name,
                        role=None,
                        role_source=None,
                        region=region,
                        service="(dag)",
                        operation="(dag)",
                        ok=False,
                        error=outcome.error,
                    ),
                )
                results.append(
                    {
                        "org": outcome.org,
                        "account_id": outcome.account_id,
                        "account_name": outcome.account_name,
                        "ok": False,
                        "error": redact(outcome.error or "unknown error"),
                    }
                )
        return {"target_count": len(results), "results": results}

    return _safe(_run)


def main() -> None:
    """Console-script entry point (registered as ``aws-sso-mcp``)."""
    app.run()


if __name__ == "__main__":
    main()
