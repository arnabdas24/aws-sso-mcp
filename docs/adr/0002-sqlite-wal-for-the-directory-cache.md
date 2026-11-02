# 2. SQLite in WAL mode for the directory cache

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

An organisation can have thousands of accounts, each with several role grants. Listing them costs
many paginated SSO API calls, far too slow to repeat per tool call. The cache must survive process
restarts, support a fan-out of hundreds of concurrent reads, and add no operational burden - this
is a desktop tool, so a server-backed store is out of the question.

## Decision

Cache the account and role directory in a local SQLite database in WAL mode (`cache/db.py`), with
one connection per thread held in `threading.local`, and writes wrapped in `BEGIN IMMEDIATE`.

## Consequences

- WAL lets concurrent readers proceed without blocking each other or the writer, so a fan-out over
  500 accounts scales roughly linearly.
- Persistence across restarts means a restart does not trigger a full directory re-sync.
- Indexed queries make account search cheap enough to run on every planning pass.
- `BEGIN IMMEDIATE` makes a refresh atomic: accounts and role grants are replaced together, so a
  reader never sees a half-refreshed directory.
- **Cost:** SQLite connections are not thread-safe, so every access path must go through the
  per-thread pool. This is easy to get wrong when adding code and is called out in the reviewer
  instructions.
- **Cost:** WAL creates `-wal` and `-shm` sidecar files, so the state directory is not a single
  file.

## Alternatives considered

- **In-memory only.** Rejected: every process restart would pay a full multi-minute re-sync.
- **JSON file.** Rejected: no concurrent access story, and a whole-file rewrite per role update.
- **Rolling our own index.** Rejected: SQLite is in the standard library and already correct.
