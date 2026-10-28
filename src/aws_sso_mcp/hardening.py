"""Bound the size of tool output, and redact it, in a single tree walk.

AWS-returned data - tag values, instance names, log messages - is untrusted
input that flows straight into model context. This module is **not** a
prompt-injection filter; there is no reliable way to strip adversarial
instructions from arbitrary text. It bounds only *how much* of any single field
can reach the model, so one enormous or endlessly repeated field cannot exhaust
the context window or amplify a low-effort attack.

:func:`process_output` combines the size cap with the redaction from
:mod:`.redact` into one traversal, rather than walking the whole structure
twice. On a fan-out returning hundreds of accounts' responses, that halves the
post-processing cost. Within that traversal, redaction runs before the cap so
that truncating a string can never split a secret and leave the surviving
fragment unmatched.
"""

from __future__ import annotations

from .redact import redact

# Longest a single string field may be before it is truncated. Generous enough
# for real values such as policy documents, small enough that one runaway field
# cannot dominate a response.
MAX_STRING_LENGTH = 4000

# Longest a single list may be before the tail is dropped.
MAX_LIST_ITEMS = 500

_TRUNCATION_SUFFIX = "...[truncated]"


def _cap_string(value: str, max_string: int) -> str:
    """Truncate ``value`` to ``max_string`` characters, marking that it was cut.

    The marker is included *within* the limit, so the returned string is never
    longer than ``max_string`` even when the suffix is added.
    """
    if len(value) <= max_string:
        return value
    keep = max(0, max_string - len(_TRUNCATION_SUFFIX))
    return (value[:keep] + _TRUNCATION_SUFFIX)[:max_string]


def process_output(
    value: object,
    *,
    max_string: int = MAX_STRING_LENGTH,
    max_list: int = MAX_LIST_ITEMS,
    do_redact: bool = True,
) -> object:
    """Cap sizes and redact secrets across a nested structure in one pass.

    Args:
        value: Any JSON-like structure of dicts, lists, tuples, and scalars -
            normally a raw AWS API response.
        max_string: Maximum length of any single string.
        max_list: Maximum number of items kept from any single list. When a
            list is trimmed, a human-readable marker is appended naming how
            many items were dropped, so the model is never silently misled
            about the size of a result.
        do_redact: Whether to apply :func:`~.redact.redact` to each string.
            Only ever disabled in tests that assert on capping alone.

    Returns:
        A new structure of the same shape, with every string redacted and
        then capped.

    Note:
        Redaction runs **before** the size cap, not after. Capping first can
        slice a secret in half at the boundary - the leading fragment stays
        inside the kept prefix, the pattern no longer matches, and the
        fragment reaches the model unredacted. Redacting the full string
        first means a secret is replaced wherever it sits, and only then is
        whatever remains trimmed to length.

    Note:
        Dictionary **keys** are intentionally left alone. Keys in an AWS
        response come from botocore's service model - they are fixed field
        names like ``"InstanceId"``, never customer data and never secrets.
        Redacting them cost a full regex pass per key per account while being
        incapable of finding anything, which is pure overhead on the hottest
        path in the server.
    """

    def _walk(current: object) -> object:
        if isinstance(current, str):
            return _cap_string(redact(current) if do_redact else current, max_string)
        if isinstance(current, dict):
            return {key: _walk(item) for key, item in current.items()}
        if isinstance(current, list):
            walked = [_walk(item) for item in current[:max_list]]
            if len(current) > max_list:
                walked.append(f"...[{len(current) - max_list} more items truncated]")
            return walked
        if isinstance(current, tuple):
            return tuple(_walk(item) for item in current)
        return current

    return _walk(value)
