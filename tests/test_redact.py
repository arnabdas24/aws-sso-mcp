"""Tests for aws_sso_mcp.redact - defense-in-depth output scrubbing."""

from __future__ import annotations

from aws_sso_mcp.redact import redact


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
