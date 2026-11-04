"""Shared pytest fixtures: isolated $HOME/config/state, never touching real files."""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest


class FakeCtx:
    """Stand-in for the MCP Context that records confirmation prompts and answers them."""

    def __init__(
        self,
        *,
        action: str = "accept",
        confirm: bool = True,
        can_elicit: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.messages: list[str] = []
        self.client_capabilities = SimpleNamespace(elicitation=object() if can_elicit else None)
        self._action = action
        self._confirm = confirm
        self._error = error

    async def elicit(self, message: str, schema: Any) -> SimpleNamespace:
        self.messages.append(message)
        if self._error is not None:
            raise self._error
        data = schema(confirm=self._confirm) if self._action == "accept" else None
        return SimpleNamespace(action=self._action, data=data)

    async def report_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def debug(self, *args: Any, **kwargs: Any) -> None:
        return None

    info = warning = error = debug


@pytest.fixture
def make_ctx() -> type[FakeCtx]:
    return FakeCtx


@pytest.fixture
def in_worker_thread() -> Callable[..., Any]:
    """Run a sync callable the way MCP runs a tool: on an anyio worker thread."""

    def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        async def _main() -> Any:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

        return anyio.run(_main)

    return _run


@pytest.fixture(autouse=True)
def _reset_module_caches() -> None:
    """Clear process-wide caches (SSO tokens, boto3 clients) between tests.

    These caches are correct in production but leak state across tests that
    share the same "org1" alias with different tmp dirs. Reset them
    proactively so no test can see another's residue.
    """
    from aws_sso_mcp import awscfg
    from aws_sso_mcp.sso import device_auth

    with device_auth._MEM_LOCK:
        device_auth._MEM_CACHE.clear()
    awscfg.reset_client_cache()


@pytest.fixture
def flush_audit() -> Callable[[Path], None]:
    """Return a helper that blocks until the audit writer has drained to disk.

    The audit log is written by a background daemon thread so that a fan-out
    never waits on file I/O. Tests that assert on the file's contents need to
    wait for that thread, which this does via the same ``flush_all`` drain that
    the production ``atexit`` hook uses - so the tests exercise the real
    shutdown path rather than a test-only imitation of it.

    The returned helper still takes the log path, which keeps call sites
    self-documenting about *what* they are waiting for, but it is unused:
    ``flush_all`` drains every writer.
    """

    def _flush(_log_path: Path) -> None:
        from aws_sso_mcp import audit

        audit.flush_all()

    return _flush


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate $HOME, AWS_SSO_MCP_CONFIG, and AWS_SSO_MCP_STATE_DIR to a tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AWS_SSO_MCP_CONFIG", raising=False)
    monkeypatch.setenv("AWS_SSO_MCP_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def sample_config_path(tmp_path: Path) -> Path:
    """Write a minimal valid two-org config file and return its path."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
default_org = "org1"
cache_ttl_hours = 12

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
