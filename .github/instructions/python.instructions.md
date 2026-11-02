---
applyTo: "**/*.py"
---

# Python architect & developer guide

Target: Python 3.12+, ruff (line length 100, double quotes), `mypy --strict`.
The linters are authoritative; this file covers what they cannot.

## 1. Style (PEP 8 / PEP 257)

- `snake_case` functions/variables, `PascalCase` classes, `UPPER_CASE` constants, `_private` helpers.
- Imports: stdlib, third party, first party, each group sorted (ruff `I`). No `import *`.
  Inline imports only with a comment giving the reason (e.g. optional dependency, import cycle,
  `anyio.from_thread` in `fanout.progress_reporter`).
- Compare with `is None`; use `isinstance`, never `type(x) ==`.
- Docstrings (PEP 257, Google style) on public modules, classes, functions: summary line, then
  `Args`/`Returns`/`Raises` only when not obvious. Do not restate the signature.
- Comments explain *why*, never *what*. Delete comments that narrate code.
- Early return instead of `else` after `return`/`raise`.

## 2. Types (PEP 484/585/604/695)

- Annotate every function. Use `list[str]`, `X | None`, `type Alias = ...`, `def f[T](x: T)`.
- No `Any` unless justified; prefer `Protocol`, `TypedDict`, frozen `dataclass(slots=True)`.
- No `# type: ignore` without an error code and reason.

## 3. Pythonic correctness

- Never use mutable defaults; use `None` and create inside, or `field(default_factory=...)`.
- Required dict keys: `d["key"]` (fail fast). Use `.get()` only when absence is valid.
- Comprehensions must not have side effects; use a loop.
- Use `pathlib`, `with` for every resource, `enum.Enum` over magic strings/numbers,
  `datetime.now(UTC)` (never naive), `secrets` for tokens.
- Binary operators on unsupported types return `NotImplemented`.
- Never use bare `except:` or `except Exception` without re-raise or a boundary comment.
  Catch the narrowest type, `raise ... from err`, keep the `try` body minimal.

## 4. System design

- **Layering**: respect the dependency direction in `.github/copilot-instructions.md`.
  Lower layers never import higher ones. No cycles.
- **Single responsibility**: functions ≲ 50 lines, ≤ 5 parameters, nesting ≤ 3–4 levels,
  cyclomatic complexity ≲ 10. Split before exceeding.
- **Functional core, imperative shell**: pure logic (planning, redaction, formatting) is separate
  from I/O (boto3, SQLite, filesystem). Inject clients/clocks/paths so tests need no patching.
- **Composition over inheritance**; depend on small `Protocol`s, not concrete classes.
- **Boundaries validate; the core trusts**: validate and normalise input at the tool layer, then
  pass typed objects inward.
- **Errors**: one taxonomy (`McpError` subclasses). Translate third-party exceptions at the
  boundary; messages are user-safe and contain no secrets.
- **Idempotent & deterministic**: sort outputs; make retries safe; bound every retry and timeout.
- **YAGNI**: no speculative abstraction. Three similar lines beat a premature framework.

## 5. Concurrency

- Shared mutable state needs a lock or must be thread-confined (`threading.local`).
- Hold locks briefly; never across network calls. Document lock ordering.
- Never mix blocking calls into `async` code; use `anyio.to_thread`.
- Cache keys must be stable (use `WeakKeyDictionary` on the object, never `id()`).
- SQLite: parameterised queries, one connection per thread, explicit transactions.
- Avoid N+1: batch calls or fan out with bounded concurrency.

## 6. Performance

- Measure before optimising. Prefer generators for large streams, `set`/`dict` for membership.
- No work at import time (no network, no file I/O); initialise lazily.
- Pagination: always follow tokens; cap total results.

## 7. Logging & observability

- `logging.getLogger(__name__)`; lazy formatting (`log.info("x %s", v)`), no f-strings in log calls.
- stderr only. Never log tokens, credentials, full request/response bodies.
- `log.exception` only where the exception is handled, once.

## 8. Testing (pytest)

- Test behaviour, not implementation. Arrange/Act/Assert; one reason to fail per test.
- Fake at the boundary (boto3 stubs, `tmp_path`, injected clock). No real network or AWS.
- Parametrize edge cases: empty, `None`, large, expired, concurrent, error paths.
- Deterministic under `pytest-randomly`; no sleeps, no ordering dependence.
- Every bug fix ships with a regression test that fails without the fix.

## 9. Dependencies & packaging

- Add dependencies only when needed; pin via `uv.lock`; justify each in the PR.
- Run `pip-audit`; prefer the standard library when adequate.

## 10. Definition of done

Ruff, mypy, vulture and pytest pass; tests added; docs updated; no secrets or debug prints;
Conventional Commit message.
