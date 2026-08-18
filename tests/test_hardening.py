"""Tests for aws_sso_mcp.hardening - output-size caps on untrusted AWS data."""

from __future__ import annotations

from aws_sso_mcp.hardening import cap_value


def test_short_string_untouched() -> None:
    assert cap_value("hello") == "hello"


def test_long_string_truncated() -> None:
    value = "x" * 5000
    out = cap_value(value, max_string=100)
    assert isinstance(out, str)
    assert len(out) == 100
    assert out.endswith("...[truncated]")


def test_long_list_truncated_with_marker() -> None:
    value = list(range(10))
    out = cap_value(value, max_list=5)
    assert out[:5] == [0, 1, 2, 3, 4]
    assert "more items truncated" in out[5]


def test_nested_dict_and_list_are_capped() -> None:
    value = {"tags": [{"note": "y" * 200}]}
    out = cap_value(value, max_string=50)
    assert len(out["tags"][0]["note"]) == 50


def test_non_string_scalars_pass_through() -> None:
    assert cap_value(42) == 42
    assert cap_value(None) is None
    assert cap_value(True) is True


def test_tuple_is_capped_recursively() -> None:
    out = cap_value(("a", "b" * 5000), max_string=10)
    assert out[0] == "a"
    assert len(out[1]) == 10
