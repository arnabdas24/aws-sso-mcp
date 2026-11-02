"""AWS SSO-OIDC device-authorization flow and per-org token cache.

Standalone reimplementation - no dependency on aws-sso-orchestrator. Tokens
are cached one file per org, under the aws-sso-mcp state directory (distinct
from the orchestrator's ``~/.aws_sso/<hash>.json`` scheme), so the two tools
never share or clobber each other's cached sign-in.

An in-memory layer sits above the on-disk cache: once a token has been read
this process, a fan-out doesn't re-read the file per account.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from ..awscfg import build_client
from ..config import PortalConfig
from ..errors import AwsCallError, NoTokenError
from ..logging_setup import get_logger

logger = get_logger(__name__)

# Cached expiry timestamps are always written in UTC. The trailing "Z" makes
# that explicit, so a cache file is unambiguous no matter which machine or
# timezone reads it.
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Fallbacks used only if StartDeviceAuthorization omits these fields; AWS
# normally supplies both and its values take precedence.
_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_DEVICE_CODE_TTL_SECONDS = 600.0

# Extra delay added to the poll interval each time AWS returns SlowDown.
_SLOW_DOWN_BACKOFF_SECONDS = 5.0

# How often to remind the user that we are still waiting for their approval.
_WAIT_NOTICE_INTERVAL_SECONDS = 15.0

# A token that is still technically valid but about to lapse is treated as
# unusable. A wide fan-out can run for minutes, and AWS rejects the token the
# instant it expires, so handing out a nearly-dead token turns a clean
# "please log in" into a partial failure somewhere in the middle of a run.
_EXPIRY_MARGIN = timedelta(minutes=5)


def _is_usable(expires_at: datetime, now: datetime) -> bool:
    """Return whether a token outlasts the safety margin, not merely ``now``."""
    return expires_at > now + _EXPIRY_MARGIN


def _unusable_reason(expires_at: datetime, now: datetime) -> str:
    """Explain why a token was rejected, distinguishing lapsed from about-to-lapse."""
    if expires_at <= now:
        return "expired"
    return f"expiring within {int(_EXPIRY_MARGIN.total_seconds() // 60)} minutes"


@dataclass(frozen=True, slots=True)
class TokenStatus:
    """Cached-token state for one org, without ever exposing the token itself."""

    org: str
    valid: bool
    expires_at: str | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _MemToken:
    access_token: str
    expires_at: datetime


_MEM_CACHE: dict[str, _MemToken] = {}
_MEM_LOCK = threading.Lock()


def token_cache_path(state_dir: Path, org: str) -> Path:
    """Return the per-org token cache file path."""
    return state_dir / "tokens" / f"{org}.json"


def _build_sso_oidc_client(portal: PortalConfig) -> Any:
    return build_client("sso-oidc", region=portal.sso_region)


def _read_cached_token(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return loaded


def _parse_expiry(raw: str) -> datetime:
    """Parse a cached ``expires_at`` string into an aware UTC datetime.

    Args:
        raw: The timestamp string read from the token cache file.

    Returns:
        A timezone-aware datetime in UTC.

    Raises:
        ValueError: If ``raw`` is not in the expected format.
    """
    return datetime.strptime(raw, _TIME_FORMAT).replace(tzinfo=UTC)


def _write_cached_token(path: Path, access_token: str, expires_at: datetime) -> None:
    """Atomically write the token cache file, restricted to the current user.

    The payload is written to a private temporary file in the same directory and
    then renamed over the target. A concurrent reader - including the
    cross-process fall-through in :func:`token_status` - therefore sees either
    the complete old file or the complete new one, never a truncated one.

    On POSIX this yields mode 0600. Windows ignores POSIX permission bits, so the
    file instead relies on the state directory's inherited ACL; that is the same
    protection the AWS CLI's own SSO cache gets.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": access_token,
        "expires_at": expires_at.strftime(_TIME_FORMAT),
    }
    # The pid in the temp name keeps two processes refreshing the same org from
    # truncating each other's half-written file before either one renames.
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # Create with 0600 from the start so the secret is never briefly readable
    # under the default umask. os.replace then carries that mode onto the target.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
            # fsync before the rename: without it a crash can leave the renamed
            # file present but empty, which would look like a corrupted cache.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        # No-op on the success path, where the rename already consumed it.
        tmp_path.unlink(missing_ok=True)
    path.chmod(0o600)  # owner read/write only - it grants the user's own SSO access


def _put_mem(org: str, access_token: str, expires_at: datetime) -> None:
    with _MEM_LOCK:
        _MEM_CACHE[org] = _MemToken(access_token=access_token, expires_at=expires_at)


def _get_mem(org: str) -> _MemToken | None:
    with _MEM_LOCK:
        return _MEM_CACHE.get(org)


def _drop_mem(org: str) -> None:
    with _MEM_LOCK:
        _MEM_CACHE.pop(org, None)


def token_status(state_dir: Path, org: str) -> TokenStatus:
    """Report cached-token validity for ``org`` without ever opening a browser.

    Checks the in-process cache first, then the on-disk cache. A token found on
    disk and still valid is promoted into the in-process cache so that a wide
    fan-out does not re-read the same file once per account.

    Args:
        state_dir: Root of the server's state directory.
        org: Org alias whose token should be inspected.

    Returns:
        A :class:`TokenStatus` describing validity and, when known, expiry.
    """
    now = datetime.now(UTC)
    mem = _get_mem(org)
    if mem is not None and _is_usable(mem.expires_at, now):
        return TokenStatus(
            org=org, valid=True, expires_at=mem.expires_at.strftime(_TIME_FORMAT), reason=None
        )
    # An expired in-memory entry falls through to disk: another process may
    # have refreshed the file since this one cached the old token.

    data = _read_cached_token(token_cache_path(state_dir, org))
    if data is None:
        return TokenStatus(org=org, valid=False, expires_at=None, reason="no cached token")
    try:
        expires_at = _parse_expiry(data["expires_at"])
        access_token: str = data["access_token"]
    except (KeyError, TypeError, ValueError):
        return TokenStatus(org=org, valid=False, expires_at=None, reason="cached token corrupted")

    valid = _is_usable(expires_at, now)
    if valid:
        _put_mem(org, access_token, expires_at)
    return TokenStatus(
        org=org,
        valid=valid,
        expires_at=expires_at.strftime(_TIME_FORMAT),
        reason=None if valid else _unusable_reason(expires_at, now),
    )


def login(state_dir: Path, portal: PortalConfig) -> TokenStatus:
    """Run the device-authorization flow for ``portal``, caching the resulting token.

    Args:
        state_dir: The aws-sso-mcp state directory.
        portal: The org to authenticate against.

    Returns:
        The resulting :class:`TokenStatus` (``valid=True`` on success).

    Raises:
        AwsCallError: If the device-authorization flow fails.
    """
    client = _build_sso_oidc_client(portal)
    logger.info("SSO device-auth starting for org=%s", portal.name)
    try:
        register = client.register_client(clientName="aws-sso-mcp", clientType="public")
        client_id = register["clientId"]
        client_secret = register["clientSecret"]

        auth = client.start_device_authorization(
            clientId=client_id, clientSecret=client_secret, startUrl=portal.start_url
        )
        device_code = auth["deviceCode"]
        # verificationUriComplete (URL with the user code pre-filled) is optional
        # in the API. Fall back to the plain URL, and always surface the user
        # code so the user can type it in manually if needed.
        verification_url = auth.get("verificationUriComplete") or auth["verificationUri"]
        user_code = auth.get("userCode", "")

        # AWS tells us how often it wants to be polled and how long the code is
        # valid for. Honoring both avoids being throttled into SlowDown, and
        # gives this loop a hard end - an earlier version looped forever and
        # relied entirely on the service eventually returning ExpiredToken,
        # which could block a tool call (and an MCP worker thread) indefinitely
        # if the user simply walked away.
        poll_interval = float(auth.get("interval", _DEFAULT_POLL_INTERVAL_SECONDS))
        expires_in = float(auth.get("expiresIn", _DEFAULT_DEVICE_CODE_TTL_SECONDS))
        deadline = time.monotonic() + expires_in

        # These records are forwarded to the chat client (see MCPContextHandler),
        # so the link is visible there even when no browser can be opened.
        logger.info(
            "Signing in to org '%s'. A browser window should open. If it does not, "
            "open this link manually to approve: %s (code: %s)",
            portal.name,
            verification_url,
            user_code or "n/a",
        )
        # webbrowser.open reports "no browser available" by returning False, not by raising.
        opened = False
        try:
            opened = webbrowser.open(verification_url)
        except Exception as exc:  # noqa: BLE001 - headless hosts have no browser
            logger.debug("Browser auto-open raised: %s", exc)
        if not opened:
            logger.warning(
                "Could not open a browser automatically. Open this link manually to "
                "sign in to org '%s': %s (code: %s)",
                portal.name,
                verification_url,
                user_code or "n/a",
            )

        token_response = _poll_for_token(
            client,
            client_id=client_id,
            client_secret=client_secret,
            device_code=device_code,
            org=portal.name,
            poll_interval=poll_interval,
            deadline=deadline,
        )

        access_token: str = token_response["accessToken"]
        expires_at = datetime.now(UTC) + timedelta(seconds=token_response["expiresIn"])
        _write_cached_token(token_cache_path(state_dir, portal.name), access_token, expires_at)
        _put_mem(portal.name, access_token, expires_at)
        logger.info(
            "SSO login OK for org=%s, expires at %s",
            portal.name,
            expires_at.strftime(_TIME_FORMAT),
        )
        return TokenStatus(
            org=portal.name, valid=True, expires_at=expires_at.strftime(_TIME_FORMAT)
        )

    except (ClientError, BotoCoreError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise AwsCallError("sso-oidc", "device_authorization", code, "Login failed.") from exc


def _poll_for_token(
    client: Any,
    *,
    client_id: str,
    client_secret: str,
    device_code: str,
    org: str,
    poll_interval: float,
    deadline: float,
) -> dict[str, Any]:
    """Poll ``CreateToken`` until the user approves, the code expires, or we time out.

    Args:
        client: The SSO-OIDC client to poll.
        client_id: Client id from ``RegisterClient``.
        client_secret: Client secret from ``RegisterClient``.
        device_code: Device code from ``StartDeviceAuthorization``.
        org: Org alias, used only for log messages.
        poll_interval: Seconds between polls, as requested by AWS.
        deadline: ``time.monotonic()`` value after which to give up.

    Returns:
        The successful ``CreateToken`` response.

    Raises:
        AwsCallError: If the device code expires, or the deadline passes
            without the user approving the sign-in.
    """
    last_notice = 0.0
    while True:
        try:
            response: dict[str, Any] = client.create_token(
                clientId=client_id,
                clientSecret=client_secret,
                grantType="urn:ietf:params:oauth:grant-type:device_code",
                deviceCode=device_code,
            )
        except client.exceptions.AuthorizationPendingException:
            pass
        except client.exceptions.SlowDownException:
            # AWS is asking us to back off; widen the interval permanently
            # rather than sleeping once, since the pressure usually persists.
            poll_interval += _SLOW_DOWN_BACKOFF_SECONDS
        except client.exceptions.ExpiredTokenException as exc:
            raise AwsCallError(
                "sso-oidc",
                "CreateToken",
                "ExpiredTokenException",
                "The device authorization code expired before the sign-in was approved. "
                "Call login() again and approve the browser prompt.",
            ) from exc
        else:
            return response

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AwsCallError(
                "sso-oidc",
                "CreateToken",
                "LoginTimeout",
                "Timed out waiting for the SSO sign-in to be approved in the browser. "
                "Call login() again when you are ready to approve it.",
            )

        # Never sleep past the deadline - it keeps the final wait honest and
        # guarantees the loop ends promptly rather than one interval late.
        time.sleep(min(poll_interval, remaining))

        # Periodic reassurance that the server is still waiting, without
        # emitting a line on every single poll.
        waited = time.monotonic()
        if waited - last_notice >= _WAIT_NOTICE_INTERVAL_SECONDS:
            last_notice = waited
            logger.info(
                "Still waiting for SSO approval for org=%s (%.0fs remaining)…",
                org,
                max(0.0, deadline - waited),
            )


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
    mem = _get_mem(portal.name)
    if mem is not None and _is_usable(mem.expires_at, datetime.now(UTC)):
        return mem.access_token

    status = token_status(state_dir, portal.name)
    if not status.valid:
        if not auto_login:
            raise NoTokenError(
                f"No valid SSO token cached for org {portal.name!r} ({status.reason}). "
                "Call login() to authenticate."
            )
        _drop_mem(portal.name)
        login(state_dir, portal)

    mem = _get_mem(portal.name)
    if mem is not None:
        return mem.access_token

    data = _read_cached_token(token_cache_path(state_dir, portal.name))
    if data is None:
        # Should be unreachable: login() just wrote it, or status.valid was
        # already True. Treated as a clean error rather than a bare assert
        # so this security-relevant path never raises an unguarded AssertionError.
        raise NoTokenError(f"Token cache for org {portal.name!r} unexpectedly empty after login.")
    return str(data["access_token"])
