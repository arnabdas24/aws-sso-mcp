"""Output-size hardening: caps on untrusted AWS-returned string/list sizes.

AWS-returned data (tag values, instance names, log messages, ...) is
untrusted input that flows straight into model context. This is not a
prompt-injection *filter* (there is no reliable way to strip adversarial
instructions from arbitrary text) - it only bounds *how much* of any single
field can reach the model, so one enormous or repeated field can't blow out
the context window or amplify a low-effort attack.
"""

from __future__ import annotations

_MAX_STRING_LENGTH = 4000
_MAX_LIST_ITEMS = 500
_TRUNCATION_SUFFIX = "...[truncated]"


def cap_value(
    value: object, *, max_string: int = _MAX_STRING_LENGTH, max_list: int = _MAX_LIST_ITEMS
) -> object:
    """Recursively cap string length and list length in an arbitrary AWS response structure."""
    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        keep = max(0, max_string - len(_TRUNCATION_SUFFIX))
        return (value[:keep] + _TRUNCATION_SUFFIX)[:max_string]
    if isinstance(value, dict):
        return {k: cap_value(v, max_string=max_string, max_list=max_list) for k, v in value.items()}
    if isinstance(value, list):
        capped = [cap_value(v, max_string=max_string, max_list=max_list) for v in value[:max_list]]
        if len(value) > max_list:
            capped.append(f"...[{len(value) - max_list} more items truncated]")
        return capped
    if isinstance(value, tuple):
        return tuple(cap_value(v, max_string=max_string, max_list=max_list) for v in value)
    return value
