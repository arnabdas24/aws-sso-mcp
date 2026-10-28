"""Two-lane concurrent execution for multi-account fan-out.

The MCP server is asyncio-based but boto3 is synchronous, so every AWS call must
be dispatched onto a thread. Three pools are used, each for a distinct reason:

* **credential lane** (small, ~10 workers): ``sso:GetRoleCredentials`` and
  ``sso:ListAccountRoles`` calls. Kept deliberately small because SSO endpoints
  throttle far harder than service APIs, and because
  :class:`~aws_sso_mcp.sso.broker.CredentialBroker` caches results so most
  accounts pay this cost only once per session.
* **service lane** (large, ~50 workers): the actual API call once credentials
  are in hand. This is where fan-out throughput comes from.
* **dispatch lane**: threads that do no work themselves - each one just chains
  "get credentials, then make the call" for a single account and spends almost
  all its time blocked. These must never be drawn from the other two pools:
  submitting a blocking wait from inside a pool onto that same pool deadlocks
  as soon as every worker is a waiting dispatcher.

All three pools are owned by one long-lived :class:`QueryExecutor`, so callers
never create their own and repeated queries do not pay thread-creation costs.

Timeout semantics
-----------------
``per_target_timeout_seconds`` bounds how long *one account's work* may take.
It deliberately does **not** include time spent queued behind other accounts.
An earlier version passed the timeout straight to ``Future.result()``, which
measures from submission - so in a 700-account fan-out through a 10-worker
lane, the accounts at the back of the queue were reported as timing out having
never actually run.

The budget is therefore a stopwatch rather than a wall-clock deadline: each
step signals when a lane worker actually picks it up, the budget runs only
from that point until the step finishes, and the wait is bounded by whatever
is left. Queue time is free, slow work is cut off promptly, and a result that
arrives inside the budget is always kept.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from ..logging_setup import get_logger

logger = get_logger(__name__)

# Upper bound on dispatch threads. Dispatch threads are almost always blocked,
# so this can comfortably exceed the worker lanes; it exists only to stop a
# pathologically large account list from creating unbounded threads.
_MAX_DISPATCH_WORKERS = 200


@dataclass(frozen=True, slots=True)
class TargetResult[R]:
    """One account's outcome from a fan-out, with success and failure equally first-class.

    A fan-out across hundreds of accounts must never let a single account's
    failure - access denied, region disabled, throttling - abort the whole
    query. Every target gets either a value or its own clean error message.

    Attributes:
        org: The org alias the account belongs to.
        account_id: The 12-digit AWS account id.
        account_name: The account's display name from the SSO directory.
        ok: Whether the call succeeded for this account.
        value: The successful result, or ``None`` when ``ok`` is false.
        error: A clean, user-facing error message, or ``None`` on success.
    """

    org: str
    account_id: str
    account_name: str
    ok: bool
    value: R | None = None
    error: str | None = None


class _StepError(Exception):
    """One submitted step failed or outran its budget.

    Carries the already user-facing message so the dispatch thread can turn
    any step's failure into a failed :class:`TargetResult` in one place,
    instead of threading an optional error through every call site.
    """


class _Deadline:
    """A budget of *active execution* time for one target, paused while queued.

    A plain wall-clock deadline is wrong here. The budget is created on the
    dispatch thread, but each step then has to queue for a slot in its worker
    lane; in a wide fan-out through a 50-worker service lane, most targets sit
    in that queue far longer than they take to run. Charging that wait against
    the budget would report the accounts at the back of the queue as having
    timed out without ever having run - and would do so *more* the wider the
    fan-out, which is exactly backwards.

    So the budget is only consumed between :meth:`resume` and :meth:`pause`,
    which bracket the part of each step that is genuinely executing.
    """

    __slots__ = ("_remaining", "_running_since", "_timeout")

    def __init__(self, timeout: float | None) -> None:
        self._timeout = timeout
        self._remaining = timeout
        self._running_since: float | None = None

    def resume(self) -> None:
        """Start charging elapsed time against the budget."""
        self._running_since = time.monotonic()

    def pause(self) -> None:
        """Stop charging, deducting whatever ran since :meth:`resume`."""
        if self._running_since is not None and self._remaining is not None:
            self._remaining = max(0.0, self._remaining - (time.monotonic() - self._running_since))
        self._running_since = None

    def remaining(self) -> float | None:
        """Seconds of budget left, or ``None`` when the timeout is disabled.

        Never negative: an already-exhausted budget returns ``0.0``, which
        makes ``Future.result(timeout=...)`` fail immediately rather than
        blocking.
        """
        return self._remaining

    @property
    def timeout(self) -> float | None:
        """The configured timeout in seconds, or ``None`` if unbounded."""
        return self._timeout


class QueryExecutor:
    """Owns the credential, service, and dispatch thread pools for one process.

    A single instance is built by :class:`~aws_sso_mcp.state.AppState` and shared
    by every tool call.
    """

    def __init__(
        self,
        *,
        credential_workers: int = 10,
        service_workers: int = 50,
        per_target_timeout_seconds: float | None = None,
        progress_min_interval_seconds: float = 0.25,
    ) -> None:
        """Build the executor's thread pools.

        Args:
            credential_workers: Size of the SSO/credential lane. Keep small to
                stay under SSO throttling limits.
            service_workers: Size of the service-call lane. This is the main
                fan-out concurrency knob.
            per_target_timeout_seconds: How long one account's work may take,
                excluding queue wait. ``None`` disables the timeout.
            progress_min_interval_seconds: Minimum gap between progress
                callbacks, to avoid flooding the client on fast queries.
        """
        self._credential_pool = ThreadPoolExecutor(
            max_workers=credential_workers, thread_name_prefix="aws-sso-mcp-cred"
        )
        self._service_pool = ThreadPoolExecutor(
            max_workers=service_workers, thread_name_prefix="aws-sso-mcp-svc"
        )
        # Long-lived rather than per-call: building a 200-thread pool for every
        # query cost more than the queries themselves on small fan-outs.
        self._dispatch_pool = ThreadPoolExecutor(
            max_workers=_MAX_DISPATCH_WORKERS, thread_name_prefix="aws-sso-mcp-dispatch"
        )
        self._per_target_timeout = per_target_timeout_seconds
        self._progress_interval = max(0.0, progress_min_interval_seconds)
        self.credential_workers = credential_workers
        self.service_workers = service_workers

    def map_credential_lane[T](self, calls: list[Callable[[], T]]) -> list[T | None]:
        """Run many credential-lane calls concurrently, returning results in order.

        A call that raises yields ``None`` in its slot rather than propagating,
        because every caller of this method treats credential-lane work as a
        best-effort warm-up: a failure here should degrade to an on-demand
        lookup later, not abort the whole query.

        Args:
            calls: Zero-argument callables to run on the credential lane.

        Returns:
            One entry per input call, in the same order, with ``None`` for any
            call that raised.
        """
        futures = [self._credential_pool.submit(call) for call in calls]
        results: list[T | None] = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - best-effort warm-up
                logger.warning("Credential-lane prefetch failed: %s", exc)
                results.append(None)
        return results

    def fan_out[R](
        self,
        targets: list[tuple[str, str, str]],
        *,
        credential_fn: Callable[[str, str, str], object],
        service_fn: Callable[[str, str, str, object], R],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[TargetResult[R]]:
        """Run ``credential_fn`` then ``service_fn`` for every target account.

        Each target is fully isolated: a credential or service failure produces
        a failed :class:`TargetResult` for that account alone, never an
        exception that aborts the fan-out.

        Args:
            targets: ``(org, account_id, account_name)`` tuples to run against.
            credential_fn: Called as ``(org, account_id, account_name)`` on the
                credential lane; returns whatever ``service_fn`` needs, normally
                a credentialed boto3 session.
            service_fn: Called as ``(org, account_id, account_name, creds)`` on
                the service lane; returns that account's result.
            on_progress: Optional ``(completed, total)`` callback. Throttled to
                at most one call per ``progress_min_interval_seconds``, plus a
                final call at 100%. Any exception it raises is swallowed -
                progress reporting must never break a query.

        Returns:
            One :class:`TargetResult` per target, in completion order.
        """
        if not targets:
            return []

        total = len(targets)
        futures = [
            self._dispatch_pool.submit(
                self._run_one_target, org, account_id, account_name, credential_fn, service_fn
            )
            for org, account_id, account_name in targets
        ]

        results: list[TargetResult[R]] = []
        last_report = 0.0
        for completed, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if on_progress is None:
                continue
            now = time.monotonic()
            is_final = completed == total
            if is_final or (now - last_report) >= self._progress_interval:
                last_report = now
                # Best-effort only: the client may have disconnected, and that
                # must not turn a successful query into a failed one.
                with contextlib.suppress(Exception):
                    on_progress(completed, total)
        return results

    def _run_one_target[R](
        self,
        org: str,
        account_id: str,
        account_name: str,
        credential_fn: Callable[[str, str, str], object],
        service_fn: Callable[[str, str, str, object], R],
    ) -> TargetResult[R]:
        """Run the credential-then-service chain for one account, isolating all failures.

        Runs on a dispatch thread, which only ever blocks waiting on the two
        worker lanes - it performs no AWS work itself.
        """
        deadline = _Deadline(self._per_target_timeout)

        try:
            cred_future, cred_started = self._submit_tracked(
                self._credential_pool, lambda: credential_fn(org, account_id, account_name)
            )
            creds = self._await_step(
                cred_future, cred_started, deadline=deadline, step="credential"
            )

            svc_future, svc_started = self._submit_tracked(
                self._service_pool, lambda: service_fn(org, account_id, account_name, creds)
            )
            value = self._await_step(
                svc_future, svc_started, deadline=deadline, step="service call"
            )
        except _StepError as exc:
            return TargetResult(
                org=org, account_id=account_id, account_name=account_name, ok=False, error=str(exc)
            )

        return TargetResult(
            org=org, account_id=account_id, account_name=account_name, ok=True, value=value
        )

    @staticmethod
    def _submit_tracked[V](
        pool: ThreadPoolExecutor, work: Callable[[], V]
    ) -> tuple[Future[V], threading.Event]:
        """Submit ``work`` to ``pool``, also returning an event set when it starts running.

        The event is what separates queue time from execution time: the
        dispatch thread waits on it without consuming any budget, and only
        starts the clock once a lane worker has actually picked the work up.
        """
        started = threading.Event()

        def _tracked() -> V:
            started.set()
            return work()

        return pool.submit(_tracked), started

    @staticmethod
    def _await_step[V](
        future: Future[V],
        started: threading.Event,
        *,
        deadline: _Deadline,
        step: str,
    ) -> V:
        """Wait for one submitted step, returning its value or raising :class:`_StepError`.

        The wait happens in two phases. First it waits, unbounded and free of
        charge, for the work to reach the front of its lane queue; then it
        waits for the result with whatever budget the target has left. Only
        the second phase is charged, so a target queued behind 600 others is
        judged on its own runtime rather than on how busy the lane was.

        The second wait is *bounded* by the budget rather than being unbounded
        and checked afterwards. Checking afterwards had two faults: a call
        slower than the budget still blocked a dispatch thread for its full
        duration, and a result that did arrive - just late - was thrown away
        and reported as a timeout, discarding work the user had already paid
        for.

        Args:
            future: The submitted work to wait for.
            started: Event set by the worker when it begins executing.
            deadline: This target's remaining execution budget.
            step: Human-readable step name, used only in the error message.

        Returns:
            The value produced by the submitted work.

        Raises:
            _StepError: The work raised, or outran the per-target budget.
        """
        # Cannot hang: the worker sets this as its first action, and nothing
        # cancels one of these futures before it is awaited here.
        started.wait()

        deadline.resume()
        try:
            return future.result(timeout=deadline.remaining())
        except FutureTimeoutError:
            future.cancel()
            raise _StepError(
                f"{step} exceeded the {deadline.timeout}s per-account budget"
            ) from None
        except Exception as exc:  # noqa: BLE001 - isolate per-target failures
            raise _StepError(str(exc)) from None
        finally:
            deadline.pause()

    def shutdown(self) -> None:
        """Shut down every pool, waiting for in-flight work to finish."""
        self._credential_pool.shutdown(wait=True)
        self._service_pool.shutdown(wait=True)
        self._dispatch_pool.shutdown(wait=True)
