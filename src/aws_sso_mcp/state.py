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
from .engine.executor import QueryExecutor
from .errors import McpError, OrgNotFoundError
from .sso.broker import CredentialBroker


@dataclass
class AppState:
    """All process-wide, non-secret runtime state for one server instance."""

    config: Config
    cache: AccountCache
    broker: CredentialBroker
    executor: QueryExecutor
    state_dir: Path
    _active_org_lock: threading.Lock
    _active_org: str | None = None

    @classmethod
    def build(cls, config: Config) -> AppState:
        state_dir = default_state_dir()
        cache_db = state_dir / "cache.db"
        cache = AccountCache(cache_db, ttl_hours=config.cache_ttl_hours)
        perf = config.performance
        return cls(
            config=config,
            cache=cache,
            broker=CredentialBroker(),
            executor=QueryExecutor(
                credential_workers=perf.credential_workers,
                service_workers=perf.service_workers,
                per_target_timeout_seconds=perf.per_target_timeout_seconds,
                progress_min_interval_seconds=perf.progress_min_interval_seconds,
            ),
            state_dir=state_dir,
            _active_org_lock=threading.Lock(),
            _active_org=config.default_org,
        )

    @property
    def audit_log_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

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
        """Resolve an explicit ``org=`` override, falling back to the active org.

        Args:
            org: An explicit org alias, or ``None`` to use the active one.

        Returns:
            The matching portal configuration.

        Raises:
            OrgNotFoundError: If ``org`` names an org that is not configured.
            McpError: If ``org`` is ``None`` and no active org is set.
        """
        if org is not None:
            return self.require_portal(org)
        active = self.get_active_org()
        if active is None:
            raise McpError(
                "No active org and none specified. Call use_org(org) first, or pass org= "
                f"explicitly. Configured orgs: {sorted(self.config.portals)}"
            )
        return self.require_portal(active)
