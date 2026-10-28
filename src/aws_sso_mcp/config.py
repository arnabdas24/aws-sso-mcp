"""Single-TOML configuration for aws-sso-mcp.

Standalone from ``aws-sso-orchestrator`` - this module has no import
dependency on that package. The shape deliberately mirrors the orchestrator's
``[portals.<name>]`` / ``[portals.<name>.environments.<env>]`` structure so
existing users recognize it immediately, but it is parsed by wholly
independent code and stored under its own config/cache/token paths.

Resolution order for the config file path:

1. An explicit ``path`` argument (programmatic override, mainly for tests).
2. The ``AWS_SSO_MCP_CONFIG`` environment variable.
3. ``~/.config/aws-sso-mcp/config.toml``.

Nothing in this module performs any network I/O. Only local filesystem
reads happen here.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

# Single source of truth for the per-account pagination ceiling: the default
# for ``PerformanceConfig.max_items_per_account`` below, and the fallback used
# by ``engine.catalog.execute_call`` when no explicit ceiling is threaded
# through. Defined here rather than in the engine so that configuration
# defaults live with the configuration.
DEFAULT_MAX_ITEMS = 1000

_ENV_CONFIG_PATH = "AWS_SSO_MCP_CONFIG"

# Key name substrings that must never appear in the config file - this file
# holds references/settings only, never secrets of any kind.
_FORBIDDEN_KEY_SUBSTRINGS = (
    "secret",
    "password",
    "passwd",
    "access_key",
    "session_token",
    "private_key",
    "api_key",
    "token",
)

_VALID_MATCH_MODES = frozenset({"exact", "startswith", "endswith", "contains", "regex"})


def default_config_path() -> Path:
    """Return the default config file path, resolving ``Path.home()`` lazily."""
    return Path.home() / ".config" / "aws-sso-mcp" / "config.toml"


def default_state_dir() -> Path:
    """Return the default state directory (tokens, cache) - resolved lazily."""
    override = os.environ.get("AWS_SSO_MCP_STATE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".aws_sso" / "mcp"


def resolve_config_path(path: Path | str | None = None) -> Path:
    """Resolve the config file path: explicit arg > env var > default."""
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get(_ENV_CONFIG_PATH)
    if override:
        return Path(override).expanduser()
    return default_config_path()


@dataclass(frozen=True, slots=True)
class EnvironmentRule:
    """One ``[portals.<org>.environments.<env>]`` table.

    Attributes:
        name: Environment label (e.g. "dev", "test", "prd") - entirely
            user-defined, no fixed set.
        match: Matching mode for both account-name patterns and role
            patterns - one of exact/startswith/endswith/contains/regex.
        patterns: Ordered account-name patterns; first match assigns this
            environment.
        roles: Ordered role-name patterns tried first for accounts
            classified into this environment.
        fallback: Ordered role-name patterns tried when nothing in
            ``roles`` matches a role actually granted on the account.
    """

    name: str
    match: str = "endswith"
    patterns: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    fallback: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PortalConfig:
    """One ``[portals.<name>]`` table - a single SSO org/portal.

    Attributes:
        name: Local alias for this org, e.g. "org1" - used everywhere as
            ``org=`` in tool calls, and to namespace this org's token cache
            file and cache-db rows.
        start_url: The IAM Identity Center (SSO) start URL for this org.
        sso_region: AWS region hosting this org's SSO/SSO-OIDC endpoints
            (NOT a default query region - query region is always supplied
            by the caller, never defaulted from config).
        environments: Environment name -> :class:`EnvironmentRule`, in TOML
            declaration order (order matters: first pattern match wins).
    """

    name: str
    start_url: str
    sso_region: str
    environments: dict[str, EnvironmentRule] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PerformanceConfig:
    """Tunable knobs for the multi-account fan-out executor.

    Every field has a safe default; an operator only needs to override values
    that do not fit their scale - very large orgs, tight SSO throttles, and so
    on.

    Attributes:
        credential_workers: Size of the SSO/credential thread lane. Kept small
            because SSO endpoints throttle aggressively.
        service_workers: Size of the service-call thread lane. The main
            throughput knob for a large fan-out.
        per_target_timeout_seconds: How long one account's work may take,
            excluding time spent queued behind other accounts. ``None``
            disables the timeout.
        progress_min_interval_seconds: Minimum gap between progress updates
            sent to the MCP client.
        max_items_per_account: Ceiling on items collected from a paginated
            operation, per account. Prevents a single account from exhausting
            the client's context window.
        max_accounts: Ceiling on how many accounts one call may fan out to.
            Guards against a mistyped selector silently touching every account
            in the org.
    """

    credential_workers: int = 10
    service_workers: int = 50
    per_target_timeout_seconds: float | None = 60.0
    progress_min_interval_seconds: float = 0.25
    max_items_per_account: int = DEFAULT_MAX_ITEMS
    max_accounts: int = 200


@dataclass(frozen=True, slots=True)
class Config:
    """Fully resolved aws-sso-mcp configuration.

    Attributes:
        portals: Every configured org, keyed by its local alias.
        default_org: The org made active at session start, if configured.
        cache_ttl_hours: Threshold before the account/role directory for an
            org is considered stale and eligible for a lazy refresh.
        log_level: Logging level name.
        performance: Executor concurrency and per-target timeout knobs.
        source_path: The config file that was loaded.
    """

    portals: dict[str, PortalConfig]
    default_org: str | None
    cache_ttl_hours: float
    log_level: str
    source_path: Path
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)


def _check_no_secrets(data: dict[str, Any], ctx: str) -> None:
    """Recursively reject any key whose name looks like it might hold a secret."""
    for key, value in data.items():
        lowered = str(key).lower()
        for bad in _FORBIDDEN_KEY_SUBSTRINGS:
            if bad in lowered:
                raise ConfigError(
                    f"{ctx}: key {key!r} looks like it may hold a secret - config files must "
                    "never contain tokens, keys, or passwords."
                )
        if isinstance(value, dict):
            _check_no_secrets(value, f"{ctx}.{key}")


def _parse_environment_rule(org: str, env_name: str, data: dict[str, Any]) -> EnvironmentRule:
    if not isinstance(data, dict):
        raise ConfigError(f"[portals.{org}.environments.{env_name}] must be a table")

    match = str(data.get("match", "endswith"))
    if match not in _VALID_MATCH_MODES:
        raise ConfigError(
            f"[portals.{org}.environments.{env_name}].match={match!r} is invalid; "
            f"must be one of {sorted(_VALID_MATCH_MODES)}"
        )

    patterns = _string_list(
        data.get("patterns"), f"[portals.{org}.environments.{env_name}].patterns"
    )
    roles = _string_list(data.get("roles"), f"[portals.{org}.environments.{env_name}].roles")
    fallback_raw = data.get("fallback", [])
    fallback = (
        _string_list(fallback_raw, f"[portals.{org}.environments.{env_name}].fallback")
        if fallback_raw
        else ()
    )

    if match == "regex":
        import re

        for pattern in (*patterns, *roles, *fallback):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"[portals.{org}.environments.{env_name}]: invalid regex {pattern!r}: {exc}"
                ) from exc

    return EnvironmentRule(
        name=env_name, match=match, patterns=patterns, roles=roles, fallback=fallback
    )


def _string_list(raw: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) and s for s in raw):
        raise ConfigError(f"{field_name} must be a non-empty list of non-empty strings")
    return tuple(raw)


def _parse_portal(name: str, data: dict[str, Any]) -> PortalConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"[portals.{name}] must be a table")

    start_url = data.get("start_url")
    sso_region = data.get("region")
    if not start_url or not isinstance(start_url, str):
        raise ConfigError(f"[portals.{name}] is missing a required 'start_url' string")
    if not sso_region or not isinstance(sso_region, str):
        raise ConfigError(f"[portals.{name}] is missing a required 'region' string")

    environments_raw = data.get("environments", {})
    environments: dict[str, EnvironmentRule] = {}
    if isinstance(environments_raw, dict):
        for env_name, env_data in environments_raw.items():
            environments[env_name] = _parse_environment_rule(name, env_name, env_data)

    return PortalConfig(
        name=name, start_url=start_url, sso_region=sso_region, environments=environments
    )


def _positive_int(raw: dict[str, Any], key: str, default: int, source: Path) -> int:
    """Read a ``[performance]`` integer that must be at least 1.

    Args:
        raw: The ``[performance]`` table.
        key: Key to read.
        default: Value to use when the key is absent.
        source: Config path, for error messages.

    Returns:
        The validated integer.

    Raises:
        ConfigError: If the value is not an integer, or is less than 1. Raised
            instead of letting ``int()`` surface a bare ``ValueError``, so a
            typo in the config produces an actionable message rather than a
            traceback.
    """
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{source}: [performance].{key} must be an integer >= 1")
    return value


def _parse_performance(raw: dict[str, Any], source: Path) -> PerformanceConfig:
    """Parse and validate the optional ``[performance]`` table.

    Args:
        raw: The ``[performance]`` table, possibly empty.
        source: Config path, for error messages.

    Returns:
        A fully validated :class:`PerformanceConfig`.

    Raises:
        ConfigError: If any value is of the wrong type or out of range.
    """
    timeout_raw = raw.get("per_target_timeout_seconds", 60.0)
    # An explicit 0 (or null) is the documented way to disable the timeout.
    if timeout_raw is None or timeout_raw == 0:
        timeout: float | None = None
    elif isinstance(timeout_raw, int | float) and not isinstance(timeout_raw, bool):
        timeout = float(timeout_raw)
    else:
        raise ConfigError(
            f"{source}: [performance].per_target_timeout_seconds must be a number, "
            "or 0 to disable the timeout"
        )

    interval_raw = raw.get("progress_min_interval_seconds", 0.25)
    if isinstance(interval_raw, bool) or not isinstance(interval_raw, int | float):
        raise ConfigError(f"{source}: [performance].progress_min_interval_seconds must be a number")

    return PerformanceConfig(
        credential_workers=_positive_int(raw, "credential_workers", 10, source),
        service_workers=_positive_int(raw, "service_workers", 50, source),
        per_target_timeout_seconds=timeout,
        progress_min_interval_seconds=float(interval_raw),
        max_items_per_account=_positive_int(
            raw, "max_items_per_account", DEFAULT_MAX_ITEMS, source
        ),
        max_accounts=_positive_int(raw, "max_accounts", 200, source),
    )


def load_config(path: Path | str | None = None) -> Config:
    """Load and validate the aws-sso-mcp TOML configuration.

    Args:
        path: Optional explicit path, overriding ``AWS_SSO_MCP_CONFIG`` and
            the default location.

    Returns:
        A fully validated, immutable :class:`Config`.

    Raises:
        ConfigError: If the file is missing, not valid TOML, fails
            structural validation, or contains a secret-like key.
    """
    resolved = resolve_config_path(path)
    if not resolved.is_file():
        raise ConfigError(
            f"Config file not found at {resolved}. Set AWS_SSO_MCP_CONFIG or create it "
            "(see the example config in the project README)."
        )

    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file {resolved} is not valid TOML: {exc}") from exc

    _check_no_secrets(raw, str(resolved))

    portals_raw = raw.get("portals", {})
    if not isinstance(portals_raw, dict) or not portals_raw:
        raise ConfigError(f"{resolved}: at least one [portals.<name>] table is required")

    portals = {name: _parse_portal(name, data) for name, data in portals_raw.items()}

    default_org = raw.get("default_org")
    if default_org is not None and default_org not in portals:
        raise ConfigError(
            f"{resolved}: default_org={default_org!r} does not match any configured "
            f"[portals.*] name. Configured: {sorted(portals)}"
        )

    cache_ttl_hours = raw.get("cache_ttl_hours", 12)
    if not isinstance(cache_ttl_hours, int | float) or cache_ttl_hours < 0:
        raise ConfigError(f"{resolved}: cache_ttl_hours must be a non-negative number")

    log_level = str(raw.get("log_level", "INFO")).upper()

    perf_raw = raw.get("performance", {})
    if not isinstance(perf_raw, dict):
        raise ConfigError(f"{resolved}: [performance] must be a table")
    performance = _parse_performance(perf_raw, resolved)

    return Config(
        portals=portals,
        default_org=default_org,
        cache_ttl_hours=float(cache_ttl_hours),
        log_level=log_level,
        source_path=resolved,
        performance=performance,
    )
