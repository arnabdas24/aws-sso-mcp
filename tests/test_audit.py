"""Tests for aws_sso_mcp.audit - the append-only JSONL audit log."""

from __future__ import annotations

import json
from pathlib import Path

from aws_sso_mcp.audit import append, new_entry


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


def test_append_writes_one_json_line(tmp_path: Path) -> None:
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
