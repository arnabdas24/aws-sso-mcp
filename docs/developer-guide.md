# Developer guide

Everything you need to set up, understand, change, test and release `aws-sso-mcp`.
New to the project? Read [how-it-works.md](how-it-works.md) and [architecture.md](architecture.md) first.

## Contents

- [Setup](#setup)
- [Daily workflow](#daily-workflow)
- [Quality gates](#quality-gates)
- [Codebase tour](#codebase-tour)
- [Coding conventions](#coding-conventions)
- [Testing](#testing)
- [Recipes: common changes](#recipes-common-changes)
- [Debugging](#debugging)
- [Commits, PRs and releases](#commits-prs-and-releases)
- [Design invariants to protect](#design-invariants-to-protect)

## Setup

**Prerequisites:** Python 3.12+ (see [.python-version](../.python-version)) and
[`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/arnabdas24/aws-sso-mcp.git
cd aws-sso-mcp
uv sync --extra dev
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Check it works:

```bash
uv run aws-sso-mcp --version
uv run pytest
```

**Never use real AWS accounts in tests.** The suite uses botocore `Stubber` and fixtures that isolate
`$HOME`, the config path and the state directory (see [tests/conftest.py](../tests/conftest.py)).

### Running the server from your checkout

```bash
mkdir -p ~/.config/aws-sso-mcp && cp examples/config.example.toml ~/.config/aws-sso-mcp/config.toml
uv run aws-sso-mcp          # waits for an MCP client on stdio; Ctrl+C to stop
```

To try it from VS Code, add a workspace `.vscode/mcp.json`:

```json
{
  "servers": {
    "aws-sso-mcp-dev": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "aws-sso-mcp"],
      "cwd": "C:\\path\\to\\aws-sso-mcp",
      "env": {
        "AWS_SSO_MCP_CONFIG": "C:\\path\\to\\dev-config.toml",
        "AWS_SSO_MCP_STATE_DIR": "C:\\path\\to\\dev-state"
      }
    }
  }
}
```

Use a **separate state dir** while developing so you do not disturb your real tokens and cache.

For protocol-level debugging without a chat client, use the Python smoke test in
[examples/README.md](../examples/README.md#protocol-smoke-test).

## Daily workflow

```bash
uv run ruff check . --fix      # lint (auto-fix what it can)
uv run ruff format .           # format
uv run mypy src                # strict type check
uv run vulture                 # dead-code check
uv run pytest                  # tests + coverage (must be >= 85%)
uv run python scripts/check_coverage_floors.py   # per-module floors (needs coverage.json)
uv run python scripts/check_doc_links.py         # relative links and anchors in *.md
```

Or run everything the hooks run: `uv run pre-commit run --all-files`.

## Quality gates

CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) runs the same checks, using
`uv sync --locked --extra dev`.

| Gate | Command | Notes |
|---|---|---|
| Lint | `ruff check .` | Rule sets in `pyproject.toml`; includes `S` (security), `BLE`, `TRY`, `DTZ` |
| Format | `ruff format --check .` | Line length 100, double quotes |
| Types | `mypy src` | `strict = true`, `warn_unreachable` |
| Dead code | `vulture` | See whitelist below |
| Tests | `pytest` | `--cov`, `fail_under = 85`, branch coverage, **warnings are errors**, random order (`pytest-randomly`) |
| Per-module coverage | `python scripts/check_coverage_floors.py` | Higher floors for security-critical modules; see below |
| Doc links | `python scripts/check_doc_links.py` | Every relative link and `#anchor` in the Markdown must resolve |
| Mutation | [mutation.yml](../.github/workflows/mutation.yml) | `mutmut`, weekly and advisory only; see below |
| Package | `python -m build` + `twine check` | Release smoke test |
| Security | [security.yml](../.github/workflows/security.yml) | `pip-audit`, `zizmor` |

### Lint rules worth knowing

- **`BLE` (blind except)** is enabled on purpose. Broad `except Exception` is allowed only at isolation
  boundaries with a justified `# noqa: BLE001 - reason`. Existing examples: `_safe` in `server.py`,
  best-effort audit and progress reporting.
- **`DTZ`**: never create naive datetimes. Use `datetime.now(UTC)`.
- **`TRY003` is ignored**: errors carry deliberate, user-facing messages.
- Tests relax `S101`, `ARG`, `TRY` and similar.

### The vulture whitelist

[vulture_whitelist.py](../vulture_whitelist.py) lists names that look unused but are live, such as **MCP
tools** (registered by decorator, never called from Python). If you add a tool, add it there. If vulture
flags anything else, delete the dead code instead of whitelisting it, unless you can state a concrete
reason.

### Per-module coverage floors

The global floor is 85%, but a module that merely *runs* is not the same as a module that is
*tested*. [scripts/check_coverage_floors.py](../scripts/check_coverage_floors.py) reads the JSON
coverage report and enforces a higher floor per security-critical module (`redact.py`,
`hardening.py`, `engine/catalog.py` and `roles.py` are at 100%). Generate the report first:

```bash
uv run pytest --cov-report=json:coverage.json
uv run python scripts/check_coverage_floors.py
```

Raising a floor after you improve a module is welcome. Lowering one needs a reason in the pull
request.

### Mutation testing

Coverage proves a line *ran*; it cannot prove an assertion would have *caught* a bug. `mutmut`
makes small edits to the source (`>` becomes `>=`, `and` becomes `or`, a line is deleted) and
reruns the suite. A mutant that **survives** marks code that is executed but unasserted.

It runs weekly from [mutation.yml](../.github/workflows/mutation.yml), is **advisory and never a
merge gate** (runs are slow and some mutants are equivalent by construction), and is scoped by
`[tool.mutmut]` in `pyproject.toml` to the security-critical modules. It is **Linux only** -
`mutmut` imports the Unix-only `resource` module, so it cannot run on Windows:

```bash
uv run mutmut run       # Linux/macOS only
uv run mutmut results
```

Treat a surviving mutant as a question ("would a test have noticed?"), not an automatic defect.

## Codebase tour

Suggested reading order for a new contributor:

| # | File | Why read it |
|---|---|---|
| 0 | [code-walkthrough.md](code-walkthrough.md) | A per-file map with call chains; keep it open while reading the code |
| 0 | [technical-deep-dive.md](technical-deep-dive.md) | Function-level mechanics: auth, token/credential caching, concurrency, known quirks |
| 1 | [server.py](../src/aws_sso_mcp/server.py) | The public surface: every tool, `_safe`, `_get_state` |
| 2 | [fanout.py](../src/aws_sso_mcp/fanout.py) | The heart: `run_fanout` and `_FanOut` |
| 3 | [planner.py](../src/aws_sso_mcp/planner.py), [roles.py](../src/aws_sso_mcp/roles.py) | Which accounts, which role |
| 4 | [engine/catalog.py](../src/aws_sso_mcp/engine/catalog.py) | The one place that calls AWS |
| 5 | [engine/executor.py](../src/aws_sso_mcp/engine/executor.py) | Threads, timeouts, isolation |
| 6 | [sso/](../src/aws_sso_mcp/sso) | Login, credentials, directory |
| 7 | [cache/](../src/aws_sso_mcp/cache) | SQLite directory cache |
| 8 | [redact.py](../src/aws_sso_mcp/redact.py), [hardening.py](../src/aws_sso_mcp/hardening.py) | Secret scrubbing and size caps |

### Module dependency rules

- `server.py` is the only module that knows about MCP tools. Keep it **thin**: validate, delegate.
- `fanout.py` owns shared pipeline logic. Never duplicate it inside a tool.
- `engine/dag.py` and `roles.py` are **pure** (no network, no I/O). Keep them that way so they stay trivially
  testable.
- `engine/catalog.py` is the only module that calls a *service* API.
- `redact.py` and `errors.py` import nothing from the project; this prevents import cycles.

## Coding conventions

1. **Typed everywhere.** `mypy --strict` must pass. Use `Any` only where boto3's dynamic nature forces it,
   and scope it narrowly (see the `_build_sso_client` docstring).
2. **User-facing errors subclass `McpError`** ([errors.py](../src/aws_sso_mcp/errors.py)). Never let a raw
   `ClientError` or `BotoCoreError` cross a tool boundary; catch it at the edge and raise `AwsCallError`
   with only the code and message.
3. **Log with the module logger**: `logger = get_logger(__name__)`. Never `print()` (stdout is the protocol).
   Use `%s` lazy formatting (ruff rules `G` and `LOG`). Do not log parameter values; names only.
4. **UTC only.** Persisted timestamps end in `Z` (`%Y-%m-%dT%H:%M:%SZ`).
5. **Comments explain *why*,** not what. The codebase documents hard-won reasons (for example why timeouts
   exclude queue time). Keep that standard.
6. **Docstrings** use Google style with `Args`, `Returns`, `Raises`.
7. **Frozen, slotted dataclasses** for value objects (`@dataclass(frozen=True, slots=True)`).
8. **Thread safety is explicit.** Any new shared mutable state needs a named lock and a comment.
9. **No secrets anywhere.** Not in config, logs, audit entries, error messages or tests (use obviously fake
   values).

## Testing

```bash
uv run pytest                              # everything
uv run pytest tests/test_roles.py          # one file
uv run pytest -k "access_denied"           # by name
uv run pytest --no-cov -x -q               # fast, stop on first failure
uv run pytest -p no:randomly               # deterministic order when debugging
```

| Test file | Covers |
|---|---|
| `test_config.py` | Config parsing, secret-key rejection, validation errors |
| `test_roles.py` | Role resolution order and error cases |
| `test_planner.py` | Selector filtering, ambiguity, not-found |
| `test_dag.py` | Ordering, `$ref` substitution, extraction, cycles |
| `test_catalog.py` | Dispatch, pagination, truncation, error translation |
| `test_executor.py` | Isolation, timeouts, queue-time accounting, progress |
| `test_broker.py`, `test_device_auth.py`, `test_directory.py` | SSO layers (stubbed) |
| `test_account_cache.py` | TTL, single-flight refresh, invalidation |
| `test_audit.py` | Background writer, flush, redaction |
| `test_redact.py`, `test_hardening.py`, `test_logging_setup.py` | Secrets never leak |
| `test_server_tools.py`, `test_cli.py` | Tools called as plain Python functions; CLI flags |
| `test_properties.py` | Hypothesis property tests |

Conventions:

- **Tools are plain callables in tests.** `@app.tool()` returns the original function, so call
  `server_module.orgs_list()` directly. Install state with `server_module._state = AppState.build(...)`.
- **AWS is always stubbed** with botocore `Stubber`. No test touches the network.
- **Fixtures isolate the filesystem** (`isolated_env`, `sample_config_path`). An autouse fixture clears the
  token memory cache and boto3 client cache between tests.
- **Audit tests** use the `flush_audit` fixture because the writer is a background thread.
- **Warnings fail the build.** Fix the cause, do not filter the warning.
- Add tests for behaviour you introduce or fix, especially negative paths and thread-safety edge cases.

## Recipes: common changes

### Add a new MCP tool

1. Add a function in `server.py` decorated with `@app.tool()`.
2. Keep the body inside a nested `_run()` and `return _safe(_run, ctx)`. Accept `ctx: Context | None = None`
   if it can take long and should show progress or logs.
3. Write a docstring aimed at the **model**: say what it does, when to use it, and constraints. The model
   reads it.
4. If it touches accounts, **do not reimplement the pipeline**. Call `run_fanout` with a per-account
   callback.
5. Add the function to [vulture_whitelist.py](../vulture_whitelist.py).
6. Add tests in `tests/test_server_tools.py`.
7. Document it in [tools-reference.md](tools-reference.md) and, if user-facing, [prompt-guide.md](prompt-guide.md).

### Add a new account selector

1. Add the field to `AccountSelector` (`fanout.py`).
2. Add the parameter to `_selector()` and to the signatures of `aws_query` and `run_dag` in `server.py`.
3. Thread it through `plan_targets` -> `resolve_targets` -> `_AccountFilter` / `_build_filter` (`planner.py`).
4. Keep `search_accounts` semantics identical if it is a name filter (see the `instr(lower())` note in
   `cache/accounts.py`).
5. Test in `test_planner.py`; document in `tools-reference.md`.

### Add a new environment match mode

1. Add it to `_VALID_MATCH_MODES` in `config.py`.
2. Handle it in `_matches` in `roles.py` (it raises `ValueError` for unknown modes).
3. If it needs compile-time validation, extend `_parse_environment_rule`.
4. Update [configuration.md](configuration.md).

### Change a performance or safety limit

- Defaults live in `config.py` (`PerformanceConfig`, `DEFAULT_MAX_ITEMS`). Update the example config and
  [configuration.md](configuration.md) together.
- Output caps live in `hardening.py` (`MAX_STRING_LENGTH`, `MAX_LIST_ITEMS`).

### Add a redaction pattern

1. Add the key to `_SENSITIVE_KEYS` or a regex to `_BARE_PATTERNS` in `redact.py`.
2. **Update `_PREFILTER`.** It is built from `_SENSITIVE_KEYS` automatically, but a new bare pattern needs
   its literal prefix added by hand, or it will be skipped by the fast path and never redact.
3. Keep bare patterns **narrow**; broad ones false-positive on ARNs and ids.
4. Add cases to `test_redact.py`, including near-miss strings that must *not* be redacted.

### Change the DB schema

1. Edit `_SCHEMA` and bump `SCHEMA_VERSION` in `cache/db.py`.
2. The cache is disposable (see `_CACHE_TABLES`), so a version bump drops and rebuilds those tables.
3. Never store tokens or credentials in this database.

### Add a dependency

Add it to `pyproject.toml`, run `uv lock`, and commit `uv.lock`. CI uses `--locked`, and the security
workflow audits dependencies.

## Debugging

| Goal | How |
|---|---|
| See what the server is doing | Set `log_level = "DEBUG"` in your dev config; logs go to **stderr** |
| See logs in VS Code | `MCP: Show Output` |
| Reproduce a tool without a client | Call the function in a Python shell after building `AppState`, or use the smoke test in `examples/README.md` |
| Check a role decision | `explain_role_selection` |
| Inspect the cache | `sqlite3 <state_dir>/cache.db "select * from sync_state;"` |
| Inspect calls made | `<state_dir>/audit.jsonl` |
| Hunt a deadlock or hang | Thread names are prefixed `aws-sso-mcp-cred`, `-svc`, `-dispatch`, `-audit` |
| A token seems stuck | Delete `<state_dir>/tokens/<org>.json` and restart (memory cache is per process) |

Common pitfalls:

- **Printing to stdout** corrupts the MCP stream. Use the logger.
- **Forgetting `auto_login=False`** in new code paths. Only `login()` may start a browser flow.
- **Blocking on the credential lane from inside the credential lane** deadlocks. Mirror the existing
  dispatch-thread pattern.
- **Caching boto3 clients by `id()`** is a security bug. Use `build_client` in `awscfg.py`.

## Commits, PRs and releases

### Conventional Commits

Enforced by commitlint (a `commit-msg` hook) because release-please derives versions and the changelog
from commit messages. Header max length is 100, and the subject must be lower-case style.

```text
feat: add exclude_account_names selector
fix: redact tokens split across log arguments
docs: add architecture diagrams
```

Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
Mark breaking changes with `!` (`feat!: ...`) or a `BREAKING CHANGE:` footer.

### Pull requests

- Branch from the current feature branch or `main`, as agreed with the maintainers.
- Ensure all [quality gates](#quality-gates) pass locally.
- Describe *why*, not just what. Mention any new thread-safety or security considerations.
- Update the docs affected by your change (this directory, plus `examples/` if setup changed).
- Code ownership is declared in [.github/CODEOWNERS](../.github/CODEOWNERS).

### Releases

Automated by release-please ([release-please-config.json](../release-please-config.json)):

1. Merging conventional commits to the default branch opens or updates a release PR that bumps the version
   (`pyproject.toml`, `uv.lock`) and `CHANGELOG.md`.
2. Merging that PR tags the release; [release.yml](../.github/workflows/release.yml) builds and publishes
   to PyPI via Trusted Publishing, with a build-provenance attestation and a CycloneDX SBOM.
3. Rehearse a publish manually against TestPyPI before a first release; see
   [CONTRIBUTING.md](../CONTRIBUTING.md).

Do not edit the version by hand for a real release. `pyproject.toml` is the only place the version is
written; `aws_sso_mcp.__version__` reads it back from the installed package, so `--version` cannot drift.
For a manual pre-release bump (`0.1.0rcN`) edit `version` in `pyproject.toml`, run `uv lock`, and commit
both.

## Design invariants to protect

If a change would violate one of these, stop and discuss it first.

1. **IAM is the only authorization boundary.** Do not add local allow- or deny-lists for operations.
   `is_read_operation` is a *label* for the audit log.
2. **The model never chooses a role.** Role selection stays deterministic in `roles.py`.
3. **Only `login()` may open a browser.** Every other path uses `auto_login=False`.
4. **Pre-flight tokens before planning.**
5. **Temporary credentials never touch disk.**
6. **Nothing secret reaches logs, errors, outputs or the audit log.** Redact before truncating; redact the
   rendered log line, not the format string.
7. **One account's failure never aborts the fan-out.**
8. **Region is always explicit.** Never default it.
9. **`stdout` belongs to the protocol.**
10. **Results are deterministic** (sorted), and truncation is always explicit, never silent.
11. **Config can never hold secrets**, and the loader enforces it.
