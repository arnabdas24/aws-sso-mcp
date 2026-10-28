"""Property-based tests for the secret-handling invariants.

Example tests pin the cases someone thought of; these pin the *invariant* across
generated inputs: no secret survives redaction at any position, any cap size, or
any nesting depth. Both bugs found in review (a secret split by the truncation
cut, a secret in logging args) were exactly the "position you didn't think of"
kind.
"""

from __future__ import annotations

import json
import string

from hypothesis import given, settings
from hypothesis import strategies as st

from aws_sso_mcp.hardening import process_output
from aws_sso_mcp.redact import redact

_KEY_BODY = st.text(alphabet=string.ascii_uppercase + string.digits, min_size=16, max_size=16)
_ACCESS_KEYS = st.builds(
    lambda prefix, body: prefix + body, st.sampled_from(["AKIA", "ASIA"]), _KEY_BODY
)
# Non-word filler: the bare-key patterns are \b-anchored, so a key glued to
# letters is intentionally not a match and would exercise nothing.
_FILLER = st.text(alphabet=" .,;:/-_()[]", max_size=120).map(lambda s: s.replace("_", "."))

_SETTINGS = settings(max_examples=200, deadline=None)


@_SETTINGS
@given(key=_ACCESS_KEYS, before=_FILLER, after=_FILLER)
def test_an_access_key_is_masked_wherever_it_appears(key: str, before: str, after: str) -> None:
    assert key not in redact(f"{before}{key}{after}")


@_SETTINGS
@given(text=st.text(max_size=300))
def test_redaction_is_idempotent(text: str) -> None:
    once = redact(text)
    assert redact(once) == once


@_SETTINGS
@given(text=st.text(alphabet=string.ascii_lowercase + " .,\n", max_size=300))
def test_text_with_no_secret_shape_is_returned_unchanged(text: str) -> None:
    assert redact(text) == text


@_SETTINGS
@given(
    key=_ACCESS_KEYS,
    before=_FILLER,
    after=_FILLER,
    max_string=st.integers(min_value=20, max_value=200),
)
def test_a_secret_never_survives_the_truncation_cut(
    key: str, before: str, after: str, max_string: int
) -> None:
    out = process_output(f"{before}{key}{after}", max_string=max_string, do_redact=True)

    assert isinstance(out, str)
    assert len(out) <= max_string
    # Any 8-char fragment of the key means the cut split an unredacted secret.
    assert not any(key[i : i + 8] in out for i in range(len(key) - 7))


@_SETTINGS
@given(
    key=_ACCESS_KEYS,
    max_string=st.integers(min_value=20, max_value=80),
    depth=st.integers(min_value=0, max_value=4),
)
def test_a_secret_is_masked_at_any_nesting_depth(key: str, max_string: int, depth: int) -> None:
    payload: object = f"token: {key} ."
    for level in range(depth):
        payload = {"items": [payload, f"sibling {level}"]}

    out = process_output(payload, max_string=max_string, do_redact=True)

    assert key not in json.dumps(out)
