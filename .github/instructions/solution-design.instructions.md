---
applyTo: "**"
---

# Solution design guide (architect role)

Use before writing code for any non-trivial change, and when reviewing one.

## 1. Design process
1. **Problem**: one sentence on the user need and who is affected. Name non-goals.
2. **Constraints**: IAM is the sole gate, read-only by default, credentials in memory, stdio MCP
   transport, Python 3.12+, cross-platform (Linux/macOS/Windows).
3. **Options**: list at least two approaches with trade-offs (complexity, security, latency, cost,
   reversibility). Pick the simplest that meets the constraints.
4. **Decision record**: for any decision that changes a boundary, dependency, data format or
   security posture, add a short ADR (context, decision, consequences) under `docs/adr/`.
5. **Plan the change** as small, independently shippable, tested steps.

## 2. Design principles
- **Simplicity first (KISS/YAGNI)**; add abstraction only on the third real use.
- **DRY (Don't Repeat Yourself)**: every piece of knowledge (a rule, constant, regex, schema,
  retry policy, error message) has one authoritative home; reuse it, don't copy it. Before
  adding logic, search for an existing helper. Apply the rule of three: tolerate two similar
  copies, extract on the third. DRY is about duplicated *knowledge*, not similar-looking code;
  don't merge things that change for different reasons, and don't build a flag-heavy
  abstraction just to avoid a few repeated lines (wrong abstraction costs more than duplication).
  Applies to tests (fixtures, parametrize), docs (link instead of re-stating) and CI config too.
- **SOLID**, pragmatically: single responsibility per module; depend on `Protocol`s; open for
  extension via new tools/planners, not by editing the executor.
- **Separation of concerns**: transport (`server.py`) / orchestration (`fanout`) / domain
  (`planner`, `roles`) / integration (`engine`, `sso`) / persistence (`cache`).
- **Make illegal states unrepresentable**: frozen dataclasses, enums, validated constructors.
- **Fail fast at the edge, degrade gracefully inside**: reject bad input immediately; isolate
  per-account failures and report partial results with clear status.
- **Least privilege and least surprise**: smallest permission set, no hidden side effects, no
  implicit defaults for region/account.
- **Reversibility**: prefer changes that are easy to roll back; avoid on-disk format changes
  without a version field and migration.

## 3. API / tool design (MCP)
- One tool = one clear intent, verb-first name, small typed input, documented output schema.
- Descriptions are written for an LLM: say when to use it, when not to, and its limits.
- Bound everything: max accounts, page size, output size, timeout. Return a truncation marker
  rather than silently cutting.
- Deterministic, sorted, machine-parseable output; errors are structured and actionable.
- Read-only unless explicitly designed otherwise; any mutating tool needs a security review.
- Backward compatibility: do not rename or remove tool fields without a deprecation note.

## 4. Resilience & scalability
- Every external call has a timeout, bounded retry with exponential backoff and jitter, and is
  safe to repeat (idempotent).
- Bounded concurrency (executor lanes); backpressure instead of unbounded queues.
- Single-flight for expensive refreshes; cache with explicit TTL, invalidation and size bound.
- Handle partial failure, throttling (`ThrottlingException`) and clock skew explicitly.
- Graceful shutdown: no orphaned threads, connections or temp files.

## 5. Data & state
- Single source of truth per datum; caches are derived and disposable.
- Schema changes are versioned and migrated; SQLite access is parameterised and thread-confined.
- Secrets never enter durable storage except the SSO token cache at mode `0600`.

## 6. Observability
- Structured, leveled logs to stderr; include correlation id, account id, operation, duration.
- Never log secrets; see `security.instructions.md`.
- Expose cache hit/miss counters and error counts for diagnosis.

## 7. Threat modelling (STRIDE) — run for new tools, boundaries, dependencies
| Threat | Ask | Typical mitigation here |
|---|---|---|
| Spoofing | Can someone impersonate the user/account? | SSO device auth; explicit account+region |
| Tampering | Can input/cache/config be altered? | Validation, `0600` files, parameterised SQL |
| Repudiation | Can actions be traced? | Logs of operation + account (no secrets); CloudTrail |
| Info disclosure | Can secrets leak via output/logs/errors? | Redact-then-truncate, clean `McpError`s |
| Denial of service | Unbounded work or retries? | Timeouts, caps, bounded lanes |
| Elevation of privilege | Can it exceed the user's IAM? | No extra allow logic; IAM is the gate; read-only tools |
Also consider prompt injection: AWS data and prompts are untrusted input.

## 8. Design review checklist
- [ ] Problem, non-goals and constraints stated
- [ ] Alternatives considered; ADR if boundary/format/security changes
- [ ] Layering and dependency direction preserved
- [ ] Failure modes, timeouts, retries and limits defined
- [ ] Threat model done; least privilege; no new secret exposure
- [ ] Tests (unit, error paths, concurrency) and docs planned
- [ ] Rollback/compat story is clear
