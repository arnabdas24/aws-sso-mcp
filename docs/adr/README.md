# Architecture Decision Records

Short records of the decisions that shape this codebase: what was decided, *why*, and what it
costs. They exist so a future maintainer (or an AI assistant) does not quietly undo a deliberate
trade-off because the reasoning was only ever a code comment.

Write a new ADR when a change alters a module boundary, a data format, a concurrency model, or the
security posture. Copy [`0000-template.md`](0000-template.md).

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-iam-is-the-sole-authorization-gate.md) | IAM is the sole authorization gate | Accepted |
| [0002](0002-sqlite-wal-for-the-directory-cache.md) | SQLite in WAL mode for the directory cache | Accepted |
| [0003](0003-synchronous-ttl-refresh.md) | Synchronous TTL-threshold refresh, no background workers | Accepted |
| [0004](0004-separate-token-cache.md) | A token cache separate from the AWS CLI's | Accepted |
| [0005](0005-narrow-redaction-with-a-prefilter.md) | Narrow redaction patterns behind a literal prefilter | Accepted |
| [0006](0006-token-expiry-margin.md) | Refuse tokens inside a five-minute expiry margin | Accepted |
