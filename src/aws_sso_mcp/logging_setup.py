"""Structured logging for aws-sso-mcp - stderr-only, MCP-client-bridged.

Two independent channels:

1. **stderr, JSON-safe.** ``configure(...)`` installs one handler that writes
   every record to :data:`sys.stderr` at the configured level. Never touches
   stdout - the stdio MCP transport uses stdout for JSON-RPC framing, so a
   stray print or a stdout logger would corrupt the wire.

2. **MCP client.** :class:`MCPContextHandler` forwards any INFO+ record
   emitted while a tool call is in flight through the ``Context`` object's
   ``info/warning/error`` methods, so the user sees them in their chat UI.
   The active ``Context`` is bound per-request via :func:`bind_context`
   (a :mod:`contextvars` variable, so it works across threads inheriting
   the caller's context).

Both channels format through :class:`RedactingFormatter`, which redacts the
fully rendered line - including interpolated ``%s`` arguments - as
defense in depth against a rogue log line ever leaking a bearer token.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import anyio.from_thread

from .redact import redact

if TYPE_CHECKING:  # avoid a hard import cycle at startup
    from mcp.server.mcpserver.context import Context

_configured = False
_root_logger_name = "aws_sso_mcp"

# Active MCP Context for the currently-executing tool call, if any.
# ContextVar is the right primitive here: it's inherited by threads spawned
# from the current context (executor.submit copies context), and it never
# leaks across concurrent tool invocations.
_active_ctx: ContextVar[Context | None] = ContextVar("aws_sso_mcp_ctx", default=None)


class RedactingFormatter(logging.Formatter):
    """A formatter that redacts the *fully rendered* log line.

    Redaction has to happen after ``%``-style argument interpolation, not
    before. ``logger.info("token for %s is %s", org, secret)`` carries the
    secret in ``record.args``, so redacting ``record.msg`` - which at that
    point is still the bare format string ``"token for %s is %s"`` - inspects
    a string that provably cannot contain the secret, and the real value is
    spliced in immediately afterwards, unredacted.

    This is also why redaction belongs in a formatter rather than a filter. A
    filter that rewrote ``record.msg`` in place mutated state shared by every
    handler, so the result depended on handler registration order, and a
    format string that itself contained a sensitive key name (e.g.
    ``"minted accessKeyId=%s"``) had its ``%s`` placeholder replaced by the
    redaction marker - after which interpolation raised ``TypeError`` and the
    record failed to emit at all.

    Formatting is per-handler and non-destructive, so neither problem can
    arise: each handler renders the record independently and redacts its own
    output.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` normally, then redact the finished line."""
        return redact(super().format(record))


class MCPContextHandler(logging.Handler):
    """Forward log records to the current MCP client Context, best-effort.

    Runs at the handler level (not the logger level) so a single dispatch
    covers every module. The lookup is one contextvar read per record;
    if no Context is bound (e.g. during startup, tests, or between calls),
    the handler is a no-op.
    """

    def emit(self, record: logging.LogRecord) -> None:
        ctx = _active_ctx.get()
        if ctx is None:
            return
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 - a broken format must not break logging
            return
        # ctx.* are async methods on the MCP SDK; we schedule via anyio's
        # from_thread bridge if we're off the event loop, otherwise call
        # directly. Any failure is swallowed - a broken client link must
        # never break a query.
        method_name = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warning",
            logging.ERROR: "error",
            logging.CRITICAL: "error",
        }.get(record.levelno, "info")
        method = getattr(ctx, method_name, None) or getattr(ctx, "info", None)
        if method is None:
            return
        with contextlib.suppress(Exception):
            _dispatch_ctx_call(method, msg)


def _dispatch_ctx_call(method: Any, msg: str) -> None:
    """Invoke a Context.info/warning/error method from any thread, best-effort."""
    result = method(msg)  # may be a coroutine or None
    # If it's a coroutine (common), we need to drive it on the anyio loop.
    if result is None:
        return
    try:
        # Grab the calling worker thread's token if present; otherwise skip.
        token = anyio.from_thread.threadlocals.current_token  # type: ignore[attr-defined]
    except AttributeError:
        # No worker-thread token - either we're already on the loop
        # (in which case the coroutine has been scheduled by the SDK) or
        # we're in a dispatch pool thread that lacks a token. Nothing to do.
        return
    with contextlib.suppress(Exception):
        anyio.from_thread.run(_await, result, token=token)


async def _await(coro: Any) -> None:
    await coro


def configure(level: str | int = "INFO") -> None:
    """Install one stderr handler + the MCP-context bridge, idempotently.

    Safe to call more than once - subsequent calls only adjust the level.
    Also disables IMDS lookups if the environment hasn't opted in, so
    ``boto3.client(...)`` construction doesn't stall on non-EC2 hosts.
    """
    global _configured
    root = logging.getLogger(_root_logger_name)

    numeric = getattr(logging, level.upper(), logging.INFO) if isinstance(level, str) else level
    root.setLevel(numeric)

    if _configured:
        for h in root.handlers:
            h.setLevel(numeric)
        return

    formatter = RedactingFormatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    stderr = logging.StreamHandler(stream=sys.stderr)
    stderr.setFormatter(formatter)
    stderr.setLevel(numeric)
    root.addHandler(stderr)

    mcp_handler = MCPContextHandler()
    mcp_handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    mcp_handler.setLevel(logging.INFO)
    root.addHandler(mcp_handler)

    # Don't propagate to Python's root logger - avoids double-print if the
    # host process already installed a root handler.
    root.propagate = False

    # Quiet noisy libs at INFO; keep them at WARN unless explicitly bumped.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))

    # Skip IMDS probes on non-EC2 hosts - saves ~1s+ per boto3.client() on
    # a local dev laptop with no metadata service reachable.
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger under the ``aws_sso_mcp`` root."""
    if name.startswith(_root_logger_name):
        return logging.getLogger(name)
    return logging.getLogger(f"{_root_logger_name}.{name}")


@contextlib.contextmanager
def bind_context(ctx: Context | None) -> Iterator[None]:
    """Bind ``ctx`` as the active MCP Context for the duration of a tool call.

    Uses :mod:`contextvars` so nested calls and executor-inherited contexts
    correctly see the same Context, and concurrent tool calls never share
    each other's binding.
    """
    token = _active_ctx.set(ctx)
    try:
        yield
    finally:
        _active_ctx.reset(token)
