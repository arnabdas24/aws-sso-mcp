"""Two-lane concurrent execution for multi-account fan-out.

The MCP server itself is asyncio-based but boto3/botocore are synchronous,
so every AWS call must be dispatched onto a thread. Two separate pools are
used because the two kinds of work have very different cost profiles:

* **credential lane** (small, e.g. 10 workers): ``sso:GetRoleCredentials``
  calls. Kept small deliberately - SSO endpoints are far more likely to
  throttle than a typical service API, and the :class:`CredentialBroker`
  cache means most accounts only pay this cost once per session anyway.
* **service lane** (larger, e.g. 50 workers): the actual read call
  (``ec2:DescribeInstances`` etc.) once credentials are in hand. This is
  where the bulk of the 700-account fan-out concurrency comes from.

Both pools are process-wide singletons owned by one :class:`QueryExecutor`,
so callers never create their own thread pools.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class TargetResult[R]:
    """One target's outcome from a fan-out call - success and failure both first-class.

    A fan-out across hundreds of accounts must never let one account's
    failure (access denied, region disabled, throttling, ...) abort the
    whole query - every target gets its own result or its own clean error.
    """

    org: str
    account_id: str
    account_name: str
    ok: bool
    value: R | None = None
    error: str | None = None


class QueryExecutor:
    """Owns the credential-lane and service-lane thread pools for one process."""

    def __init__(self, *, credential_workers: int = 10, service_workers: int = 50) -> None:
        self._credential_pool = ThreadPoolExecutor(
            max_workers=credential_workers, thread_name_prefix="aws-sso-mcp-cred"
        )
        self._service_pool = ThreadPoolExecutor(
            max_workers=service_workers, thread_name_prefix="aws-sso-mcp-svc"
        )

    def run_credential_lane(self, fn: Callable[[], _T]) -> _T:
        """Run one credential-minting call on the (small) credential lane, blocking."""
        return self._credential_pool.submit(fn).result()

    def run_service_lane(self, fn: Callable[[], _T]) -> _T:
        """Run one service call on the (large) service lane, blocking."""
        return self._service_pool.submit(fn).result()

    def fan_out[R](
        self,
        targets: list[tuple[str, str, str]],
        *,
        credential_fn: Callable[[str, str, str], object],
        service_fn: Callable[[str, str, str, object], R],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[TargetResult[R]]:
        """Run ``credential_fn`` then ``service_fn`` for every ``(org, account_id, account_name)``.

        Each target is fully isolated: a credential or service failure for
        one account produces a failed :class:`TargetResult` for that account
        only, never an exception that aborts the whole fan-out.

        A short-lived dispatch pool (sized to the target count, capped) is
        used to coordinate each target's two-step credential-then-service
        call. Dispatch threads spend almost all their time blocked on
        ``.result()``, so they must never be drawn from ``_credential_pool``
        or ``_service_pool`` themselves - submitting a blocking-wait task
        from inside one of those pools onto that same pool can deadlock
        once every worker is a waiting dispatcher.

        ``on_progress``, if given, is invoked as ``(completed_count,
        total_count)`` every time one target finishes (success or failure).
        It runs on a dispatch-pool thread and any exception it raises is
        swallowed - progress reporting must never break a query.
        """
        if not targets:
            return []

        def _one(org: str, account_id: str, account_name: str) -> TargetResult[R]:
            try:
                creds = self.run_credential_lane(
                    lambda: credential_fn(org, account_id, account_name)
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-target failures
                return TargetResult(
                    org=org,
                    account_id=account_id,
                    account_name=account_name,
                    ok=False,
                    error=str(exc),
                )
            try:
                value = self.run_service_lane(
                    lambda: service_fn(org, account_id, account_name, creds)
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-target failures
                return TargetResult(
                    org=org,
                    account_id=account_id,
                    account_name=account_name,
                    ok=False,
                    error=str(exc),
                )
            return TargetResult(
                org=org, account_id=account_id, account_name=account_name, ok=True, value=value
            )

        total = len(targets)
        dispatch_workers = min(total, 200)
        with ThreadPoolExecutor(
            max_workers=dispatch_workers, thread_name_prefix="aws-sso-mcp-dispatch"
        ) as dispatch_pool:
            futures = [
                dispatch_pool.submit(_one, org, account_id, account_name)
                for org, account_id, account_name in targets
            ]
            results: list[TargetResult[R]] = []
            for completed, future in enumerate(as_completed(futures), start=1):
                results.append(future.result())
                if on_progress is not None:
                    # Progress reporting is best-effort only - it must never
                    # break a query (e.g. client disconnected, no listener).
                    with contextlib.suppress(Exception):
                        on_progress(completed, total)
            return results

    def shutdown(self) -> None:
        self._credential_pool.shutdown(wait=True)
        self._service_pool.shutdown(wait=True)
