"""Deterministic role resolution.

The model must never choose which IAM role to assume - this module is the
single place that decision is made, and it is pure/synchronous (no network
calls, no LLM involvement). Resolution order, in priority:

1. An explicit ``role=`` supplied by the caller/user - used as-is if it is
   actually granted on the account, otherwise a clean error naming what
   *is* granted.
2. The TOML ``[portals.<org>.environments.<env>]`` mapping: the account
   name is classified into an environment by ``patterns``, then the first
   pattern in ``roles`` (or ``fallback``) that matches a granted role wins.
3. If exactly one role is granted on the account, use it.
4. Otherwise, resolution fails with the full list of granted roles so the
   caller can retry with an explicit ``role=``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import EnvironmentRule, PortalConfig
from .errors import RoleResolutionError


@dataclass(frozen=True, slots=True)
class RoleResolution:
    """The outcome of resolving a role for one account."""

    role: str
    source: str  # "explicit" | "environment" | "sole-grant"
    environment: str | None = None


def _matches(value: str, pattern: str, mode: str) -> bool:
    if mode == "exact":
        return value == pattern
    if mode == "startswith":
        return value.startswith(pattern)
    if mode == "endswith":
        return value.endswith(pattern)
    if mode == "contains":
        return pattern in value
    if mode == "regex":
        return re.search(pattern, value) is not None
    raise ValueError(f"unknown match mode: {mode!r}")  # pragma: no cover - config validates this


def classify_environment(portal: PortalConfig, account_name: str) -> EnvironmentRule | None:
    """Return the first configured environment whose patterns match this account name."""
    for env in portal.environments.values():
        if any(_matches(account_name, pattern, env.match) for pattern in env.patterns):
            return env
    return None


def _first_role_match(granted: list[str], patterns: tuple[str, ...], mode: str) -> str | None:
    for pattern in patterns:
        for role in granted:
            if _matches(role, pattern, mode):
                return role
    return None


def resolve_role(
    *,
    portal: PortalConfig,
    account_id: str,
    account_name: str,
    granted_roles: list[str],
    explicit_role: str | None = None,
) -> RoleResolution:
    """Resolve exactly one role to assume for this account.

    Raises:
        RoleResolutionError: If no role can be determined deterministically.
    """
    if explicit_role is not None:
        if explicit_role in granted_roles:
            return RoleResolution(role=explicit_role, source="explicit")
        raise RoleResolutionError(
            account_id,
            account_name,
            f"requested role {explicit_role!r} is not granted on this account",
            granted_roles,
        )

    env = classify_environment(portal, account_name)
    if env is not None:
        match = _first_role_match(granted_roles, env.roles, env.match)
        if match is None:
            match = _first_role_match(granted_roles, env.fallback, env.match)
        if match is not None:
            return RoleResolution(role=match, source="environment", environment=env.name)

    if len(granted_roles) == 1:
        return RoleResolution(role=granted_roles[0], source="sole-grant")

    reason = (
        "no environment mapping matched and multiple roles are granted"
        if env is None
        else f"account classified as environment {env.name!r} but no configured role/fallback "
        "pattern matched a granted role"
        if len(granted_roles) != 1
        else "no role could be determined"
    )
    raise RoleResolutionError(account_id, account_name, reason, granted_roles)
