"""aws-sso-mcp entry point: the MCP tools exposed to a chat client.

This module is deliberately thin. Each tool validates its own arguments, then
delegates: session and cache tools to :class:`~aws_sso_mcp.state.AppState`, and
the two account-touching tools (:func:`aws_query` and :func:`run_dag`) to the
shared pipeline in :mod:`aws_sso_mcp.fanout`. All the machinery those two have
in common - scope resolution, blast-radius limits, SSO pre-flight, role warm-up,
concurrency, retries, auditing, and output shaping - lives in that one place
rather than being duplicated here.

Authorization model: AWS IAM is the sole boundary. Any operation the assumed
role can perform, read or mutating, is dispatched and AWS decides the outcome.
Nothing is refused locally; a role without permission simply gets AccessDenied
back from AWS, surfaced as a clean error.
"""

from __future__ import annotations

import argparse
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from . import __version__
from .config import PortalConfig, load_config
from .engine.catalog import execute_call
from .engine.dag import DagError, Step
from .engine.dag import run_dag as execute_dag
from .errors import McpError, RoleResolutionError
from .fanout import AccountContext, AccountSelector, run_fanout
from .logging_setup import bind_context, get_logger
from .logging_setup import configure as configure_logging
from .redact import redact
from .roles import resolve_role
from .sso.device_auth import get_access_token
from .sso.device_auth import login as sso_login
from .sso.device_auth import token_status as sso_token_status
from .state import AppState

logger = get_logger(__name__)

app = MCPServer("aws-sso-mcp")

# The server runs as one process per client session, so a module-level
# singleton is the correctly-scoped place for runtime state: it is created on
# the first tool call and disappears when the session ends.
_state: AppState | None = None
_state_lock = threading.Lock()


def _get_state() -> AppState:
    """Return the process-wide :class:`AppState`, building it on first use.

    Construction is guarded by a lock because MCP dispatches tool calls onto a
    worker-thread pool: two calls arriving together would otherwise each build
    their own state, producing two SQLite connection pools and two credential
    brokers that silently fail to share any cached work.
    """
    global _state
    if _state is not None:
        return _state

    with _state_lock:
        # Re-check inside the lock: another thread may have just built it.
        if _state is None:
            cfg = load_config()
            configure_logging(cfg.log_level)
            logger.info(
                "aws-sso-mcp starting: orgs=%s default=%s cache_ttl=%.1fh",
                sorted(cfg.portals),
                cfg.default_org,
                cfg.cache_ttl_hours,
            )
            _state = AppState.build(cfg)
        return _state


def _safe(fn: Callable[[], dict[str, Any]], ctx: Context | None = None) -> dict[str, Any]:
    """Run a tool body, converting any failure into a clean error dict.

    Two things happen here:

    * The MCP ``Context`` is bound to a contextvar for the duration of the
      call, so ordinary ``logger`` calls anywhere in the stack are forwarded to
      the user's chat UI.
    * Every exception is caught. :class:`McpError` subclasses are already
      user-facing and are returned as-is. Anything else is unexpected, so it is
      logged with a full traceback for the operator and returned to the client
      as a short, redacted message - an unhandled exception would otherwise
      surface as a raw traceback that both reads badly and risks echoing
      internal details back to the model.

    Args:
        fn: The tool body to run.
        ctx: The MCP ``Context`` for this call, if the tool accepts one.

    Returns:
        Either the tool's result or ``{"error": ...}``.
    """
    with bind_context(ctx):
        try:
            return fn()
        except McpError as exc:
            logger.warning("Tool returned error: %s", exc)
            return {"error": redact(str(exc))}
        except Exception as exc:
            # Deliberately broad: this is the MCP tool boundary. An unexpected
            # exception escaping here would surface to the client as a
            # protocol-level failure with a raw traceback, so everything is
            # caught, logged with its traceback, and returned as data.
            logger.exception("Unexpected failure in tool")
            return {
                "error": redact(f"Unexpected {type(exc).__name__}: {exc}"),
                "hint": "This is a bug in aws-sso-mcp; the server log has the full traceback.",
            }


def _selector(
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
) -> AccountSelector:
    """Collect the account-selector arguments shared by the fan-out tools."""
    return AccountSelector(
        org=org,
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


def _token_getter(state: AppState, portal: PortalConfig) -> Callable[[], str]:
    """Build a lazily-invoked access-token getter bound to one portal.

    Used by cache refreshes, which should only trigger an SSO lookup if the
    cache actually turns out to be stale.

    ``auto_login=False`` keeps :func:`login` the only tool that can ever open a
    browser: a tool that silently started a device-authorization flow would
    block for as long as it took the user to notice and approve it. Callers
    get :class:`NoTokenError` naming the ``login()`` call to make instead.
    """

    def _get() -> str:
        return get_access_token(state.state_dir, portal, auto_login=False)

    return _get


# ---------------------------------------------------------------- session tools


@app.tool()
def orgs_list() -> dict[str, Any]:
    """List every org configured in the aws-sso-mcp TOML config."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        return {
            "active_org": state.get_active_org(),
            "orgs": [
                {"name": name, "start_url": portal.start_url, "sso_region": portal.sso_region}
                for name, portal in sorted(state.config.portals.items())
            ],
        }

    return _safe(_run)


@app.tool()
def use_org(org: str) -> dict[str, Any]:
    """Set the active org for this session (one-off org= overrides still win)."""

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
    return _safe(lambda: {"active_org": _get_state().get_active_org()})


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
            "cache_stale": state.cache.state_is_stale(sync),
        }

    return _safe(_run)


@app.tool()
def token_status(org: str | None = None) -> dict[str, Any]:
    """Check cached SSO token validity WITHOUT ever opening a browser."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        return asdict(sso_token_status(state.state_dir, state.resolve_org(org).name))

    return _safe(_run)


@app.tool()
def login(org: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Authenticate to an org via SSO device authorization (opens a browser for approval).

    This is the only tool that will ever open a browser. Queries never start a
    sign-in on your behalf; they fail with a message telling you to call this
    first, so a browser window never appears unexpectedly mid-query.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        logger.info("Starting device-auth login for org=%s", portal.name)
        return asdict(sso_login(state.state_dir, portal))

    return _safe(_run, ctx)


# ------------------------------------------------------------- directory tools


@app.tool()
def refresh_accounts(
    org: str | None = None, all_orgs: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Force a refresh of the cached account directory for one org, or every configured org."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        targets = list(state.config.portals) if all_orgs else [state.resolve_org(org).name]
        refreshed = []
        for name in targets:
            portal = state.require_portal(name)
            sync = state.cache.ensure_fresh(
                name,
                region=portal.sso_region,
                access_token_getter=_token_getter(state, portal),
                force=True,
            )
            refreshed.append(
                {
                    "org": name,
                    "account_count": sync.account_count,
                    "last_full_sync": sync.last_full_sync.isoformat()
                    if sync.last_full_sync
                    else None,
                }
            )
        return {"refreshed": refreshed}

    return _safe(_run, ctx)


@app.tool()
def cache_status(org: str | None = None) -> dict[str, Any]:
    """Report cache freshness for one org, or every configured org."""

    def _describe(state: AppState, name: str) -> dict[str, Any]:
        """Summarise one org's cache, reading its sync row exactly once."""
        sync = state.cache.sync_state(name)
        last_sync = sync.last_full_sync
        return {
            "org": name,
            "account_count": sync.account_count,
            "last_full_sync": last_sync.isoformat() if last_sync is not None else None,
            "stale": state.cache.state_is_stale(sync),
        }

    def _run() -> dict[str, Any]:
        state = _get_state()
        targets = [state.resolve_org(org).name] if org is not None else list(state.config.portals)
        return {"orgs": [_describe(state, name) for name in targets]}

    return _safe(_run)


@app.tool()
def search_accounts(
    name: str | None = None,
    name_contains: str | None = None,
    account_id: str | None = None,
    org: str | None = None,
    all_orgs: bool = False,
    limit: int = 200,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Search the cached account directory by exact name, substring, or account id.

    The cache is refreshed automatically per org if it is missing or older than
    the configured TTL. ``name_contains`` is a case-insensitive substring and
    behaves identically to the ``name_contains`` selector on ``aws_query``, so
    what you find here is exactly what a query will target.
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
            orgs=targets,
            name=name,
            name_contains=name_contains,
            account_id=account_id,
            # Fetch one extra row so we can tell "exactly at the limit" from
            # "there were more", without a second COUNT query.
            limit=limit + 1,
        )
        truncated = len(matches) > limit
        response: dict[str, Any] = {
            "matches": [
                {"org": org_name, "account_id": acct.account_id, "account_name": acct.account_name}
                for org_name, acct in matches[:limit]
            ],
            "truncated": truncated,
        }
        if truncated:
            response["warning"] = (
                f"More than {limit} accounts matched; narrow the search or raise limit."
            )
        return response

    return _safe(_run, ctx)


@app.tool()
def list_roles(
    account_id: str, org: str | None = None, force_refresh: bool = False
) -> dict[str, Any]:
    """List the IAM roles granted to you on one account, via that account's SSO portal."""

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        roles = state.cache.roles_for_account(
            portal.name,
            account_id,
            region=portal.sso_region,
            access_token=get_access_token(state.state_dir, portal, auto_login=False),
            force=force_refresh,
        )
        return {"org": portal.name, "account_id": account_id, "roles": roles}

    return _safe(_run)


@app.tool()
def explain_role_selection(
    account_id: str, account_name: str, org: str | None = None
) -> dict[str, Any]:
    """Dry-run deterministic role resolution for one account; makes no service call.

    Shows exactly which role the TOML environment rules would assume. The role is
    never chosen by the model or by the caller, and there is no override argument.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        portal = state.resolve_org(org)
        granted = state.cache.roles_for_account(
            portal.name,
            account_id,
            region=portal.sso_region,
            access_token=get_access_token(state.state_dir, portal, auto_login=False),
        )
        try:
            resolution = resolve_role(
                portal=portal,
                account_id=account_id,
                account_name=account_name,
                granted_roles=granted,
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


# ------------------------------------------------------------- the query tools


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
    max_accounts: int | None = None,
    max_items: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run one AWS API call across every account a selector resolves to.

    ``service``/``operation``/``params`` follow the AWS CLI and botocore shape,
    e.g. ``service="ec2"``, ``operation="DescribeInstances"``,
    ``params={"Filters": [{"Name": "instance-state-name", "Values": ["stopped"]}]}``.
    Paginated operations are followed to completion automatically, so results
    are complete rather than a silent first page. If a result is truncated,
    raise ``max_items`` to collect more from each account.

    ``region`` is always required and never defaulted. Account selectors decide
    which accounts are touched; with none given, every account in the active org
    is targeted, subject to the configured blast-radius cap. The model never
    picks which IAM role to assume, and neither does the caller: the role comes only
    from the ``[portals.<org>.environments.*]`` rules in your TOML config.

    Any operation the assumed role may perform is dispatched - reads, writes,
    deletes, secret reads, all of it - with AWS IAM as the sole authorization
    boundary. Nothing is refused locally; if the role lacks permission, AWS
    returns AccessDenied and that is what comes back.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        call_params = params or {}

        def _call(account: AccountContext) -> dict[str, Any]:
            """Run the single API call for one account, recording it for the audit log."""
            try:
                response = execute_call(
                    session=account.session,
                    region=account.region,
                    service=service,
                    operation=operation,
                    params=call_params,
                    max_items=account.max_items,
                )
            except McpError as exc:
                account.record(service, operation, ok=False, error=str(exc))
                raise
            account.record(service, operation, ok=True)
            return response

        logger.info("aws_query %s:%s region=%s", service, operation, region)
        return run_fanout(
            state,
            selector=_selector(
                org=org,
                orgs=orgs,
                account_name=account_name,
                account_names=account_names,
                account_id=account_id,
                account_ids=account_ids,
                name_contains=name_contains,
                environment=environment,
                exclude_account_ids=exclude_account_ids,
                exclude_account_names=exclude_account_names,
            ),
            region=region,
            per_account=_call,
            audit_label=(service, operation),
            verb="queried",
            ctx=ctx,
            max_accounts=max_accounts,
            max_items=max_items,
        )

    return _safe(_run, ctx)


def _parse_steps(steps: list[dict[str, Any]]) -> list[Step]:
    """Turn raw step dicts from the client into validated :class:`Step` objects.

    Args:
        steps: Raw step dicts as supplied to :func:`run_dag`.

    Returns:
        The parsed steps, in the order given.

    Raises:
        DagError: If a step is missing a required key. Raised as a clean,
            user-facing error rather than letting a ``KeyError`` escape.
    """
    parsed = []
    for index, raw in enumerate(steps):
        missing = {"id", "service", "operation"} - raw.keys()
        if missing:
            raise DagError(
                f"step {index} is missing required key(s): {sorted(missing)}. "
                "Every step needs 'id', 'service', and 'operation'."
            )
        parsed.append(
            Step(
                id=raw["id"],
                service=raw["service"],
                operation=raw["operation"],
                params=raw.get("params", {}),
                extract=raw.get("extract"),
                depends_on=tuple(raw.get("depends_on", ())),
            )
        )
    return parsed


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
    max_accounts: int | None = None,
    max_items: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run a small declarative multi-step query DAG against every resolved account.

    Each entry in ``steps`` is
    ``{"id": ..., "service": ..., "operation": ..., "params": {...},
    "extract": "...", "depends_on": [...]}``.

    A later step's ``params`` may reference an earlier step's extracted output
    anywhere a literal would go, via ``{"$ref": "<step_id>"}``. There is no
    expression language and nothing is ever evaluated as code.

    Every resolved account runs its own independent copy of the whole DAG, with
    one credential mint per account rather than one per step. As with
    ``aws_query``, AWS IAM is the sole authorization boundary for every step,
    and ``max_items`` raises the per-account pagination ceiling for every step.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        parsed_steps = _parse_steps(steps)

        def _call_dag(account: AccountContext) -> dict[str, Any]:
            """Run the whole DAG for one account, recording each step for the audit log."""

            def _call_step(step: Step, resolved_params: dict[str, Any]) -> dict[str, Any]:
                try:
                    response = execute_call(
                        session=account.session,
                        region=account.region,
                        service=step.service,
                        operation=step.operation,
                        params=resolved_params,
                        max_items=account.max_items,
                    )
                except McpError as exc:
                    account.record(step.service, step.operation, ok=False, error=str(exc))
                    raise
                account.record(step.service, step.operation, ok=True)
                return response

            try:
                return execute_dag(parsed_steps, call=_call_step)
            except DagError as exc:
                # A malformed DAG is the caller's mistake, not this account's
                # failure, so report it in-band rather than as an AWS error.
                return {"dag_error": str(exc)}

        logger.info("run_dag steps=%d region=%s", len(parsed_steps), region)
        return run_fanout(
            state,
            selector=_selector(
                org=org,
                orgs=orgs,
                account_name=account_name,
                account_names=account_names,
                account_id=account_id,
                account_ids=account_ids,
                name_contains=name_contains,
                environment=environment,
                exclude_account_ids=exclude_account_ids,
                exclude_account_names=exclude_account_names,
            ),
            region=region,
            per_account=_call_dag,
            audit_label=("(dag)", f"{len(parsed_steps)} steps"),
            verb="ran DAG on",
            ctx=ctx,
            max_accounts=max_accounts,
            max_items=max_items,
        )

    return _safe(_run, ctx)


# ----------------------------------------------------------- diagnostics tools


@app.tool()
def config_show() -> dict[str, Any]:
    """Show the resolved, loaded configuration (it never contains secrets)."""

    def _run() -> dict[str, Any]:
        cfg = _get_state().config
        perf = cfg.performance
        return {
            "source_path": str(cfg.source_path),
            "default_org": cfg.default_org,
            "cache_ttl_hours": cfg.cache_ttl_hours,
            "log_level": cfg.log_level,
            "performance": {
                "credential_workers": perf.credential_workers,
                "service_workers": perf.service_workers,
                "per_target_timeout_seconds": perf.per_target_timeout_seconds,
                "max_items_per_account": perf.max_items_per_account,
                "max_accounts": perf.max_accounts,
            },
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
    """Re-load and validate the config file, reporting errors without applying them."""

    def _run() -> dict[str, Any]:
        cfg = load_config()
        return {"valid": True, "source_path": str(cfg.source_path), "orgs": sorted(cfg.portals)}

    return _safe(_run)


@app.tool()
def perf_stats() -> dict[str, Any]:
    """Report cache hit/miss counts and executor sizing for the running server.

    Answers "why was that fan-out slow?" without turning on debug logging. A
    high credential-broker miss rate points to short-lived sessions or role
    churn; a high role-cache miss rate suggests the directory TTL is too short.
    """

    def _run() -> dict[str, Any]:
        state = _get_state()
        return {
            "executor": {
                "credential_workers": state.executor.credential_workers,
                "service_workers": state.executor.service_workers,
            },
            "broker": state.broker.stats(),
            "accounts_cache": state.cache.stats(),
        }

    return _safe(_run)


def _parse_args(argv: Sequence[str] | None) -> None:
    """Handle ``--version`` / ``--help``; the server itself takes no arguments.

    Exists so the installed command can be smoke-tested (``aws-sso-mcp
    --version``) without starting the stdio transport, which would block
    waiting for an MCP client.
    """
    parser = argparse.ArgumentParser(
        prog="aws-sso-mcp",
        description="MCP server for AWS IAM Identity Center (SSO). Speaks MCP over stdio.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Console-script entry point, registered as ``aws-sso-mcp``."""
    _parse_args(argv)
    # Configure logging before anything else so startup messages from the MCP
    # SDK and boto3 are captured too. The configured level is re-applied from
    # the user's config on the first tool call.
    configure_logging("INFO")
    logger.info("aws-sso-mcp starting MCP stdio transport")
    app.run()


if __name__ == "__main__":
    main()
