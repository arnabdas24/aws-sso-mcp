"""Tests for aws_sso_mcp.audit - the append-only JSONL audit log."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from aws_sso_mcp.audit import append, flush_all, new_entry


def test_new_entry_redacts_error_field() -> None:
    entry = new_entry(
        org="org1",
        account_id="111111111111",
        account_name="acct-a",
        role="ReadOnly",
        role_source="environment",
        region="us-east-1",
        service="ec2",
        operation="DescribeInstances",
        ok=False,
        error="profile uses AKIAABCDEFGHIJKLMNOP as its key",
    )
    assert entry.error is not None
    assert "AKIAABCDEFGHIJKLMNOP" not in entry.error


def test_append_writes_one_json_line(tmp_path: Path, flush_audit: Callable[[Path], None]) -> None:
    log_path = tmp_path / "audit.jsonl"
    entry = new_entry(
        org="org1",
        account_id="111111111111",
        account_name="acct-a",
        role="ReadOnly",
        role_source="environment",
        region="us-east-1",
        service="ec2",
        operation="DescribeInstances",
        ok=True,
    )
    append(log_path, entry)
    append(log_path, entry)
    flush_audit(log_path)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["service"] == "ec2"
    assert parsed["ok"] is True
    assert parsed["error"] is None


def test_append_never_raises_on_bad_path(tmp_path: Path) -> None:
    bad_path = tmp_path / "does-not-exist" / "nested" / "audit.jsonl"
    entry = new_entry(
        org="org1",
        account_id="111111111111",
        account_name="acct-a",
        role=None,
        role_source=None,
        region="us-east-1",
        service="ec2",
        operation="DescribeInstances",
        ok=True,
    )
    # Parent dir doesn't exist -> OSError internally, must not raise.
    append(bad_path, entry)


def _entry(operation: str):
    return new_entry(
        org="org1",
        account_id="111111111111",
        account_name="acct-a",
        role="ReadOnly",
        role_source="environment",
        region="us-east-1",
        service="ec2",
        operation=operation,
        ok=True,
    )


def test_flush_all_drains_entries_that_are_still_queued(tmp_path: Path) -> None:
    """Shutdown must not lose queued entries.

    The writer is a daemon thread, so before ``flush_all`` existed the
    interpreter simply exited without waiting for it and anything still in
    the queue was dropped. The entries most likely to be in flight are the
    ones from the call that just ran, which is precisely what an audit log
    exists to record.
    """
    log_path = tmp_path / "audit.jsonl"
    for i in range(50):
        append(log_path, _entry(f"Describe{i}"))

    flush_all()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    assert {json.loads(line)["operation"] for line in lines} == {f"Describe{i}" for i in range(50)}


def test_flush_all_is_safe_to_call_twice(tmp_path: Path) -> None:
    """The atexit hook may run after an explicit flush; neither may raise."""
    log_path = tmp_path / "audit.jsonl"
    append(log_path, _entry("DescribeInstances"))
    flush_all()
    flush_all()
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1


def test_appending_after_a_flush_starts_a_fresh_writer(tmp_path: Path) -> None:
    """A flushed path must still be usable - closing is not a permanent teardown."""
    log_path = tmp_path / "audit.jsonl"
    append(log_path, _entry("First"))
    flush_all()
    append(log_path, _entry("Second"))
    flush_all()

    operations = [
        json.loads(line)["operation"] for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert operations == ["First", "Second"]


def test_flush_all_with_no_writers_is_a_no_op() -> None:
    flush_all()
