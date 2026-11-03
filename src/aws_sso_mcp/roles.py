"""Deterministic role resolution.

The model must never choose which IAM role to assume - this module is the
single place that decision is made, and it is pure/synchronous (no network
calls, no LLM involvement).

The TOML config is the only source of truth: the account name is classified
into a ``[portals.<org>.environments.<env>]`` by ``patterns``, then the first
pattern in ``roles`` (or ``fallback``) that matches a granted role wins. There
is deliberately no caller-supplied override and no "only one role granted"
default, so neither the model nor a prompt injection can pick or escalate a
role. If the rules select nothing, resolution fails with the granted roles
listed so the user can fix the config.
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
    source: str  # always "environment": the TOML rules are the only selector
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
) -> RoleResolution:
    """Resolve exactly one role to assume for this account.

    Raises:
        RoleResolutionError: If no role can be determined deterministically.
    """
    env = classify_environment(portal, account_name)
    if env is not None:
        match = _first_role_match(granted_roles, env.roles, env.match)
        if match is None:
            match = _first_role_match(granted_roles, env.fallback, env.match)
        if match is not None:
            return RoleResolution(role=match, source="environment", environment=env.name)

    if not granted_roles:
        reason = "no roles are granted to you on this account"
    elif env is None:
        reason = "no [environments] rule in the config matches this account name"
    else:
        reason = (
            f"account classified as environment {env.name!r} but no configured role or "
            "fallback pattern matched a granted role"
        )
    raise RoleResolutionError(account_id, account_name, reason, granted_roles)
