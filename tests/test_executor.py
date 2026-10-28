"""Tests for the two-lane concurrent executor (engine/executor.py)."""

from __future__ import annotations

import threading
import time

from aws_sso_mcp.engine.executor import QueryExecutor, TargetResult


def test_fan_out_success_all_targets() -> None:
    executor = QueryExecutor(credential_workers=4, service_workers=8)
    targets = [("org1", f"{i:012d}", f"account-{i}") for i in range(10)]

    def credential_fn(org: str, account_id: str, account_name: str) -> str:
        return f"creds-{account_id}"

    def service_fn(org: str, account_id: str, account_name: str, creds: object) -> dict:
        return {"instances": [], "creds": creds}

    results = executor.fan_out(targets, credential_fn=credential_fn, service_fn=service_fn)
    executor.shutdown()

    assert len(results) == 10
    assert all(r.ok for r in results)
    assert all(r.value == {"instances": [], "creds": f"creds-{r.account_id}"} for r in results)


def test_fan_out_isolates_credential_failures() -> None:
    executor = QueryExecutor(credential_workers=4, service_workers=8)
    targets = [("org1", "111111111111", "good"), ("org1", "222222222222", "bad")]

    def credential_fn(org: str, account_id: str, account_name: str) -> str:
        if account_name == "bad":
            raise RuntimeError("access denied")
        return "creds"

    def service_fn(org: str, account_id: str, account_name: str, creds: object) -> str:
        return "ok"

    results = executor.fan_out(targets, credential_fn=credential_fn, service_fn=service_fn)
    executor.shutdown()

    by_name = {r.account_name: r for r in results}
    assert by_name["good"].ok is True
    assert by_name["good"].value == "ok"
    assert by_name["bad"].ok is False
    assert by_name["bad"].error is not None
    assert "access denied" in by_name["bad"].error


def test_fan_out_isolates_service_call_failures() -> None:
    executor = QueryExecutor(credential_workers=4, service_workers=8)
    targets = [("org1", "111111111111", "a"), ("org1", "222222222222", "b")]

    def credential_fn(org: str, account_id: str, account_name: str) -> str:
        return "creds"

    def service_fn(org: str, account_id: str, account_name: str, creds: object) -> str:
        if account_name == "b":
            raise RuntimeError("region disabled")
        return "ok"

    results = executor.fan_out(targets, credential_fn=credential_fn, service_fn=service_fn)
    executor.shutdown()

    by_name = {r.account_name: r for r in results}
    assert by_name["a"].ok is True
    assert by_name["b"].ok is False
    assert "region disabled" in (by_name["b"].error or "")


def test_fan_out_empty_targets() -> None:
    executor = QueryExecutor()
    results = executor.fan_out([], credential_fn=lambda *_: None, service_fn=lambda *_: None)
    executor.shutdown()
    assert results == []


def test_fan_out_runs_concurrently_not_serially() -> None:
    """A large fan-out with a small per-call sleep must complete in roughly one sleep's worth
    of wall time, not N sleeps - proving the dispatch/service lanes are actually parallel."""
    executor = QueryExecutor(credential_workers=20, service_workers=20)
    targets = [("org1", f"{i:012d}", f"account-{i}") for i in range(20)]

    def credential_fn(org: str, account_id: str, account_name: str) -> str:
        return "creds"

    def service_fn(org: str, account_id: str, account_name: str, creds: object) -> str:
        time.sleep(0.1)
        return "ok"

    start = time.monotonic()
    results = executor.fan_out(targets, credential_fn=credential_fn, service_fn=service_fn)
    elapsed = time.monotonic() - start
    executor.shutdown()

    assert all(r.ok for r in results)
    # 20 targets * 0.1s serially would be ~2s; concurrently it should be well under 1s.
    assert elapsed < 1.0


def test_credential_lane_and_service_lane_use_different_pools() -> None:
    executor = QueryExecutor(credential_workers=2, service_workers=2)
    seen_credential_threads: set[str] = set()
    seen_service_threads: set[str] = set()
    lock = threading.Lock()

    def credential_fn(org: str, account_id: str, account_name: str) -> str:
        with lock:
            seen_credential_threads.add(threading.current_thread().name)
        return "creds"

    def service_fn(org: str, account_id: str, account_name: str, creds: object) -> str:
        with lock:
            seen_service_threads.add(threading.current_thread().name)
        return "ok"

    targets = [("org1", f"{i:012d}", f"a{i}") for i in range(5)]
    executor.fan_out(targets, credential_fn=credential_fn, service_fn=service_fn)
    executor.shutdown()

    assert all("cred" in name for name in seen_credential_threads)
    assert all("svc" in name for name in seen_service_threads)
    assert seen_credential_threads.isdisjoint(seen_service_threads)


def test_target_result_is_a_plain_dataclass() -> None:
    result = TargetResult(
        org="org1", account_id="111111111111", account_name="a", ok=True, value=42
    )
    assert result.value == 42
    assert result.error is None


def test_fan_out_reports_progress_for_every_target() -> None:
    """Progress is throttled but must always fire at least a final 100% call."""
    executor = QueryExecutor(
        credential_workers=4, service_workers=8, progress_min_interval_seconds=0.0
    )
    targets = [("org1", f"{i:012d}", f"a{i}") for i in range(6)]
    lock = threading.Lock()
    calls: list[tuple[int, int]] = []

    def on_progress(done: int, total: int) -> None:
        with lock:
            calls.append((done, total))

    results = executor.fan_out(
        targets,
        credential_fn=lambda *_: "creds",
        service_fn=lambda *_: "ok",
        on_progress=on_progress,
    )
    executor.shutdown()

    assert len(results) == 6
    # With interval=0 every completion emits a progress call.
    assert len(calls) == 6
    assert {total for _, total in calls} == {6}
    assert sorted(done for done, _ in calls) == list(range(1, 7))


def test_fan_out_throttles_progress_by_default() -> None:
    """The default 250ms throttle should still deliver a final 100% call."""
    executor = QueryExecutor(credential_workers=4, service_workers=8)
    targets = [("org1", f"{i:012d}", f"a{i}") for i in range(6)]
    calls: list[tuple[int, int]] = []
    lock = threading.Lock()

    def on_progress(done: int, total: int) -> None:
        with lock:
            calls.append((done, total))

    executor.fan_out(
        targets,
        credential_fn=lambda *_: "creds",
        service_fn=lambda *_: "ok",
        on_progress=on_progress,
    )
    executor.shutdown()
    assert calls, "expected at least one progress call"
    # The last call is always the 100% completion notification.
    assert calls[-1] == (6, 6)


def test_fan_out_swallows_on_progress_exceptions() -> None:
    """A broken progress callback must never break the underlying fan-out."""
    executor = QueryExecutor(credential_workers=2, service_workers=2)
    targets = [("org1", "111111111111", "a"), ("org1", "222222222222", "b")]

    def on_progress(done: int, total: int) -> None:
        raise RuntimeError("client disconnected")

    results = executor.fan_out(
        targets,
        credential_fn=lambda *_: "creds",
        service_fn=lambda *_: "ok",
        on_progress=on_progress,
    )
    executor.shutdown()

    assert len(results) == 2
    assert all(r.ok for r in results)


# ------------------------------------------------------------ timeout budget


def test_per_target_timeout_bounds_a_slow_call() -> None:
    """A call that overruns the budget is abandoned, not waited out.

    Regression test: an earlier version called ``future.result()`` with no
    timeout at all and only consulted the deadline afterwards, so a slow
    account blocked for its full duration no matter how small the budget.
    """
    executor = QueryExecutor(
        credential_workers=2, service_workers=2, per_target_timeout_seconds=0.2
    )
    # Lets the abandoned worker exit promptly once the assertions are done,
    # so shutdown() does not have to sit through the full simulated delay.
    release = threading.Event()

    def slow_service(org: str, account_id: str, account_name: str, creds: object) -> str:
        release.wait(timeout=5.0)
        return "never seen"

    start = time.monotonic()
    results = executor.fan_out(
        [("org1", "111111111111", "slow")],
        credential_fn=lambda *_: "creds",
        service_fn=slow_service,
    )
    elapsed = time.monotonic() - start
    release.set()
    executor.shutdown()

    assert results[0].ok is False
    assert "budget" in (results[0].error or "").lower()
    # The whole point: we returned long before the 5s call would have finished.
    assert elapsed < 2.0


def test_result_arriving_just_inside_the_budget_is_kept() -> None:
    """A call that finishes before the budget expires keeps its result.

    Regression test for the inverse failure of the one above: the old code
    waited for the future with no timeout and *then* asked whether the
    deadline had passed, so a call that genuinely succeeded could have its
    result thrown away - the worst of both worlds, a full wait and no answer.
    """
    executor = QueryExecutor(
        credential_workers=2, service_workers=2, per_target_timeout_seconds=1.0
    )

    def service(org: str, account_id: str, account_name: str, creds: object) -> str:
        time.sleep(0.2)
        return "real answer"

    results = executor.fan_out(
        [("org1", "111111111111", "a")],
        credential_fn=lambda *_: "creds",
        service_fn=service,
    )
    executor.shutdown()

    assert results[0].ok is True
    assert results[0].value == "real answer"


def test_per_target_timeout_excludes_queue_wait() -> None:
    """The budget starts when a target runs, not when it is submitted.

    With one service worker and a budget shorter than the total serial
    runtime, every target must still succeed: each one gets its own fresh
    budget rather than racing a clock that started at submission.
    """
    executor = QueryExecutor(
        credential_workers=1, service_workers=1, per_target_timeout_seconds=0.5
    )
    targets = [("org1", f"{i:012d}", f"a{i}") for i in range(5)]

    def service(org: str, account_id: str, account_name: str, creds: object) -> str:
        time.sleep(0.15)
        return "ok"

    results = executor.fan_out(targets, credential_fn=lambda *_: "creds", service_fn=service)
    executor.shutdown()

    assert len(results) == 5
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]


def test_timeout_disabled_lets_a_long_call_finish() -> None:
    """``per_target_timeout_seconds=None`` means no bound at all."""
    executor = QueryExecutor(
        credential_workers=1, service_workers=1, per_target_timeout_seconds=None
    )

    def service(org: str, account_id: str, account_name: str, creds: object) -> str:
        time.sleep(0.3)
        return "finished"

    results = executor.fan_out(
        [("org1", "111111111111", "a")],
        credential_fn=lambda *_: "creds",
        service_fn=service,
    )
    executor.shutdown()

    assert results[0].ok is True
    assert results[0].value == "finished"
