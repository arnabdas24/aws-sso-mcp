# 6. Refuse tokens inside a five-minute expiry margin

- **Status:** Accepted
- **Date:** 2025-11-02

## Context

`token_status` originally treated a token as usable whenever `expires_at > now`. A wide fan-out can
run for minutes, and AWS rejects a token the instant it lapses. A token with thirty seconds left
therefore passed the check, and the run failed partway through with scattered per-account
authentication errors - the worst possible outcome, since partial results look like a permissions
problem rather than an expired sign-in.

## Decision

Treat a token as unusable once it falls inside a five-minute margin (`_EXPIRY_MARGIN` in
`sso/device_auth.py`). The status reason distinguishes `expired` from
`expiring within 5 minutes`, so the cause is never ambiguous.

## Consequences

- A fan-out either starts with a token that will survive it, or fails immediately with a clear
  "log in" instruction.
- An entire class of confusing mid-run partial failure disappears.
- **Cost:** up to five minutes of otherwise-valid token life is discarded, so users re-authenticate
  marginally more often. A cheap trade for a deterministic failure mode.
- **Cost:** the margin is a fixed constant, not proportional to the size of the planned fan-out.
  A run longer than the token's remaining life can still fail mid-way; that would need the planner
  to estimate duration, which is not worth the complexity today.

## Alternatives considered

- **Refresh mid-fan-out.** Rejected: refreshing may require an interactive browser login, which
  must never happen outside `login()`.
- **A margin proportional to the planned call count.** Deferred: it needs a duration estimate the
  planner does not currently produce.
