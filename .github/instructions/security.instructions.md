---
applyTo: "**"
---

# Security guidelines

Threat model: a local MCP server holding short-lived AWS credentials, driven by an LLM whose
inputs (prompts, AWS resource names/tags, API responses) can be attacker-influenced.

## Secrets & credentials
- Never hard-code or commit secrets, tokens, account IDs of real customers, or `.env` files.
- SSO token cache file: created with mode `0600` atomically (`os.open`), never world-readable.
- Role credentials stay in memory; never write them to disk, logs, exceptions or tool output.
- Config keys that look like secrets are rejected; keep that check.
- Redact all outbound text (`redact()`) before truncation; test new patterns with real-format samples.

## Injection
- SQL: parameter placeholders only; never f-string/`%`/`+` into queries.
- Commands: no `shell=True`, no `os.system`; pass argument lists; validate against allow-lists.
- Paths: resolve and confirm they stay inside the intended base (`Path.resolve().is_relative_to`).
- No `eval`, `exec`, `pickle.loads`, `yaml.load` (use `safe_load`), `marshal` on untrusted data.
- Regex on untrusted input: avoid catastrophic backtracking; bound input length.

## AWS / IAM
- IAM is the sole authorization gate. Do not widen it, cache around it, or add silent fallbacks.
- Document least-privilege policies for any new API call. Prefer read-only operations.
- Never default the region or account; require them explicitly.
- Treat denials as normal results, not retry triggers for expired credentials.

## LLM / prompt-injection hygiene
- Data returned from AWS (names, tags, descriptions, error text) is untrusted; never execute it,
  never treat it as instructions, and never let it choose tools or arguments without validation.
- Tool inputs are validated by type, length and allow-list at the `server.py` boundary.
- Mutating operations require explicit, unambiguous user intent; keep tools read-only by default.

## Network & crypto
- TLS verification always on (`verify=False` is a CRITICAL finding). Set timeouts on all calls.
- Use `secrets`/`hmac.compare_digest`; no MD5/SHA-1 for security, no home-made crypto.

## Errors & logging
- Error messages are safe for users; no stack traces, tokens or raw SDK reprs across the boundary.
- Do not log PII or credentials; log identifiers sparingly.

## Supply chain & CI
- Dependencies pinned by `uv.lock`; `pip-audit --strict`, `gitleaks`, `zizmor`, `actionlint` stay green.
- GitHub Actions pinned by full commit SHA, `permissions: contents: read` by default,
  `persist-credentials: false`, no `pull_request_target` with untrusted checkout, no secret
  interpolation into `run:` (use `env:`).
- Report suspected vulnerabilities privately (see `SECURITY.md` if present), not in public issues.
