"""Central boto3 client factory with a tuned ``botocore.Config`` and safe caching.

Every AWS client in this server is built through :func:`build_client`, so retry
mode, timeouts, connection-pool size, and user-agent are configured in exactly
one place.

Why clients are cached
----------------------
boto3 clients are thread-safe and cheap to *use*, but expensive to *build*:
constructing one resolves the service endpoint, loads the JSON service model,
and sets up a connection pool. A fan-out across hundreds of accounts that built
a fresh client per account would spend most of its wall-clock time in
construction, so clients are reused.

How clients are cached (and why it is done this way)
----------------------------------------------------
A client is only reusable by a caller holding the *same credentials*. Caching
therefore has to be keyed by credential identity, and getting that key wrong is
a security bug, not merely a performance bug.

An earlier version keyed the cache on ``id(session)``. That is unsafe: a
botocore client does not keep its parent :class:`boto3.Session` alive, so a
session could be garbage-collected while its client stayed in the cache. CPython
reuses object addresses aggressively, so a brand-new session *for a different
AWS account* could be allocated at the same address and collide with the stale
entry - handing account B's client to a caller asking for account A.

The fix used here is to hang the cache off the session itself via a
:class:`weakref.WeakKeyDictionary`. This makes the cache lifetime exactly equal
to the session lifetime: entries become unreachable the moment the session they
belong to does, so a stale entry can never outlive - or be confused with - its
credentials. It also bounds memory automatically, with no eviction policy to
tune.
"""

from __future__ import annotations

import threading
import weakref
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from .logging_setup import get_logger

logger = get_logger(__name__)

_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_READ_TIMEOUT = 30.0
_DEFAULT_MAX_ATTEMPTS = 8
_USER_AGENT = "aws-sso-mcp/0.1.0"

# A cache key within one session's client cache. Service and region are the only
# things that distinguish two clients built from the same credentials.
_ClientKey = tuple[str, str]


def build_boto_config(
    *,
    max_pool_connections: int = 64,
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = _DEFAULT_READ_TIMEOUT,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> BotoConfig:
    """Return a ``botocore.Config`` with adaptive retries and a large connection pool.

    ``adaptive`` retry mode measures observed throttling and backs off on its
    own, which matters most for SSO endpoints - they throttle considerably
    harder than typical service APIs.

    Args:
        max_pool_connections: Size of the per-client HTTP connection pool.
        connect_timeout: Seconds to wait for a TCP/TLS connection.
        read_timeout: Seconds to wait for a response body.
        max_attempts: Total botocore attempts, including the first try.

    Returns:
        A ready-to-use ``botocore.Config``.
    """
    return BotoConfig(
        retries={"mode": "adaptive", "max_attempts": max_attempts},
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_pool_connections=max_pool_connections,
        user_agent_extra=_USER_AGENT,
    )


_DEFAULT_CONFIG = build_boto_config()

# Clients built from a credentialed session, keyed by that session. Weak keys
# mean an entry disappears as soon as its session is garbage-collected, so a
# client can never outlive the credentials it was built with.
_SESSION_CLIENTS: weakref.WeakKeyDictionary[boto3.Session, dict[_ClientKey, Any]] = (
    weakref.WeakKeyDictionary()
)

# Clients built from the process-default session - the unauthenticated SSO and
# SSO-OIDC calls that carry an explicit access token instead of credentials.
# There is exactly one default session per process, so a plain dict is correct,
# and it stays small: a handful of services across a handful of regions.
_DEFAULT_CLIENTS: dict[_ClientKey, Any] = {}

_CACHE_LOCK = threading.Lock()


def _cache_for(session: boto3.Session | None) -> dict[_ClientKey, Any]:
    """Return the client cache belonging to ``session``, creating it on first use.

    Must be called with ``_CACHE_LOCK`` held.
    """
    if session is None:
        return _DEFAULT_CLIENTS
    cache = _SESSION_CLIENTS.get(session)
    if cache is None:
        cache = {}
        _SESSION_CLIENTS[session] = cache
    return cache


def _construct_client(
    service: str,
    region: str,
    session: boto3.Session | None,
    config: BotoConfig,
) -> Any:
    """Build one boto3 client, with no caching involved."""
    if session is None:
        return boto3.client(service, region_name=region, config=config)  # type: ignore[call-overload]
    return session.client(service, region_name=region, config=config)  # type: ignore[call-overload]


def build_client(
    service: str,
    *,
    region: str,
    session: boto3.Session | None = None,
    config: BotoConfig | None = None,
) -> Any:
    """Return a cached boto3 client for ``service`` in ``region``.

    Args:
        service: A boto3 service name, e.g. ``"ec2"`` or ``"sso"``.
        region: The region to build the client for. Always explicit - this
            server never defaults a region.
        session: A credentialed session from
            :class:`~aws_sso_mcp.sso.broker.CredentialBroker`, used for calls
            against a specific account/role. ``None`` uses the process-default
            session, which is correct for SSO and SSO-OIDC calls that
            authenticate with an explicit access token rather than credentials.
        config: Optional ``botocore.Config`` override. When supplied the
            resulting client is **not** cached, since the config changes the
            client's behaviour and a caller passing one is assumed to want a
            purpose-built client.

    Returns:
        A boto3 client. Safe to share across threads and to call concurrently.

    Note:
        ``service`` is inherently dynamic here (the caller names any AWS
        service), so the ``boto3-stubs`` ``Literal`` overloads cannot apply and
        the return type is ``Any``. Everything downstream - whether the
        operation exists, and whether its parameters are valid - is still
        validated at runtime by botocore against the service model.
    """
    # A caller-supplied config makes the client non-interchangeable with the
    # shared one, so build a fresh client and skip the cache entirely.
    if config is not None:
        return _construct_client(service, region, session, config)

    key: _ClientKey = (service, region)

    with _CACHE_LOCK:
        cached = _cache_for(session).get(key)
    if cached is not None:
        return cached

    # Construct outside the lock: building a client loads the service model and
    # resolves the endpoint, and holding a process-wide lock across that would
    # serialize the start of every fan-out.
    client = _construct_client(service, region, session, _DEFAULT_CONFIG)

    with _CACHE_LOCK:
        # Another thread may have built the same client while we were building
        # ours. Either is equally valid, so keep whichever landed first and let
        # the duplicate be collected - this guarantees one client per key.
        return _cache_for(session).setdefault(key, client)


def reset_client_cache() -> None:
    """Drop every cached client.

    Intended for tests, which need a clean slate between runs. Safe to call at
    any time: discarding a cached client only forces the next caller to build a
    new one.
    """
    with _CACHE_LOCK:
        _SESSION_CLIENTS.clear()
        _DEFAULT_CLIENTS.clear()
