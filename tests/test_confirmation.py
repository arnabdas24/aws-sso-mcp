"""Tests for aws_sso_mcp.confirmation: consent before destructive calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from aws_sso_mcp.confirmation import make_confirmer
from aws_sso_mcp.errors import ConfirmationError
from aws_sso_mcp.planner import ResolvedTarget

TARGETS = [ResolvedTarget(org="org1", account_id="111111111111", account_name="acct-a-dev")]
TERMINATE = [("ec2", "TerminateInstances", {"InstanceIds": ["i-123"]})]


def test_read_only_calls_need_no_confirmation() -> None:
    calls = [("ec2", "DescribeInstances", {}), ("s3", "ListBuckets", {})]
    assert make_confirmer(None, region="us-east-1", calls=calls) is None


def test_confirmed_call_proceeds_and_prompt_shows_what_will_happen(
    make_ctx: Callable[..., Any], in_worker_thread: Callable[..., Any]
) -> None:
    ctx = make_ctx()
    confirm = make_confirmer(ctx, region="us-east-1", calls=TERMINATE)
    assert confirm is not None

    in_worker_thread(confirm, TARGETS)

    prompt = ctx.messages[0]
    for expected in ("ec2:TerminateInstances", "us-east-1", "acct-a-dev (111111111111)", "i-123"):
        assert expected in prompt


def test_prompt_lists_only_the_destructive_calls(
    make_ctx: Callable[..., Any], in_worker_thread: Callable[..., Any]
) -> None:
    ctx = make_ctx()
    calls = [("ec2", "DescribeVpcs", {}), ("s3", "DeleteBucket", {"Bucket": "b"})]
    confirm = make_confirmer(ctx, region="us-east-1", calls=calls)
    assert confirm is not None

    in_worker_thread(confirm, TARGETS)

    assert "s3:DeleteBucket" in ctx.messages[0]
    assert "DescribeVpcs" not in ctx.messages[0]


@pytest.mark.parametrize(
    ("action", "ticked"),
    [("decline", False), ("cancel", False), ("accept", False)],
    ids=["declined", "cancelled", "accepted-but-unticked"],
)
def test_anything_but_an_explicit_yes_stops_the_call(
    make_ctx: Callable[..., Any],
    in_worker_thread: Callable[..., Any],
    action: str,
    ticked: bool,
) -> None:
    confirm = make_confirmer(
        make_ctx(action=action, confirm=ticked), region="us-east-1", calls=TERMINATE
    )
    assert confirm is not None

    with pytest.raises(ConfirmationError, match="was not confirmed, so nothing was run"):
        in_worker_thread(confirm, TARGETS)


def test_client_that_cannot_ask_is_refused_without_prompting(
    make_ctx: Callable[..., Any], in_worker_thread: Callable[..., Any]
) -> None:
    ctx = make_ctx(can_elicit=False)
    confirm = make_confirmer(ctx, region="us-east-1", calls=TERMINATE)
    assert confirm is not None

    with pytest.raises(ConfirmationError, match="cannot ask"):
        in_worker_thread(confirm, TARGETS)
    assert ctx.messages == []


def test_missing_context_is_refused() -> None:
    confirm = make_confirmer(None, region="us-east-1", calls=TERMINATE)
    assert confirm is not None

    with pytest.raises(ConfirmationError, match="cannot ask"):
        confirm(TARGETS)


def test_failure_to_ask_fails_closed(
    make_ctx: Callable[..., Any], in_worker_thread: Callable[..., Any]
) -> None:
    boom = RuntimeError("transport closed")
    confirm = make_confirmer(make_ctx(error=boom), region="us-east-1", calls=TERMINATE)
    assert confirm is not None

    with pytest.raises(ConfirmationError, match="Could not ask you") as caught:
        in_worker_thread(confirm, TARGETS)
    assert caught.value.__cause__ is boom


def test_asking_off_a_worker_thread_fails_closed(make_ctx: Callable[..., Any]) -> None:
    confirm = make_confirmer(make_ctx(), region="us-east-1", calls=TERMINATE)
    assert confirm is not None

    with pytest.raises(ConfirmationError, match="Could not ask you"):
        confirm(TARGETS)


def test_prompt_redacts_secrets_in_parameters(
    make_ctx: Callable[..., Any], in_worker_thread: Callable[..., Any]
) -> None:
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    ctx = make_ctx()
    calls = [("iam", "DeleteAccessKey", {"AccessKeyId": key})]
    confirm = make_confirmer(ctx, region="us-east-1", calls=calls)
    assert confirm is not None

    in_worker_thread(confirm, TARGETS)

    assert key not in ctx.messages[0]


def test_prompt_is_bounded_for_many_accounts_and_long_parameters(
    make_ctx: Callable[..., Any], in_worker_thread: Callable[..., Any]
) -> None:
    ctx = make_ctx()
    targets = [
        ResolvedTarget(org="org1", account_id=f"{i:012d}", account_name=f"acct-{i}")
        for i in range(20)
    ]
    calls = [("s3", "DeleteObject", {"Key": "k" * 1000})]
    confirm = make_confirmer(ctx, region="us-east-1", calls=calls)
    assert confirm is not None

    in_worker_thread(confirm, targets)

    prompt = ctx.messages[0]
    assert "Accounts (20)" in prompt
    assert "and 5 more" in prompt
    assert "acct-19" not in prompt
    assert "...[truncated]" in prompt
    assert "k" * 1000 not in prompt
