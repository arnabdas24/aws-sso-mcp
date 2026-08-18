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


@pytest.fixture()
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


def test_explicit_role_wins_when_granted(portal: PortalConfig) -> None:
    result = resolve_role(
        portal=portal,
        account_id="111111111111",
        account_name="billing-dev",
        granted_roles=["DevRole", "ReadOnly", "AdminRole"],
        explicit_role="AdminRole",
    )
    assert result.role == "AdminRole"
    assert result.source == "explicit"


def test_explicit_role_rejected_if_not_granted(portal: PortalConfig) -> None:
    with pytest.raises(RoleResolutionError) as exc_info:
        resolve_role(
            portal=portal,
            account_id="111111111111",
            account_name="billing-dev",
            granted_roles=["DevRole", "ReadOnly"],
            explicit_role="AdminRole",
        )
    assert "AdminRole" in str(exc_info.value)
    assert exc_info.value.available == ["DevRole", "ReadOnly"]


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


def test_sole_grant_used_when_no_environment_matches(portal: PortalConfig) -> None:
    result = resolve_role(
        portal=portal,
        account_id="333333333333",
        account_name="billing-sandbox",
        granted_roles=["OnlyRole"],
    )
    assert result.role == "OnlyRole"
    assert result.source == "sole-grant"


def test_ambiguous_without_mapping_raises(portal: PortalConfig) -> None:
    with pytest.raises(RoleResolutionError) as exc_info:
        resolve_role(
            portal=portal,
            account_id="333333333333",
            account_name="billing-sandbox",
            granted_roles=["RoleA", "RoleB"],
        )
    assert exc_info.value.available == ["RoleA", "RoleB"]


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
