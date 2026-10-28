"""aws-sso-mcp: standalone MCP server for AWS IAM Identity Center (SSO).

Multi-org, multi-account AWS querying and management via chat - no dependency
on aws-sso-orchestrator or any other package for its SSO/credential logic.
AWS IAM is the sole authorization boundary: any operation the assumed role
can perform (read or mutating) is dispatched.
"""

from __future__ import annotations

__version__ = "0.1.0"  # x-release-please-version
