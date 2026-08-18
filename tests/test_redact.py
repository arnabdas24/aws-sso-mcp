"""Tests for aws_sso_mcp.redact - defense-in-depth output scrubbing."""

from __future__ import annotations

from aws_sso_mcp.redact import redact, redact_value


def test_redacts_known_key_value_pairs() -> None:
    text = '{"accessKeyId": "AKIAABCDEFGHIJKLMNOP", "secretAccessKey": "abc123"}'
    out = redact(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "abc123" not in out
    assert "REDACTED" in out


def test_redacts_bare_access_key() -> None:
    text = "profile uses AKIAABCDEFGHIJKLMNOP as its key"
    out = redact(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in out


def test_leaves_normal_text_untouched() -> None:
    text = "account xyz has 3 stopped instances in eu-west-1"
    assert redact(text) == text


def test_redact_value_walks_nested_structures() -> None:
    value = {
        "instances": [
            {"id": "i-123", "note": "profile uses AKIAABCDEFGHIJKLMNOP as its key"},
        ],
        "region": "us-east-1",
    }
    out = redact_value(value)
    assert isinstance(out, dict)
    assert "AKIAABCDEFGHIJKLMNOP" not in str(out)
    assert out["region"] == "us-east-1"
    assert out["instances"][0]["id"] == "i-123"


def test_redact_value_passes_through_non_strings() -> None:
    assert redact_value(42) == 42
    assert redact_value(None) is None
    assert redact_value(True) is True


def test_redact_value_handles_tuples() -> None:
    out = redact_value(("a", "AKIAABCDEFGHIJKLMNOP"))
    assert out[0] == "a"
    assert "AKIAABCDEFGHIJKLMNOP" not in out[1]
