"""AWS SSO-OIDC device-authorization flow and per-org token cache.

Standalone reimplementation - no dependency on aws-sso-orchestrator. Tokens
are cached one file per org, under the aws-sso-mcp state directory (distinct
from the orchestrator's ``~/.aws_sso/<hash>.json`` scheme), so the two tools
never share or clobber each other's cached sign-in.
"""

from __future__ import annotations

import json
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ..config import PortalConfig
from ..errors import AwsCallError, NoTokenError

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True, slots=True)
class TokenStatus:
    """Cached-token state for one org, without ever exposing the token itself."""

    org: str
    valid: bool
    expires_at: str | None
    reason: str | None = None


def token_cache_path(state_dir: Path, org: str) -> Path:
    """Return the per-org token cache file path."""
    return state_dir / "tokens" / f"{org}.json"


def _build_sso_oidc_client(portal: PortalConfig) -> Any:
    return boto3.client("sso-oidc", region_name=portal.sso_region)


def _read_cached_token(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    except (json.JSONDecodeError, OSError):
        return None


def _write_cached_token(path: Path, access_token: str, expires_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": access_token,
        "expires_at": expires_at.strftime(_TIME_FORMAT),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    path.chmod(0o600)  # owner read/write only - it grants the user's own SSO access


def token_status(state_dir: Path, org: str) -> TokenStatus:
    """Report cached-token validity for ``org`` without ever opening a browser."""
    path = token_cache_path(state_dir, org)
    data = _read_cached_token(path)
    if data is None:
        return TokenStatus(org=org, valid=False, expires_at=None, reason="no cached token")
    try:
        expires_at = datetime.strptime(data["expires_at"], _TIME_FORMAT)
    except (KeyError, ValueError):
        return TokenStatus(org=org, valid=False, expires_at=None, reason="cached token corrupted")
    valid = expires_at > datetime.now()
    return TokenStatus(
        org=org,
        valid=valid,
        expires_at=data["expires_at"],
        reason=None if valid else "expired",
    )


def login(state_dir: Path, portal: PortalConfig, *, on_verification_url: Any = None) -> TokenStatus:
    """Run the device-authorization flow for ``portal``, caching the resulting token.

    Args:
        state_dir: The aws-sso-mcp state directory.
        portal: The org to authenticate against.
        on_verification_url: Optional callback invoked with the verification
            URL before the browser is opened (e.g. for the caller to also
            surface it as text, since a headless environment may not have a
            usable browser).

    Returns:
        The resulting :class:`TokenStatus` (``valid=True`` on success).

    Raises:
        AwsCallError: If the device-authorization flow fails.
    """
    client = _build_sso_oidc_client(portal)
    try:
        register = client.register_client(clientName="aws-sso-mcp", clientType="public")
        client_id = register["clientId"]
        client_secret = register["clientSecret"]

        auth = client.start_device_authorization(
            clientId=client_id, clientSecret=client_secret, startUrl=portal.start_url
        )
        device_code = auth["deviceCode"]
        verification_url = auth["verificationUriComplete"]

        if on_verification_url is not None:
            on_verification_url(verification_url)
        webbrowser.open(verification_url)

        while True:
            try:
                token_response = client.create_token(
                    clientId=client_id,
                    clientSecret=client_secret,
                    grantType="urn:ietf:params:oauth:grant-type:device_code",
                    deviceCode=device_code,
                )
                break
            except client.exceptions.AuthorizationPendingException:
                time.sleep(5)
            except client.exceptions.SlowDownException:
                time.sleep(10)
            except client.exceptions.ExpiredTokenException as exc:
                raise AwsCallError(
                    "sso-oidc",
                    "CreateToken",
                    "ExpiredTokenException",
                    "Device authorization code expired before approval.",
                ) from exc

        access_token: str = token_response["accessToken"]
        expires_at = datetime.now() + timedelta(seconds=token_response["expiresIn"])
        _write_cached_token(token_cache_path(state_dir, portal.name), access_token, expires_at)
        return TokenStatus(
            org=portal.name, valid=True, expires_at=expires_at.strftime(_TIME_FORMAT)
        )

    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise AwsCallError("sso-oidc", "device_authorization", code, "Login failed.") from exc


def get_access_token(state_dir: Path, portal: PortalConfig, *, auto_login: bool = True) -> str:
    """Return a valid cached access token for ``portal``, logging in if needed/allowed.

    Args:
        state_dir: The aws-sso-mcp state directory.
        portal: The org to get a token for.
        auto_login: If ``False`` and no valid cached token exists, raises
            :class:`NoTokenError` instead of opening a browser - for callers
            that want to report "needs login" rather than block.

    Raises:
        NoTokenError: If no valid token is cached and ``auto_login`` is ``False``.
    """
    status = token_status(state_dir, portal.name)
    if not status.valid:
        if not auto_login:
            raise NoTokenError(
                f"No valid SSO token cached for org {portal.name!r} ({status.reason}). "
                "Call login() to authenticate."
            )
        login(state_dir, portal)

    data = _read_cached_token(token_cache_path(state_dir, portal.name))
    if data is None:
        # Should be unreachable: login() just wrote it, or status.valid was
        # already True. Treated as a clean error rather than a bare assert
        # so this security-relevant path never raises an unguarded AssertionError.
        raise NoTokenError(f"Token cache for org {portal.name!r} unexpectedly empty after login.")
    return str(data["access_token"])
