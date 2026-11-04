# aws-sso-mcp — repository instructions

Python 3.12+ MCP server for AWS IAM Identity Center (SSO). Package: `src/aws_sso_mcp`.
Deep docs: `docs/developer-guide.md`, `docs/internals.md`.
Detailed rules live in `.github/instructions/` (`python`, `security`, `code-reviewer`).

## Commands (run before finishing any change)

```
uv sync --locked --extra dev
ruff check . && ruff format --check .
mypy --strict src
pytest --cov-report=json:coverage.json   # branch coverage, fail_under=85, warnings are errors
python scripts/check_coverage_floors.py  # per-module floors for security-critical code
vulture           # new MCP tools must be added to vulture_whitelist.py
```

Mutation testing (`mutmut`) runs weekly in CI and is Linux-only; it cannot run on Windows.

## Architecture (dependency direction is one-way, downward)

`server.py` (thin tools) → `fanout.py` → `planner`/`roles` → `engine/` (`catalog.execute_call` is the
ONLY service-API caller) → `sso/` → `cache/`, `awscfg`.

## Invariants — never break

- IAM is the only authorization gate; do not add allow/deny logic. Destructive operations still need the
  human's confirmation via MCP elicitation, failing closed (consent, not authorization; ADR 0008).
- Roles come only from the TOML environment rules: no `role=` tool argument, no sole-grant default.
- Only `login()` may open a browser; everything else uses `auto_login=False`.
- Region is always required; never default it.
- Credentials live in memory only; never log, persist or return them.
- Redact, then truncate. New bare-secret regex needs its prefix in `_PREFILTER`.
- Tools raise `McpError` subclasses with clean messages; no raw botocore reprs.
- One account failing must not fail the fan-out; results are sorted.
- No lock held across slow network calls (except per-org single-flight refresh).
- stdout is the MCP protocol channel: log to stderr, never `print`.

## Conventions

- Conventional Commits (header ≤ 100 chars). Add trailer
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.
- Add or update tests with every behaviour change; update docs that describe it.
- Decisions that change a boundary, data format or security posture get an ADR in `docs/adr/`.
- Security issues are reported privately; see `SECURITY.md`.
- Trust these instructions; search only if they are incomplete or wrong.
