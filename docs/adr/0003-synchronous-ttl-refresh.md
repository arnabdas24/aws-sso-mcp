# 3. Synchronous TTL-threshold refresh, no background workers

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The cached directory goes stale. The usual options are a background refresher thread,
stale-while-revalidate, or refreshing inline when a caller notices staleness. The server is a
short-lived stdio process, and refreshing needs a valid SSO token, which may require a browser
login - something a background thread must never trigger on its own.

## Decision

Refresh lazily and synchronously. The first account-touching operation in each TTL window pays the
full refresh cost; every later call in that window is a local SQLite read. A per-org lock makes it
single-flight, and the token getter is called lazily so a fresh cache never touches SSO at all.

## Consequences

- No background threads means no surprise browser pop-ups, no work after the MCP client has gone
  away, and a far simpler shutdown path.
- Single-flight means a 500-account fan-out triggers exactly one refresh, not 500.
- Behaviour is deterministic and easy to test: no sleeping, no polling, no scheduler.
- **Cost:** one unlucky caller per TTL window waits for the full refresh. Accepted - it is visible
  and explicable, where a background refresh failing silently is not.
- **Cost:** the per-org lock is held across a network call. This is the single deliberate exception
  to the "never hold a lock across I/O" rule, and it is what makes single-flight work.

## Alternatives considered

- **Background refresher thread.** Rejected: it could demand an interactive login with no user
  present, and it keeps working after the client disconnects.
- **Stale-while-revalidate.** Rejected: returning known-stale role grants risks planning a call
  against a role the user no longer holds.
