"""Ask the user to confirm risky AWS operations before they run.

IAM stays the only *authorization* gate. This module adds *consent*: when a call
would delete, stop, change or create something (and so may be irreversible or incur
cost), the server asks the human directly
through the MCP client (elicitation) and runs nothing unless they accept. The
model cannot answer for the user, pass a flag, or retry its way past a refusal.

It fails closed. If the client cannot ask, or asking fails for any reason, the
call is refused with a clean error rather than run unconfirmed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from anyio import from_thread
from pydantic import BaseModel, Field

from .engine.catalog import Risk, operation_risk
from .errors import ConfirmationError
from .planner import ResolvedTarget
from .redact import redact

# (service, operation, params) for one planned call.
type PlannedCall = tuple[str, str, dict[str, Any]]

_MAX_ACCOUNTS_SHOWN = 15
_MAX_CALLS_SHOWN = 10
_MAX_PARAMS_CHARS = 400
_TRUNCATION_SUFFIX = "...[truncated]"


class _Confirmation(BaseModel):
    """The form shown to the user. Unticked means no."""

    confirm: bool = Field(
        default=False,
        description="Tick to run the operations listed above on those accounts.",
    )


def _client_can_ask(ctx: Any) -> bool:
    try:
        capabilities = ctx.client_capabilities
    except (AttributeError, ValueError):
        return False
    return capabilities is not None and capabilities.elicitation is not None


def _describe_params(params: dict[str, Any]) -> str:
    # Redact first, then truncate, so a cut can never split a secret past the patterns.
    text = redact(json.dumps(params, sort_keys=True, default=str))
    if len(text) <= _MAX_PARAMS_CHARS:
        return text
    return text[:_MAX_PARAMS_CHARS] + _TRUNCATION_SUFFIX


_WARNINGS: dict[Risk, str] = {
    "irreversible": "IRREVERSIBLE: permanently deletes or detaches resources. It cannot be undone.",
    "change": "This changes or interrupts existing resources and may disrupt running services.",
    "cost": "This may create billable resources and incur AWS charges.",
}


def _message(calls: Sequence[PlannedCall], region: str, targets: Sequence[ResolvedTarget]) -> str:
    accounts = ", ".join(
        redact(f"{t.account_name} ({t.account_id})") for t in targets[:_MAX_ACCOUNTS_SHOWN]
    )
    if len(targets) > _MAX_ACCOUNTS_SHOWN:
        accounts += f", and {len(targets) - _MAX_ACCOUNTS_SHOWN} more"
    lines = [
        "An AWS operation that needs your confirmation was requested. Nothing has run yet.",
        "",
        f"Region: {region}",
        f"Accounts ({len(targets)}): {accounts}",
        "Operations:",
    ]
    lines += [
        f"  {service}:{operation} {_describe_params(params)}"
        for service, operation, params in calls[:_MAX_CALLS_SHOWN]
    ]
    if len(calls) > _MAX_CALLS_SHOWN:
        lines.append(f"  ...and {len(calls) - _MAX_CALLS_SHOWN} more")
    risks = {risk for _, operation, _ in calls if (risk := operation_risk(operation))}
    lines += [""]
    lines += [f"WARNING - {_WARNINGS[risk]}" for risk in _WARNINGS if risk in risks]
    lines += ["", "Run it?"]
    return "\n".join(lines)


def make_confirmer(
    ctx: Any, *, region: str, calls: Sequence[PlannedCall]
) -> Callable[[Sequence[ResolvedTarget]], None] | None:
    """Build the check that asks the user before ``calls`` run, if any need it.

    Args:
        ctx: The MCP ``Context`` for the in-flight tool call, or ``None``.
        region: The region the calls will run in, shown to the user.
        calls: Every call the tool is about to make, as ``(service, operation, params)``.

    Returns:
        ``None`` when no call needs confirmation. Otherwise a function that takes the
        resolved accounts, asks the user, and raises :class:`ConfirmationError`
        unless they accept. It must be called from a tool's worker thread.
    """
    destructive = [call for call in calls if operation_risk(call[1])]
    if not destructive:
        return None

    names = ", ".join(sorted({f"{service}:{operation}" for service, operation, _ in destructive}))

    def _confirm(targets: Sequence[ResolvedTarget]) -> None:
        if not _client_can_ask(ctx):
            raise ConfirmationError(
                f"{names} needs your confirmation, but this client cannot "
                "ask for one. Nothing was run. Use a client that supports MCP elicitation, or "
                "make the change yourself."
            )
        try:
            result = from_thread.run(
                ctx.elicit, _message(destructive, region, targets), _Confirmation
            )
        # Boundary: fail closed. Any failure to ask the user means "not confirmed".
        except Exception as exc:
            raise ConfirmationError(
                f"Could not ask you to confirm {names}, so nothing was run."
            ) from exc
        data = getattr(result, "data", None)
        if result.action != "accept" or data is None or not data.confirm:
            raise ConfirmationError(f"{names} was not confirmed, so nothing was run.")

    return _confirm
