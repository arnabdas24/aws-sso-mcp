"""Standalone redaction for arbitrary tool-output strings.

``aws_sso_orchestrator.logging.RedactingFilter`` only operates on
``logging.LogRecord``. Every string this server returns to an MCP client goes
through :func:`redact` instead, as defense-in-depth on top of the tools never
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

# Bare-value fallback: AWS access key ids and long opaque bearer-style tokens,
# in case they appear without an adjacent recognizable key name.
_BARE_VALUE_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\b[A-Za-z0-9/+=_-]{40,}\b")


def redact(text: str) -> str:
    """Return ``text`` with known-sensitive key/value pairs and bare secrets masked."""
    text = _KEY_VALUE_PATTERN.sub(lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", text)
    return _BARE_VALUE_PATTERN.sub(_REDACTED, text)


def redact_value(value: object) -> object:
    """Recursively apply :func:`redact` to every string inside a dict/list/tuple structure.

    Defense-in-depth over raw AWS API responses before they ever reach an
    MCP client - tool code should never rely on this alone, but every tool
    output is passed through it regardless.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(str(k)): redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value
