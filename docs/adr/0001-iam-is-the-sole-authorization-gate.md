# 1. IAM is the sole authorization gate

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The server executes AWS API calls on behalf of a user, chosen by an LLM. The obvious instinct is to
add a safety layer: an allow-list of tools, a deny-list of destructive operations, or a
confirmation prompt. The question is whether this server should hold *any* authorization logic.

Two facts shape the answer. First, the LLM is not a trust boundary - prompt injection via account
names, tags or error text can steer it. Second, the user's IAM policy already expresses, precisely
and centrally, what they may do; AWS evaluates it on every call regardless of what this server
thinks.

## Decision

The server performs **no** authorization. It never allows what IAM denies, and never denies what
IAM allows. The only safety posture in code is that tools are read-only by default.

## Consequences

- There is exactly one place to audit and one place to change: the IAM policy.
- No false sense of security from a bypassable client-side check, and no drift between a local
  allow-list and the real policy.
- CloudTrail remains the authoritative audit log, attributed to the user's own role.
- **Cost:** an over-permissioned user gets an over-permissioned assistant. The mitigation is
  documentation (`docs/configuration.md`) urging least-privilege roles, not code.
- **Cost:** a confused LLM can make an expensive-but-permitted call. Bounded concurrency and
  result caps limit the blast radius; they are not authorization.

## Alternatives considered

- **Client-side deny-list of destructive verbs.** Rejected: it is a speed bump, not a boundary,
  and it drifts from IAM. It would also imply a security guarantee that cannot be kept.
- **Per-call user confirmation.** Rejected for read-only tools: it trains users to click through
  prompts, which weakens the confirmation that a future mutating tool would genuinely need.
