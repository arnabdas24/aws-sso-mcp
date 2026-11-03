"""Tests for deterministic role resolution (roles.py)."""

from __future__ import annotations

import pytest

from aws_sso_mcp.config import EnvironmentRule, PortalConfig
from aws_sso_mcp.errors import RoleResolutionError
from aws_sso_mcp.roles import _matches, classify_environment, resolve_role


@pytest.mark.parametrize(
    ("value", "pattern", "mode", "expected"),
    [
        ("prod-account", "prod-account", "exact", True),
        ("prod-account", "other", "exact", False),
        ("prod-account", "prod", "startswith", True),
        ("prod-account", "account", "endswith", True),
        ("prod-account", "d-acc", "contains", True),
        ("prod-account", "^prod-.*$", "regex", True),
        ("dev-account", "^prod-.*$", "regex", False),
    ],
)
def test_matches_all_modes(value: str, pattern: str, mode: str, expected: bool) -> None:
    assert _matches(value, pattern, mode) is expected


def test_matches_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown match mode"):
        _matches("x", "y", "bogus")


@pytest.fixture
def portal() -> PortalConfig:
    return PortalConfig(
        name="org1",
        start_url="https://example.awsapps.com/start",
        sso_region="us-east-1",
        environments={
            "dev": EnvironmentRule(
                name="dev",
                match="endswith",
                patterns=("-dev",),
                roles=("DevRole",),
                fallback=("ReadOnly",),
            ),
            "prd": EnvironmentRule(
                name="prd",
                match="endswith",
                patterns=("-prd",),
                roles=("ProdDeployer", "ProdRole"),
                fallback=(),
            ),
        },
    )


def test_classify_environment_matches_first_pattern(portal: PortalConfig) -> None:
    env = classify_environment(portal, "billing-dev")
    assert env is not None
    assert env.name == "dev"


def test_classify_environment_no_match(portal: PortalConfig) -> None:
    assert classify_environment(portal, "billing-sandbox") is None


def test_resolve_role_accepts_no_override(portal: PortalConfig) -> None:
    """The TOML rules are the only selector: there must be no way to name a role."""
    with pytest.raises(TypeError):
        resolve_role(  # type: ignore[call-arg]
            portal=portal,
            account_id="111111111111",
            account_name="billing-dev",
            granted_roles=["DevRole", "AdminRole"],
            explicit_role="AdminRole",
        )


def test_granted_admin_role_is_not_used_when_rules_pick_another(portal: PortalConfig) -> None:
    result = resolve_role(
        portal=portal,
        account_id="111111111111",
        account_name="billing-dev",
        granted_roles=["AdminRole", "DevRole"],
    )
    assert result.role == "DevRole"


def test_environment_mapping_picks_primary_role(portal: PortalConfig) -> None:
    result = resolve_role(
        portal=portal,
        account_id="111111111111",
        account_name="billing-dev",
        granted_roles=["ReadOnly", "DevRole"],
    )
    assert result.role == "DevRole"
    assert result.source == "environment"
    assert result.environment == "dev"


def test_environment_mapping_falls_back(portal: PortalConfig) -> None:
    result = resolve_role(
        portal=portal,
        account_id="111111111111",
        account_name="billing-dev",
        granted_roles=["ReadOnly", "OtherRole"],
    )
    assert result.role == "ReadOnly"
    assert result.source == "environment"


def test_environment_first_role_pattern_priority(portal: PortalConfig) -> None:
    result = resolve_role(
        portal=portal,
        account_id="222222222222",
        account_name="billing-prd",
        granted_roles=["ProdRole", "ProdDeployer"],
    )
    assert result.role == "ProdDeployer"


@pytest.mark.parametrize("granted", [["OnlyRole"], ["RoleA", "RoleB"]])
def test_no_matching_rule_raises_even_with_granted_roles(
    portal: PortalConfig, granted: list[str]
) -> None:
    """No default role: a sole grant is not used when no rule matches the account."""
    with pytest.raises(RoleResolutionError) as exc_info:
        resolve_role(
            portal=portal,
            account_id="333333333333",
            account_name="billing-sandbox",
            granted_roles=granted,
        )
    assert exc_info.value.available == granted
    assert "no [environments] rule" in str(exc_info.value)


def test_environment_matched_but_no_role_or_fallback_matches(portal: PortalConfig) -> None:
    with pytest.raises(RoleResolutionError) as exc_info:
        resolve_role(
            portal=portal,
            account_id="111111111111",
            account_name="billing-dev",
            granted_roles=["RoleA", "RoleB"],
        )
    assert "billing-dev" in str(exc_info.value)


def test_no_roles_granted_at_all(portal: PortalConfig) -> None:
    with pytest.raises(RoleResolutionError):
        resolve_role(
            portal=portal,
            account_id="333333333333",
            account_name="billing-sandbox",
            granted_roles=[],
        )


def test_no_granted_roles_says_so_plainly() -> None:
    """Reporting "multiple roles are granted" when zero are is actively misleading.

    It sends the user looking for an ambiguity to disambiguate, when the real
    answer is that they have no access to the account at all.
    """
    portal = PortalConfig(
        name="org1",
        start_url="https://example.awsapps.com/start",
        sso_region="us-east-1",
        environments={},
    )
    with pytest.raises(RoleResolutionError) as excinfo:
        resolve_role(
            portal=portal,
            account_id="111111111111",
            account_name="acct-a",
            granted_roles=[],
        )

    message = str(excinfo.value)
    assert "no roles are granted" in message
    assert "multiple roles" not in message
