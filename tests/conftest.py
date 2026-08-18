"""Shared pytest fixtures: isolated $HOME/config/state, never touching real files."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate $HOME, AWS_SSO_MCP_CONFIG, and AWS_SSO_MCP_STATE_DIR to a tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AWS_SSO_MCP_CONFIG", raising=False)
    monkeypatch.setenv("AWS_SSO_MCP_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture()
def sample_config_path(tmp_path: Path) -> Path:
    """Write a minimal valid two-org config file and return its path."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
default_org = "org1"
cache_ttl_hours = 12
allow_secret_values = false

[portals.org1]
start_url = "https://example.awsapps.com/start"
region = "us-east-1"

[portals.org1.environments.dev]
match = "endswith"
patterns = ["dev"]
roles = ["DevRole"]
fallback = ["ReadOnly"]

[portals.org2]
start_url = "https://example2.awsapps.com/start"
region = "eu-west-1"
""",
        encoding="utf-8",
    )
    return config_path
