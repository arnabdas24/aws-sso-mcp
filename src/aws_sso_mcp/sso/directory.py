"""AWS account/role discovery via IAM Identity Center (SSO), paginated.

Standalone reimplementation - takes a bare access token + region rather than
any orchestrator ``Config``/``PortalConfig`` type.
"""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ..errors import AwsCallError


@dataclass(frozen=True, slots=True)
class SsoAccount:
    """One AWS account reachable via IAM Identity Center."""

    account_id: str
    account_name: str


def _build_sso_client(region: str) -> object:
    return boto3.client("sso", region_name=region)


def list_accounts(region: str, access_token: str) -> list[SsoAccount]:
    """Return every AWS account reachable via SSO with the given access token.

    Raises:
        AwsCallError: If the SSO ``ListAccounts`` call fails.
    """
    client = _build_sso_client(region)
    try:
        accounts: list[dict[str, str]] = []
        next_token: str | None = None
        while True:
            kwargs = {"accessToken": access_token, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = client.list_accounts(**kwargs)  # type: ignore[attr-defined]
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
    try:
        roles: list[str] = []
        next_token: str | None = None
        while True:
            kwargs = {"accessToken": access_token, "accountId": account_id, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            response = client.list_account_roles(**kwargs)  # type: ignore[attr-defined]
            roles.extend(role["roleName"] for role in response.get("roleList", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
        return roles
    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise AwsCallError(
            "sso", "ListAccountRoles", code, f"Failed to list roles for account {account_id}."
        ) from exc
