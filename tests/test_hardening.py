"""Tests for aws_sso_mcp.hardening - output-size caps on untrusted AWS data.

``process_output`` does two jobs in one pass: it caps sizes and (optionally)
redacts secrets. These tests pin the capping half by passing
``do_redact=False``; redaction itself is covered by ``test_redact.py``.
"""

from __future__ import annotations

from typing import Any

from aws_sso_mcp.hardening import (
    _TRUNCATION_SUFFIX,
    MAX_LIST_ITEMS,
    MAX_STRING_LENGTH,
    process_output,
)


def cap(value: object, **kwargs: Any) -> Any:
    """Cap ``value`` without redacting, so tests see size behaviour in isolation."""
    return process_output(value, do_redact=False, **kwargs)


def test_short_string_untouched() -> None:
    assert cap("hello") == "hello"


def test_long_string_truncated() -> None:
    out = cap("x" * 5000, max_string=100)
    assert isinstance(out, str)
    # The marker counts against the limit, so the result never exceeds max_string.
    assert len(out) == 100
    assert out.endswith("...[truncated]")


def test_long_list_truncated_with_marker() -> None:
    out = cap(list(range(10)), max_list=5)
    assert out[:5] == [0, 1, 2, 3, 4]
    assert "more items truncated" in out[5]


def test_nested_dict_and_list_are_capped() -> None:
    out = cap({"tags": [{"note": "y" * 200}]}, max_string=50)
    assert len(out["tags"][0]["note"]) == 50


def test_non_string_scalars_pass_through() -> None:
    assert cap(42) == 42
    assert cap(None) is None
    assert cap(True) is True


def test_tuple_is_capped_recursively() -> None:
    out = cap(("a", "b" * 5000), max_string=10)
    assert out[0] == "a"
    assert len(out[1]) == 10


def test_dict_keys_are_never_redacted() -> None:
    """Keys come from botocore's service model, so they are structure, not secrets.

    Redacting them would rename fields and make responses unparseable.
    """
    out = process_output({"SecretAccessKey": "AKIAABCDEFGHIJKLMNOP"}, do_redact=True)
    assert "SecretAccessKey" in out
    assert "AKIAABCDEFGHIJKLMNOP" not in out["SecretAccessKey"]


def test_defaults_are_applied_when_not_overridden() -> None:
    out = cap(["z" * (MAX_STRING_LENGTH + 10)] * (MAX_LIST_ITEMS + 10))
    assert len(out) == MAX_LIST_ITEMS + 1  # kept items plus the truncation marker
    assert len(out[0]) == MAX_STRING_LENGTH


# --------------------------------------------- redaction happens before capping

SECRET = "AKIAABCDEFGHIJKLMNOP"


def test_a_secret_straddling_the_truncation_boundary_is_still_redacted() -> None:
    """Capping before redacting can slice a secret in half and let it through.

    With the cap applied first, the leading half of the key stays inside the
    kept prefix while its tail is cut off; the pattern no longer matches, so
    redaction finds nothing and the fragment reaches the model. Redacting the
    full string first makes the position of the secret irrelevant.
    """
    max_string = 60
    # _cap_string keeps this many characters before appending its marker, so
    # placing the key to span that index puts it exactly on the cut. The
    # filler is non-alphanumeric because the bare-key pattern is \b-anchored.
    keep = max_string - len(_TRUNCATION_SUFFIX)
    prefix = "." * (keep - len(SECRET) // 2)
    raw = prefix + SECRET + "." * max_string
    assert len(raw) > max_string  # the cap must actually fire

    out = process_output(raw, max_string=max_string, do_redact=True)

    assert isinstance(out, str)
    assert len(out) <= max_string
    # The surviving half of the key is what the old cap-then-redact order leaked.
    assert SECRET[: len(SECRET) // 2] not in out


def test_a_secret_pushed_entirely_past_the_cap_is_also_safe() -> None:
    out = process_output("." * 60 + SECRET, max_string=40, do_redact=True)
    assert isinstance(out, str)
    assert SECRET not in out
    assert len(out) <= 40


def test_redaction_can_shorten_a_string_below_the_cap() -> None:
    """Replacing a long secret with a short marker may remove the need to truncate."""
    out = process_output(f"key={SECRET}", max_string=30, do_redact=True)
    assert isinstance(out, str)
    assert SECRET not in out
    assert not out.endswith("...[truncated]")


def test_capping_still_applies_to_redacted_output() -> None:
    """Redaction first must not become an excuse to skip the size bound."""
    out = process_output("w" * 5000, max_string=100, do_redact=True)
    assert isinstance(out, str)
    assert len(out) == 100
