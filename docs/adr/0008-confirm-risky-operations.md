# 8. Risky operations need the user's confirmation

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

`aws_query` and `run_dag` sent any operation the selected role permitted, and IAM was the only check
(ADR 0001). A mistaken or prompt-injected request such as "delete these buckets" ran immediately,
against every matching account, if the role allowed it.

## Decision

Before any account is contacted, calls whose operation name starts with `Delete`, `Terminate`,
`Remove`, `Detach`, `Disassociate`, `Deregister`, `Revoke`, `Purge`, `Destroy`, `Release`, `Stop`,
`Reboot`, `Disable`, `Update`, `Put`, `Cancel`, `Modify`, `Reset`, `Suspend`, `Create`, `Run`, `Start`,
`Launch` or `Allocate` (plus
`ScheduleKeyDeletion`; a leading `Batch` is ignored) trigger an MCP **elicitation**: the client shows
the human a prompt naming the accounts, region and operations, with a checkbox.

- The prompt carries a warning by risk: **irreversible** (delete family), **disruptive** (stop, update,
  put, ...) or **may incur cost** (create, run, start, ...).
- The answer comes from the human through the client, so the model cannot confirm for itself. A
  `confirm=True` tool argument was rejected for exactly that reason.
- It fails closed. A decline, a cancel, an unticked box, a client without elicitation support, a missing
  context or any error raises `ConfirmationError` and nothing runs.
- `run_dag` asks once, for all steps, before the first one runs.
- The server also sends `instructions` telling the model not to assume missing facts, to say so, and not
  to retry a declined call.

This is **consent, not authorization**. The server still adds no allow or deny logic; IAM still decides
what the role may do. Confirmation only makes the user's intent explicit.

## Consequences

- Clients without elicitation support cannot run destructive operations through this server.
- Classification is by name prefix. Verbs that are not listed (reads, for example) are not prompted. Extending the list is a one-line change.
- `Put*` and `Update*` prompt too, so routine writes such as `PutMetricData` need a click each time.
- A client or agent that auto-approves elicitation defeats the prompt; that is outside this server's reach.
- Adds a direct `pydantic` dependency (already installed transitively by the MCP SDK).
- "Never assume" is a request to the model, not something the server can enforce.

## Alternatives considered

- **A `confirm` tool argument.** Rejected: the model fills it in.
- **A local deny-list.** Rejected: contradicts ADR 0001 and blocks legitimate work.
- **Prompt only on every write.** Rejected: too noisy; users would learn to click through.
