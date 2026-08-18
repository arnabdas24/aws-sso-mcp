"""Typed, user-facing errors for aws-sso-mcp.

Every exception a tool can raise back to an MCP client is one of the types
defined here. Raw ``botocore``/``boto3`` exceptions (which can embed partial
request/response data, including near-secret material in their ``repr``)
must never cross a tool boundary directly - callers catch
``(ClientError, BotoCoreError)`` at the edge and re-raise one of these with a
clean, one-line message instead.
"""

from __future__ import annotations


class McpError(RuntimeError):
    """Base class for every clean, user-facing error this server raises."""


class ConfigError(McpError):
    """Raised for a problem loading or validating the TOML configuration."""


class OrgNotFoundError(McpError):
    """Raised when a referenced org name is not present in the configuration."""

    def __init__(self, org: str, known: list[str]) -> None:
        known_str = ", ".join(sorted(known)) if known else "(none configured)"
        super().__init__(f"Unknown org {org!r}. Configured orgs: {known_str}.")
        self.org = org


class AmbiguousAccountError(McpError):
    """Raised when an account name resolves to more than one account/org."""

    def __init__(self, name: str, matches: list[tuple[str, str]]) -> None:
        rendered = ", ".join(f"{org}/{account_id}" for org, account_id in matches)
        super().__init__(
            f"Account name {name!r} is ambiguous - found in multiple places: {rendered}. "
            "Disambiguate with org= or account_id="
        )
        self.name = name
        self.matches = matches


class AccountNotFoundError(McpError):
    """Raised when an account name/id cannot be resolved in the given scope."""


class NoTokenError(McpError):
    """Raised when a valid SSO token is required but none is cached and auto-login is disabled."""


class AuthPendingError(McpError):
    """Raised (non-fatal) while a device-authorization flow is awaiting browser approval."""


class RoleResolutionError(McpError):
    """Raised when a role cannot be determined for an account (no mapping, no grant, ambiguous)."""

    def __init__(
        self, account_id: str, account_name: str, reason: str, available: list[str]
    ) -> None:
        available_str = ", ".join(available) if available else "(none discoverable)"
        super().__init__(
            f"Could not resolve a role for account {account_name!r} ({account_id}): {reason}. "
            f"Available roles: {available_str}."
        )
        self.account_id = account_id
        self.account_name = account_name
        self.reason = reason
        self.available = available


class OperationDeniedError(McpError):
    """Raised when a requested AWS operation is a mutating call or otherwise disallowed."""


class RegionError(McpError):
    """Raised for an unknown or unresolvable region."""


class AwsCallError(McpError):
    """Raised for a clean-message wrapper around any AWS-side call failure.

    Never constructed from a raw exception's ``str()``/``repr()`` directly -
    callers extract only the AWS error code/message fields they need.
    """

    def __init__(self, service: str, operation: str, code: str, message: str) -> None:
        super().__init__(f"{service}:{operation} failed ({code}): {message}")
        self.service = service
        self.operation = operation
        self.code = code
