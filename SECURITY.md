# Security Policy

`aws-sso-mcp` brokers short-lived AWS credentials on a user's workstation and is driven by an LLM.
Security reports are taken seriously; please read the reporting rules before filing anything public.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately through GitHub Security Advisories:
[**Report a vulnerability**](https://github.com/arnabdas24/aws-sso-mcp/security/advisories/new).
If that is unavailable, email <arnabds991@gmail.com> with `SECURITY` in the subject.

Please include: affected version or commit, environment (OS, Python version), reproduction steps or
a proof of concept, and the impact you believe it has. **Never include real AWS account IDs,
access keys, SSO tokens or customer data** - redact them, as the maintainer cannot un-see them.

| Stage | Target |
| --- | --- |
| Acknowledgement | 3 business days |
| Initial assessment and severity | 10 business days |
| Fix or documented mitigation | 90 days, sooner for actively exploited issues |

This is a volunteer-maintained project, so these are good-faith targets, not contractual SLAs.
Coordinated disclosure is appreciated; credit is given in the advisory unless you prefer otherwise.

## Supported versions

The project is pre-1.0 (`Development Status :: 3 - Alpha`). Only the latest released version on
PyPI receives security fixes; there are no maintained back-ports.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Anything older | No - upgrade first |

## Threat model

The server runs locally, holds AWS credentials in memory, and consumes untrusted input from two
directions: the LLM/user on one side, and AWS API responses on the other.

**In scope** - please report these:

- Credential or SSO-token disclosure: leakage into logs, tool output, model context, error
  messages, or files readable by other local users.
- Failure of the redaction layer (`src/aws_sso_mcp/redact.py`) on a real secret format.
- Bypassing IAM as the authorization gate, or privilege escalation beyond the user's own role.
- Injection: SQL, command, or path traversal reachable from tool input or AWS response data.
- Prompt injection via AWS data (account names, tags, descriptions, error text) that causes the
  server to take an action the user did not request.
- Token cache files created with permissions broader than owner-only on POSIX.
- Supply-chain issues: dependency confusion, a malicious or compromised pinned dependency, or
  workflow misconfiguration that could leak `GITHUB_TOKEN` or PyPI publishing rights.

**Out of scope:**

- An LLM calling a legitimate read-only tool you did not expect. IAM is the authorization
  boundary; the model is not a trust boundary.
- Anything requiring an attacker who already has local code execution as your user, since they
  can read the AWS CLI cache anyway.
- Results from AWS APIs that your own IAM policy permits.
- Missing POSIX permission bits on Windows, which ignores them; protection there relies on the
  inherited ACL of the state directory, the same as the AWS CLI's SSO cache.
- Vulnerabilities in AWS services themselves - report those to
  [AWS security](https://aws.amazon.com/security/vulnerability-reporting/).

## Security design

The controls a report should try to defeat:

| Control | Where |
| --- | --- |
| IAM is the sole authorization gate; the server adds no allow/deny logic | `engine/catalog.py` |
| The role is chosen only by the user's TOML rules; no model or caller override, no default role | `roles.py` |
| Role credentials live in memory only and are never written to disk | `sso/broker.py` |
| SSO token cache is created `0600` and replaced atomically | `sso/device_auth.py` |
| Tokens are refused once inside a 5-minute expiry margin | `sso/device_auth.py` |
| Every outbound string is redacted before truncation | `redact.py`, `hardening.py` |
| Only `login()` may open a browser | `server.py` |
| Region is always explicit, never defaulted | `server.py`, `awscfg.py` |
| Errors cross the tool boundary as clean `McpError`s, never raw SDK reprs | `errors.py` |
| stdout is reserved for the MCP protocol; logs go to stderr | `logging_setup.py` |

Redaction is covered by property-based tests (`tests/test_properties.py`) that assert no known
secret shape survives at any position, nesting depth or truncation point.

## Verifying a release

Releases are built by GitHub Actions via PyPI Trusted Publishing (OIDC); no long-lived PyPI token
exists. Every artifact carries a signed SLSA build-provenance attestation and a CycloneDX SBOM:

```bash
gh attestation verify ./aws_sso_mcp-<version>-py3-none-any.whl --repo arnabdas24/aws-sso-mcp
```

## Automated security gates

`.github/workflows/security.yml` runs on every pull request and weekly: `gitleaks` (secret
scanning), `pip-audit --strict` (known CVEs in pinned dependencies), and `zizmor` plus `actionlint`
(workflow misconfiguration). All GitHub Actions are pinned by full commit SHA, and Dependabot keeps
them current.
