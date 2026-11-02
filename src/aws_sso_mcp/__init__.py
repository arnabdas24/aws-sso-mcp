"""aws-sso-mcp: standalone MCP server for AWS IAM Identity Center (SSO).

Multi-org, multi-account AWS querying and management via chat - no dependency
on aws-sso-orchestrator or any other package for its SSO/credential logic.
AWS IAM is the sole authorization boundary: any operation the assumed role
can perform (read or mutating) is dispatched.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# The single source of truth is ``version`` in pyproject.toml, read back from the
# installed package so the two can never disagree. The fallback covers running from
# a bare source tree that was never installed.
try:
    __version__ = version("aws-sso-mcp")
except PackageNotFoundError:  # pragma: no cover - only without an install
    __version__ = "0+unknown"
