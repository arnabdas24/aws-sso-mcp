"""aws-sso-mcp entry point: MCP server exposing SSO org/account tools.

Standalone - no dependency on aws-sso-orchestrator. Phase 1 surface: org
switching, login/token status, account search, and cache management. Later
phases add role resolution, query execution, and the generic AWS engine on
top of this same session state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import PortalConfig, load_config
from .errors import McpError
from .redact import redact
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


def main() -> None:
    """Console-script entry point (registered as ``aws-sso-mcp``)."""
    app.run()


if __name__ == "__main__":
    main()
