"""Tests for log redaction and the MCP-context log bridge (logging_setup.py).

These are regression tests for a real leak: redaction used to run as a logging
*filter* over ``record.msg``, which is the bare ``%``-format string. Any secret
passed as an interpolated argument was therefore never inspected, and reached
both stderr and the user's chat UI verbatim.
"""

from __future__ import annotations

import logging

from aws_sso_mcp.logging_setup import (
    MCPContextHandler,
    RedactingFormatter,
    bind_context,
)

SECRET_KEY = "ASIAABCDEFGHIJKLMNOP"  # a syntactically valid but fake STS key id


def _record(msg: str, *args: object, level: int = logging.INFO) -> logging.LogRecord:
    """Build a LogRecord the same way ``logger.info(msg, *args)`` would."""
    return logging.LogRecord(
        name="aws_sso_mcp.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or None,
        exc_info=None,
    )


def test_redacts_a_secret_passed_as_an_interpolated_argument() -> None:
    """The headline bug: a secret in ``record.args`` must not survive formatting."""
    formatter = RedactingFormatter("%(message)s")
    line = formatter.format(_record("device token for org=%s is %s", "org1", SECRET_KEY))

    assert SECRET_KEY not in line
    assert "REDACTED" in line
    assert "org1" in line


def test_redacts_a_secret_in_the_message_itself() -> None:
    formatter = RedactingFormatter("%(message)s")
    line = formatter.format(_record(f"minted accessKeyId={SECRET_KEY}"))

    assert SECRET_KEY not in line


def test_sensitive_key_name_in_the_format_string_still_emits() -> None:
    """A format string naming a sensitive key must not break the record.

    The old filter redacted ``record.msg`` *before* interpolation. With
    ``"minted accessKeyId=%s"`` the redaction swallowed the ``%s`` placeholder,
    interpolation then raised ``TypeError``, and the record was dropped with a
    ``--- Logging error ---`` on stderr instead of being logged.
    """
    formatter = RedactingFormatter("%(message)s")
    line = formatter.format(_record("minted accessKeyId=%s", SECRET_KEY))

    assert SECRET_KEY not in line
    assert "minted" in line


def test_formatting_does_not_mutate_the_record() -> None:
    """Two handlers must format identically; redaction is not allowed to be stateful.

    The old filter rewrote ``record.msg`` in place, so whichever handler ran
    second saw an already-mangled record and the output depended on handler
    registration order.
    """
    record = _record("token for %s is %s", "org1", SECRET_KEY)
    first = RedactingFormatter("%(message)s").format(record)
    second = RedactingFormatter("%(message)s").format(record)

    assert first == second
    assert record.msg == "token for %s is %s"
    assert record.args == ("org1", SECRET_KEY)


def test_non_sensitive_lines_pass_through_unchanged() -> None:
    formatter = RedactingFormatter("%(message)s")
    assert formatter.format(_record("queried %d accounts", 7)) == "queried 7 accounts"


class _RecordingContext:
    """Minimal stand-in for the MCP ``Context``, capturing forwarded messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, msg: str) -> None:
        self.messages.append(("info", msg))

    def warning(self, msg: str) -> None:
        self.messages.append(("warning", msg))

    def error(self, msg: str) -> None:
        self.messages.append(("error", msg))


def test_mcp_handler_forwards_redacted_text_to_the_client() -> None:
    """The chat UI is the other place a leaked secret would be visible."""
    handler = MCPContextHandler()
    handler.setFormatter(RedactingFormatter("%(message)s"))
    ctx = _RecordingContext()

    with bind_context(ctx):  # type: ignore[arg-type]
        handler.emit(_record("token for org=%s is %s", "org1", SECRET_KEY))

    assert len(ctx.messages) == 1
    level, text = ctx.messages[0]
    assert level == "info"
    assert SECRET_KEY not in text


def test_mcp_handler_routes_by_level() -> None:
    handler = MCPContextHandler()
    handler.setFormatter(RedactingFormatter("%(message)s"))
    ctx = _RecordingContext()

    with bind_context(ctx):  # type: ignore[arg-type]
        handler.emit(_record("careful", level=logging.WARNING))
        handler.emit(_record("broken", level=logging.ERROR))
        handler.emit(_record("fatal", level=logging.CRITICAL))

    assert [level for level, _ in ctx.messages] == ["warning", "error", "error"]


def test_mcp_handler_is_a_no_op_without_a_bound_context() -> None:
    """Outside a tool call there is no client to talk to; emitting must not raise."""
    handler = MCPContextHandler()
    handler.setFormatter(RedactingFormatter("%(message)s"))
    handler.emit(_record("no context bound"))
