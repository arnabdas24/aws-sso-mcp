"""Standalone redaction for arbitrary tool-output strings.

:class:`aws_sso_mcp.logging_setup.RedactingFormatter` applies this to every
rendered log line. Every string this server returns to an MCP client goes
through :func:`redact` directly, as defense-in-depth on top of the tools never
constructing or returning a credential/token in the first place.
"""

from __future__ import annotations

import re

_REDACTED = "***REDACTED***"

_SENSITIVE_KEYS = (
    "accessToken",
    "accessKeyId",
    "secretAccessKey",
    "sessionToken",
    "refreshToken",
    "clientSecret",
)

_KEY_VALUE_PATTERN = re.compile(
    r"(?P<key>" + "|".join(re.escape(k) for k in _SENSITIVE_KEYS) + r")"
    r"(?P<sep>['\"]?\s*[:=]\s*['\"]?)"
    r"(?P<value>[^'\",\s}]+)",
    re.IGNORECASE,
)

# Anchored bare-value patterns. Deliberately narrow: prior implementations
# used a length-40 base64 catch-all that false-positived on ARNs, S3 keys,
# long tag values, AMI IDs, etc. The patterns here match only known secret
# *shapes*, so AWS API responses flow through untouched.
_BARE_PATTERNS = (
    # IAM long-term access key id (AKIA…) and STS session key id (ASIA…).
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # STS session token (opaque bearer, starts with "FQoG"/"IQoJ"/"FwoG"…
    # base64url-ish, >=100 chars).
    re.compile(r"\b(?:FQoG|IQoJ|FwoG|FwoJ)[A-Za-z0-9/+=_-]{100,}\b"),
    # SSO-OIDC access tokens (also base64url, prefixed "eyJ" for JWTs).
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
)

# Every literal substring that could possibly start a match in any pattern
# above. Redaction runs over every string in every response from every account
# in a fan-out, so the common case - a string containing no secret at all -
# must be as cheap as possible. One combined scan here replaces four full
# regex passes; only strings that trip this filter pay for the real patterns.
#
# This is purely a performance gate and can never cause a missed redaction: any
# string the detailed patterns would match necessarily contains one of these
# literals, so a non-match here provably cannot match below.
_PREFILTER = re.compile(
    "|".join(
        [
            *(re.escape(k) for k in _SENSITIVE_KEYS),
            "AKIA",
            "ASIA",
            "FQoG",
            "IQoJ",
            "FwoG",
            "FwoJ",
            "eyJ",
        ]
    ),
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Return ``text`` with known-sensitive key/value pairs and bare secrets masked.

    Args:
        text: Any string headed for a log line or an MCP client.

    Returns:
        ``text`` with secret material replaced by a fixed marker. Strings that
        contain nothing secret are returned unchanged, after a single cheap
        scan.
    """
    if not _PREFILTER.search(text):
        return text
    text = _KEY_VALUE_PATTERN.sub(lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", text)
    for pat in _BARE_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


def redact_value(value: object) -> object:
    """Recursively apply :func:`redact` to every string inside a nested structure.

    Defense-in-depth over raw AWS API responses before they reach an MCP client.
    Tool code should never rely on this alone - it should not place secrets in
    its output in the first place - but every tool output passes through it
    regardless.

    Args:
        value: Any JSON-like structure of dicts, lists, tuples, and scalars.

    Returns:
        The same structure with every string redacted. Non-string scalars are
        returned as-is.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        # Only values are redacted, not keys. See the note in
        # :func:`aws_sso_mcp.hardening.process_output` for why keys are safe.
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value
