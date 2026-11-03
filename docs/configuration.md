# Configuration

All behaviour is driven by one TOML file. It contains **references and settings only, never secrets**.
Loader: `load_config()` in [config.py](../src/aws_sso_mcp/config.py). A fully commented sample is in
[examples/config.example.toml](../examples/config.example.toml).

## Contents

- [Where files live](#where-files-live)
- [Top-level keys](#top-level-keys)
- [Portals (orgs)](#portals-orgs)
- [Environments and role rules](#environments-and-role-rules)
- [Performance tuning](#performance-tuning)
- [Validation rules](#validation-rules)
- [Recipes](#recipes)

## Where files live

| What | Resolution order |
|---|---|
| Config file | explicit path (tests) > `$AWS_SSO_MCP_CONFIG` > `~/.config/aws-sso-mcp/config.toml` |
| State directory | `$AWS_SSO_MCP_STATE_DIR` > `~/.aws_sso/mcp` |

The state directory holds `tokens/<org>.json`, `cache.db` and `audit.jsonl`.

On Windows, `~` is your user profile (for example `C:\Users\you`).

## Top-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `default_org` | string | none | Org made active at session start. Must match a `[portals.<alias>]` |
| `cache_ttl_hours` | number >= 0 | `12` | Age after which an org's account directory is refreshed on next use |
| `log_level` | string | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Portals (orgs)

One table per IAM Identity Center instance. At least one is required.

```toml
[portals.org1]
start_url = "https://your-org1.awsapps.com/start"   # required
region    = "us-east-1"                              # required
```

| Key | Meaning |
|---|---|
| `<alias>` (table name) | The name you use as `org=` and when chatting. It also namespaces token files and cache rows |
| `start_url` | Your Identity Center start URL (the page you visit to sign in) |
| `region` | Region **hosting Identity Center** (its SSO and SSO-OIDC endpoints). **Not** a query region |

> Find the region and URL in the AWS console: IAM Identity Center > Settings. The start URL is also on
> your portal's login page.

## Environments and role rules

Environments let you say "every prod account" and let rules decide the role, deterministically.

```toml
[portals.org1.environments.prod]
match    = "endswith"
patterns = ["-prod", "-production"]
roles    = ["ReadOnlyAccess"]
fallback = ["SecurityAudit"]
```

| Key | Required | Meaning |
|---|---|---|
| table name (`prod`) | yes | Your label. Free-form |
| `match` | no (default `endswith`) | How both `patterns` and role names are compared: `exact`, `startswith`, `endswith`, `contains`, `regex` |
| `patterns` | yes | Account-name patterns. **First environment (in file order) with a matching pattern wins** |
| `roles` | yes | Role names to try first, in order, against roles **actually granted** to you on that account |
| `fallback` | no | Tried only if nothing in `roles` was granted |

### How a role is chosen

Your environment rules are the **only** thing that selects a role:

1. The account name is matched against each environment's `patterns` (first match wins).
2. In that environment, the first entry in `roles` that you are granted is used, else the first in
   `fallback`.
3. If no environment matches, or none of its roles is granted, the account is refused and the error lists
   the roles you do hold.

There is no per-call override and no "default" or "only role granted" shortcut. A prompt, or text
injected through AWS data, cannot choose or escalate a role. Rules can only pick from roles you were already
granted, and IAM remains the authorization gate
([ADR 0007](adr/0007-toml-rules-are-the-only-role-selector.md)).

Every account you want to query must therefore match an environment. Use `explain_role_selection` to see
which rule fires for a specific account.

### Matching examples

| `match` | Pattern | Matches | Does not match |
|---|---|---|---|
| `endswith` | `-prod` | `shop-prod` | `prod-shop` |
| `startswith` | `prod-` | `prod-shop` | `shop-prod` |
| `contains` | `prod` | `shop-prod-eu` | `shop-dev` |
| `exact` | `shop-prod` | `shop-prod` | `shop-prod-2` |
| `regex` | `^prod-.*` | `prod-anything` | `x-prod-y` (uses `re.search`, so anchor with `^`/`$`) |

Note that `match` applies to **both** account patterns and role names. With `match = "regex"`, a role of
`ReadOnlyAccess` matches as a regex search. With `endswith`, `roles = ["ReadOnlyAccess"]` also matches a
granted role named `Custom-ReadOnlyAccess`. Use `exact` if that matters.

**Order matters.** Put specific environments before general ones, for example `prod` before a broad
`contains` rule.

## Performance tuning

Optional `[performance]` table. All values must be integers >= 1 unless stated.

| Key | Default | Meaning | Tune when |
|---|---|---|---|
| `credential_workers` | 10 | Concurrency for SSO calls (`GetRoleCredentials`, `ListAccountRoles`) | Raise carefully; SSO throttles hard |
| `service_workers` | 50 | Concurrency for the real AWS calls | Large fan-outs are slow |
| `per_target_timeout_seconds` | 60 | Active-time budget per account. `0` disables (number) | Slow regions/APIs |
| `progress_min_interval_seconds` | 0.25 | Min gap between progress updates (number) | Chat UI is flooded |
| `max_items_per_account` | 1000 | Pagination ceiling per account per call | Results are `_truncated` |
| `max_accounts` | 200 | Blast-radius cap | You intentionally run wider |

Defaults suit roughly 100 to 500 accounts. Use `perf_stats` to see cache hit rates.

## Validation rules

Enforced at load, with clear `ConfigError` messages:

- File exists and is valid TOML.
- **No key name may contain** `secret`, `password`, `passwd`, `access_key`, `session_token`,
  `private_key`, `api_key` or `token` (checked recursively, case-insensitive).
- At least one `[portals.<alias>]`; each needs string `start_url` and `region`.
- `default_org` (if set) must exist.
- `cache_ttl_hours` must be a non-negative number.
- Environments: valid `match`, non-empty `patterns` and `roles` lists of non-empty strings; with
  `regex`, every pattern must compile.
- `[performance]` values must have the right type and range.

Run `config_validate` from chat to re-check after editing. Restart the server to apply changes.

## Recipes

### Two orgs, read-only everywhere

```toml
default_org = "corp"
cache_ttl_hours = 12

[portals.corp]
start_url = "https://corp.awsapps.com/start"
region = "eu-west-1"

[portals.corp.environments.prod]
match = "endswith"
patterns = ["-prod"]
roles = ["ReadOnlyAccess"]

[portals.corp.environments.nonprod]
match = "regex"
patterns = ["-(dev|test|stage)$"]
roles = ["ReadOnlyAccess", "PowerUserAccess"]

[portals.acquired]
start_url = "https://acquired.awsapps.com/start"
region = "us-east-1"
```

`acquired` has no environments, so every query against it is refused with "no [environments] rule in the
config matches this account name". Add an environment before using it.

### Cautious defaults for a large estate

```toml
[performance]
max_accounts = 50
service_workers = 30
per_target_timeout_seconds = 90
```

### Using a separate config per project

Set `AWS_SSO_MCP_CONFIG` and `AWS_SSO_MCP_STATE_DIR` in the client's `env` block (see
[setup-guide.md](setup-guide.md#3-register-it-with-your-chat-client)).
