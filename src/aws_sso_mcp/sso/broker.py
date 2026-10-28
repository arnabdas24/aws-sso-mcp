"""STS credential retrieval via SSO, with an in-memory per-(org,account,role) cache.

Standalone reimplementation. Never persists credentials to disk - the cache
here is process-memory only and is gone the moment the server exits.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ..awscfg import build_client
from ..errors import AwsCallError
from ..logging_setup import get_logger

logger = get_logger(__name__)

# Refresh this many seconds before actual expiry, so a long-running multi-step
# query never has credentials expire mid-flight.
_REFRESH_MARGIN_SECONDS = 300


@dataclass
class _CachedSession:
    session: boto3.Session
    expires_at: datetime


class CredentialBroker:
    """Mints and caches temporary STS credentials for (org, account, role) keys.

    Thread-safe; intended to be a single shared instance across the process
    so repeated queries for the same account/role never re-mint credentials
    unnecessarily.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], _CachedSession] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get_session(
        self, *, org: str, region: str, account_id: str, role: str, access_token: str
    ) -> boto3.Session:
        """Return a cached or freshly minted boto3 session for this account/role.

        Raises:
            AwsCallError: If ``GetRoleCredentials`` fails (e.g. the role is
                no longer granted).
        """
        key = (org, account_id, role)
        now = datetime.now(UTC)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now:
                self.hits += 1
                return cached.session
            self.misses += 1

        client = build_client("sso", region=region)
        logger.debug("sso:GetRoleCredentials org=%s account=%s role=%s", org, account_id, role)
        try:
            response = client.get_role_credentials(
                accessToken=access_token, roleName=role, accountId=account_id
            )
        except (ClientError, BotoCoreError) as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
            raise AwsCallError(
                "sso",
                "GetRoleCredentials",
                code,
                f"Failed to mint credentials for {account_id}/{role}.",
            ) from exc

        creds = response.get("roleCredentials")
        if not creds:
            raise AwsCallError(
                "sso",
                "GetRoleCredentials",
                "NoCredentials",
                f"No credentials returned for {account_id}/{role}.",
            )

        session = boto3.Session(
            aws_access_key_id=creds["accessKeyId"],
            aws_secret_access_key=creds["secretAccessKey"],
            aws_session_token=creds["sessionToken"],
        )
        # expiration is epoch millis (UTC)
        expires_at = datetime.fromtimestamp(creds["expiration"] / 1000.0, tz=UTC) - timedelta(
            seconds=_REFRESH_MARGIN_SECONDS
        )

        with self._lock:
            self._cache[key] = _CachedSession(session=session, expires_at=expires_at)
        return session

    def invalidate(self, *, org: str, account_id: str, role: str) -> None:
        """Drop a cached session, e.g. after an access-denied at call time."""
        with self._lock:
            self._cache.pop((org, account_id, role), None)

    def stats(self) -> dict[str, int]:
        """Return cheap counters for perf tooling - hits/misses/currently-cached."""
        with self._lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "cached_sessions": len(self._cache),
            }
