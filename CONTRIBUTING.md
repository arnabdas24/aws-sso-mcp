# Contributing

Thanks for considering a contribution. This project optimises for **correctness and security over
feature velocity** - it brokers AWS credentials, so a subtle bug is a security bug.

For security problems, do **not** open an issue: see [SECURITY.md](SECURITY.md).

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/arnabdas24/aws-sso-mcp
cd aws-sso-mcp
uv sync --locked --extra dev
uv run pre-commit install
```

## Quality gates

These are exactly what CI enforces, so run them before pushing. Each gate is explained in
[`docs/developer-guide.md`](docs/developer-guide.md#quality-gates):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run pytest                       # branch coverage, fail_under=85, warnings are errors
uv run python scripts/check_coverage_floors.py   # per-module floors
uv run vulture                      # dead code
uv run python scripts/check_doc_links.py         # links and anchors in *.md
```

Non-negotiables:

- `mypy --strict` must stay clean. No `# type: ignore` without an error code and a reason.
- Every behaviour change ships with a test; every bug fix ships with a **regression test that
  fails without the fix**.
- New MCP tools must be added to `vulture_whitelist.py`.
- Security-critical modules have per-module coverage floors in
  `scripts/check_coverage_floors.py`. Raising a floor is welcome; lowering one needs a reason.

Mutation testing (`mutmut`) runs weekly in CI over the security-critical modules. It is advisory,
never a merge gate, and is Linux only. See
[`docs/developer-guide.md`](docs/developer-guide.md#mutation-testing).

## Design rules

Read [`.github/copilot-instructions.md`](.github/copilot-instructions.md) for the invariants, and
[`docs/developer-guide.md`](docs/developer-guide.md) for background. The short version:

- Respect the one-way layering: `server.py` -> `fanout.py` -> `planner`/`roles` -> `engine/` ->
  `sso/` -> `cache/`.
- IAM is the only authorization gate. Do not add allow/deny logic.
- Credentials stay in memory. Redact before truncating. Log to stderr, never stdout.
- Significant design decisions get an ADR in [`docs/adr/`](docs/adr/README.md).

## Commits and pull requests

[Conventional Commits](https://www.conventionalcommits.org/), header <= 100 characters, enforced by
commitlint. `release-please` derives the version and changelog from them, so the prefix matters:

```
feat(cache): invalidate role grants with the account directory
fix(sso): write the token cache atomically
```

Keep pull requests focused, explain the user-visible effect, and note any new dependency and why
the standard library will not do.

## Using AI assistants

Instructions for AI tools live in [`AGENTS.md`](AGENTS.md) and `.github/instructions/`. If you use
an assistant, you remain the author: review its output against the same gates above. The
`python-reviewer` agent in `.github/agents/` performs the same review pass locally.
