# 7. TOML rules are the only role selector

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

Every query tool used to accept an optional `role=` argument, and when a call named no role and no rule
matched, the server used the account's only granted role. Resolution order was: explicit `role=`, then
the TOML environment rules, then a sole grant.

The model fills in tool arguments, so it could pass `role="AdministratorAccess"` on its own, after being
nudged by an error that listed the granted roles, or because text in AWS data (a tag, a description)
told it to. The server accepted any role the user held and skipped the user's own rules. That
contradicted the documented claim that the model never picks a role, and it let a prompt injection
escalate within the user's grants. ADR 0001 keeps IAM as the authorization gate; this is a different
question: which of the user's roles is *selected*.

## Decision

Role selection comes **only** from the `[portals.<org>.environments.*]` rules in the TOML config.

- The `role` argument is removed from `aws_query`, `run_dag` and `explain_role_selection`.
  `resolve_role` has no override parameter.
- The sole-grant default is removed. An account that matches no rule, or whose rule matches no granted
  role, is refused with a `RoleResolutionError` that lists the roles the user holds.

This is selection, not authorization: the server still adds no allow or deny logic of its own, and IAM
still decides what the chosen role may do.

## Consequences

- Neither the model, the caller nor injected text can choose or escalate a role. To use a different role,
  the user edits their config.
- Fan-out roles are predictable: one account, one role, visible in `explain_role_selection` and in each
  result's `role` field.
- **Breaking change:** accounts that matched no environment and relied on a sole grant or an explicit
  `role=` now fail until a rule covers them. Users must add environments.
- A client that still sends `role` has it ignored (the framework drops unknown arguments); it is not an
  error, so the result's `role` field is the place to check what was used.
- **Cost:** one-off use of a different role needs a config edit and a restart. That friction is the
  point of the decision.
- Users who hold powerful roles in the same SSO session can still map a rule to one. Keeping the config
  least-privilege is still the user's job (ADR 0001).

## Alternatives considered

- **Keep `role=` behind a config flag, off by default.** Rejected: it keeps the escalation path in the
  code and adds a mode to test and document, for a use the config already covers.
- **Allow `role=` only for roles named in the TOML.** Rejected: it adds a second, partly redundant
  allow-list and still lets the model pick between configured roles.
- **Keep the sole-grant default.** Rejected: it silently selects a role no rule asked for, which is
  exactly the default-role behaviour this decision removes.
