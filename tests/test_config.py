"""Tests for aws_sso_mcp.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from aws_sso_mcp.config import load_config
from aws_sso_mcp.errors import ConfigError


def test_load_valid_config(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    assert cfg.default_org == "org1"
    assert set(cfg.portals) == {"org1", "org2"}
    assert cfg.portals["org1"].start_url == "https://example.awsapps.com/start"
    assert cfg.portals["org1"].sso_region == "us-east-1"
    assert cfg.cache_ttl_hours == 12
    assert cfg.allow_secret_values is False
    assert "dev" in cfg.portals["org1"].environments
    env = cfg.portals["org1"].environments["dev"]
    assert env.roles == ("DevRole",)
    assert env.fallback == ("ReadOnly",)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("this is not [ valid toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


def test_secret_like_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[portals.org1]
start_url = "https://example.awsapps.com/start"
region = "us-east-1"
access_key = "AKIAFAKE"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="secret"):
        load_config(path)


def test_no_portals_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('default_org = "org1"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="portals"):
        load_config(path)


def test_missing_start_url_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[portals.org1]
region = "us-east-1"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="start_url"):
        load_config(path)


def test_unknown_default_org_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
default_org = "nope"

[portals.org1]
start_url = "https://example.awsapps.com/start"
region = "us-east-1"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="default_org"):
        load_config(path)


def test_invalid_match_mode_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[portals.org1]
start_url = "https://example.awsapps.com/start"
region = "us-east-1"

[portals.org1.environments.dev]
match = "fuzzy"
patterns = ["dev"]
roles = ["DevRole"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="match"):
        load_config(path)


def test_invalid_regex_pattern_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[portals.org1]
start_url = "https://example.awsapps.com/start"
region = "us-east-1"

[portals.org1.environments.dev]
match = "regex"
patterns = ["["]
roles = ["DevRole"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="regex"):
        load_config(path)
