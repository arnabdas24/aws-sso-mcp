"""Per-process session state: active org, wired-up config/cache/broker.

A stdio MCP server is one process per client session, so a module-level
singleton is a natural, correctly-scoped place for "which org is active
right now" - it resets automatically when the session/process ends.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .cache.accounts import AccountCache
from .config import Config, PortalConfig, default_state_dir
from .errors import OrgNotFoundError
from .sso.broker import CredentialBroker


@dataclass
class AppState:
    """All process-wide, non-secret runtime state for one server instance."""

    config: Config
    cache: AccountCache
    broker: CredentialBroker
    state_dir: Path
    _active_org_lock: threading.Lock
    _active_org: str | None = None

    @classmethod
    def build(cls, config: Config) -> AppState:
        state_dir = default_state_dir()
        cache_db = state_dir / "cache.db"
        cache = AccountCache(cache_db, ttl_hours=config.cache_ttl_hours)
        return cls(
            config=config,
            cache=cache,
            broker=CredentialBroker(),
            state_dir=state_dir,
            _active_org_lock=threading.Lock(),
            _active_org=config.default_org,
        )

    def get_active_org(self) -> str | None:
        with self._active_org_lock:
            return self._active_org

    def set_active_org(self, org: str) -> PortalConfig:
        portal = self.require_portal(org)
        with self._active_org_lock:
            self._active_org = org
        return portal

    def require_portal(self, org: str) -> PortalConfig:
        portal = self.config.portals.get(org)
        if portal is None:
            raise OrgNotFoundError(org, list(self.config.portals))
        return portal

    def resolve_org(self, org: str | None) -> PortalConfig:
        """Resolve an explicit ``org=`` override, else fall back to the active org."""
        if org is not None:
            return self.require_portal(org)
        active = self.get_active_org()
        if active is None:
            raise _no_active_org_error(self.config)
        return self.require_portal(active)


def _no_active_org_error(config: Config) -> Exception:
    from .errors import McpError

    return McpError(
        "No active org and none specified. Call use_org(org) first, or pass org= explicitly. "
        f"Configured orgs: {sorted(config.portals)}"
    )
