"""AWS account/role discovery via IAM Identity Center (SSO), paginated.

Standalone reimplementation - takes a bare access token + region rather than
any orchestrator ``Config``/``PortalConfig`` type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from ..awscfg import build_client
from ..errors import AwsCallError
from ..logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SsoAccount:
    """One AWS account reachable via IAM Identity Center."""

    account_id: str
    account_name: str


def _build_sso_client(region: str) -> Any:
    """Return a boto3 ``sso`` client for ``region``.

    Typed as ``Any`` rather than ``object`` because boto3 generates its service
    methods at runtime, so no static type exists for ``list_accounts`` and
    friends. ``object`` forced a ``type: ignore`` at every call site, which
    suppressed *all* type checking there - including real mistakes in the
    surrounding code - whereas ``Any`` scopes the unavoidable looseness to the
    client itself.
    """
    return build_client("sso", region=region)


def list_accounts(region: str, access_token: str) -> list[SsoAccount]:
    """Return every AWS account reachable via SSO with the given access token.

    Raises:
        AwsCallError: If the SSO ``ListAccounts`` call fails.
    """
    client = _build_sso_client(region)
    logger.debug("sso:ListAccounts region=%s", region)
    try:
        accounts: list[dict[str, str]] = []
        next_token: str | None = None
        while True:
            kwargs = {"accessToken": access_token, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = client.list_accounts(**kwargs)
            accounts.extend(response.get("accountList", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
        return [
            SsoAccount(account_id=a["accountId"], account_name=a["accountName"]) for a in accounts
        ]
    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise AwsCallError("sso", "ListAccounts", code, "Failed to list SSO accounts.") from exc


def list_account_roles(region: str, access_token: str, account_id: str) -> list[str]:
    """Return every role name granted on ``account_id`` via the current SSO session.

    Raises:
        AwsCallError: If the SSO ``ListAccountRoles`` call fails.
    """
    client = _build_sso_client(region)
    logger.debug("sso:ListAccountRoles region=%s account_id=%s", region, account_id)
    roles: list[str] = []
    next_token: str | None = None
    try:
        while True:
            kwargs = {"accessToken": access_token, "accountId": account_id, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = client.list_account_roles(**kwargs)
            roles.extend(role["roleName"] for role in response.get("roleList", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise AwsCallError(
            "sso", "ListAccountRoles", code, f"Failed to list roles for account {account_id}."
        ) from exc
    return roles
