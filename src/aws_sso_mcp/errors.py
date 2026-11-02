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


class RegionError(McpError):
    """Raised for a missing, unknown, or unresolvable region."""


# AWS error codes that mean "these credentials are not allowed to do this".
# A stale cached role is the most common cause, so the server treats these as a
# signal to drop its caches and retry once rather than as a terminal failure.
_ACCESS_DENIED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "UnauthorizedException",
        "ForbiddenException",
        "InvalidClientTokenId",
        "ExpiredToken",
        "ExpiredTokenException",
    }
)


# Transport-level failure codes that no amount of AWS permission will fix, mapped
# to the one thing the user can actually act on. ``AwsCallError`` is constructed
# with ``type(exc).__name__`` when botocore raises a connection error rather than
# a service error, so these keys are botocore exception class names - which is why
# this module still needs no botocore import.
_CONNECTIVITY_HINTS = {
    "SSLError": (
        "TLS verification failed, so the certificate presented was not trusted. This is "
        "usually a corporate proxy intercepting HTTPS. Point AWS_CA_BUNDLE at a PEM file "
        "holding your proxy's root CA, set it in your MCP client's env block as an absolute "
        'path, and restart the client. Verify the bundle first with: python -c "import '
        "boto3; boto3.client('sso-oidc', region_name='us-east-1').register_client("
        "clientName='t', clientType='public')\""
    ),
}


class AwsCallError(McpError):
    """A clean-message wrapper around any AWS-side call failure.

    Never constructed from a raw exception's ``str()`` or ``repr()``: callers
    extract only the AWS error code and message, because botocore exceptions can
    embed partial request and response data.

    Attributes:
        service: The boto3 service name the call was made against.
        operation: The PascalCase operation name.
        code: The AWS error code, e.g. ``"AccessDenied"`` or ``"Throttling"``.
    """

    def __init__(self, service: str, operation: str, code: str, message: str) -> None:
        hint = _CONNECTIVITY_HINTS.get(code)
        if hint:
            message = f"{message} {hint}"
        super().__init__(f"{service}:{operation} failed ({code}): {message}")
        self.service = service
        self.operation = operation
        self.code = code

    @property
    def is_access_denied(self) -> bool:
        """Whether this failure looks like a permissions or expired-credential problem.

        Used to decide whether invalidating the cached role and credentials and
        retrying once is worth attempting.
        """
        return self.code in _ACCESS_DENIED_CODES
