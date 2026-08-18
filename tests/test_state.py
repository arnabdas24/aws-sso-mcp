"""Tests for aws_sso_mcp.state - active-org session state and resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from aws_sso_mcp.config import load_config
from aws_sso_mcp.errors import McpError, OrgNotFoundError
from aws_sso_mcp.state import AppState


def test_default_org_is_active_on_build(sample_config_path: Path, isolated_env: Path) -> None:
    state = AppState.build(load_config(sample_config_path))
    assert state.get_active_org() == "org1"


def test_use_org_switches_active(sample_config_path: Path, isolated_env: Path) -> None:
    state = AppState.build(load_config(sample_config_path))
    state.set_active_org("org2")
    assert state.get_active_org() == "org2"


def test_use_org_unknown_raises(sample_config_path: Path, isolated_env: Path) -> None:
    state = AppState.build(load_config(sample_config_path))
    with pytest.raises(OrgNotFoundError):
        state.set_active_org("does-not-exist")


def test_resolve_org_explicit_overrides_active(
    sample_config_path: Path, isolated_env: Path
) -> None:
    state = AppState.build(load_config(sample_config_path))
    assert state.get_active_org() == "org1"
    portal = state.resolve_org("org2")
    assert portal.name == "org2"
    assert state.get_active_org() == "org1"  # unaffected by one-off override


def test_resolve_org_no_active_no_explicit_raises(tmp_path: Path, isolated_env: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[portals.org1]
start_url = "https://example.awsapps.com/start"
region = "us-east-1"
""",
        encoding="utf-8",
    )
    state = AppState.build(load_config(config_path))
    assert state.get_active_org() is None
    with pytest.raises(McpError, match="No active org"):
        state.resolve_org(None)
