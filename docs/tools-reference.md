# Tools reference

Every MCP tool exposed by `aws-sso-mcp`, defined in [server.py](../src/aws_sso_mcp/server.py).
All tools return a JSON object. On failure, the object is `{"error": "..."}` (and sometimes a `hint`).

## Contents

- [Common concepts](#common-concepts)
- [Session tools](#session-tools)
- [Directory tools](#directory-tools)
- [Query tools](#query-tools)
- [Diagnostics tools](#diagnostics-tools)
- [Response shapes](#response-shapes)

## Common concepts

### `org` resolution

When a tool takes an optional `org`, it resolves as: explicit `org=` > the **active org** (from `use_org` or
`default_org`) > error asking you to call `use_org`.

### Account selectors (shared by `aws_query` and `run_dag`)

All optional; combined with **AND**; exclusions applied last.

| Argument | Type | Meaning |
|---|---|---|
| `org` | string | Restrict to one org alias |
| `orgs` | list | Several orgs; the explicit opt-in to cross-org |
| `account_name` | string | Exact name; must be unambiguous across orgs in scope |
| `account_names` | list | Several exact names |
| `account_id` | string | Exact 12-digit id |
| `account_ids` | list | Several ids |
| `name_contains` | string | Case-insensitive substring of the name |
| `environment` | string | Accounts your TOML classifies into this environment |
| `exclude_account_ids` | list | Dropped even if matched |
| `exclude_account_names` | list | Dropped even if matched |
| `max_accounts` | int | Raise the blast-radius cap for this call |
| `max_items` | int | Raise the per-account pagination ceiling |

With no selector at all, **every account in the active org** is targeted, subject to `max_accounts`.

There is **no `role` argument**. The role comes only from your TOML environment rules; a client that sends
one anyway has it ignored. The `role` field in each result shows what was used.

---

## Session tools

| Tool | Arguments | Returns | Notes |
|---|---|---|---|
| `orgs_list` | none | `active_org`, `orgs[]` (`name`, `start_url`, `sso_region`) | |
| `use_org` | `org` | `active_org`, `token_valid`, `token_expires_at`, `cached_accounts`, `last_full_sync` | Does not log in |
| `current_org` | none | `active_org` | |
| `org_status` | `org?` | `org`, `token_valid`, `token_expires_at`, `token_reason`, `cached_accounts`, `last_full_sync`, `cache_stale` | |
| `token_status` | `org?` | `org`, `valid`, `expires_at`, `reason` | **Never opens a browser** |
| `login` | `org?` | `org`, `valid`, `expires_at` | **The only tool that opens a browser**; blocks until approval or code expiry |

## Directory tools

| Tool | Arguments | Returns | Notes |
|---|---|---|---|
| `refresh_accounts` | `org?`, `all_orgs=false` | `refreshed[]` (`org`, `account_count`, `last_full_sync`) | Forces refresh, ignoring TTL. Needs a valid token |
| `cache_status` | `org?` | `orgs[]` (`org`, `account_count`, `last_full_sync`, `stale`) | Local only |
| `search_accounts` | `name?`, `name_contains?`, `account_id?`, `org?`, `all_orgs=false`, `limit=200` | `matches[]` (`org`, `account_id`, `account_name`), `truncated`, optional `warning` | Auto-refreshes stale caches. Same substring semantics as query selectors |
| `list_roles` | `account_id`, `org?`, `force_refresh=false` | `org`, `account_id`, `roles[]` | Cached; use `force_refresh` after a grant change |
| `explain_role_selection` | `account_id`, `account_name`, `org?` | `resolved`, `role`, `source`, `environment`, `granted_roles` (or `reason`) | Dry run; no service call |

## Query tools

### `aws_query`

Run **one** AWS API call across every selected account.

| Argument | Required | Description |
|---|---|---|
| `service` | yes | boto3 service name: `ec2`, `s3`, `iam`, `rds`, ... |
| `operation` | yes | PascalCase operation: `DescribeInstances`. Exactly as in the AWS API reference |
| `region` | yes | Never defaulted |
| `params` | no | Dict of operation parameters, validated by botocore |
| selectors | no | See [above](#account-selectors-shared-by-aws_query-and-run_dag) |

Behaviour:

- Paginates automatically up to `max_items` (default `performance.max_items_per_account`, 1000).
  Truncation adds `_truncated` to that account's data.
- Reads **and** writes are dispatched; IAM decides.
- Unknown operations return an `UnknownOperation` error for that account.

Example:

```json
{
  "service": "ec2",
  "operation": "DescribeInstances",
  "region": "us-east-1",
  "environment": "prod",
  "params": {"Filters": [{"Name": "instance-state-name", "Values": ["stopped"]}]}
}
```

### `run_dag`

Run a small multi-step plan, independently per account.

| Argument | Required | Description |
|---|---|---|
| `steps` | yes | List of step objects |
| `region` | yes | Used for every step |
| selectors | no | As above |

**Step object**

| Key | Required | Description |
|---|---|---|
| `id` | yes | Unique within the DAG |
| `service` | yes | boto3 service name |
| `operation` | yes | PascalCase operation |
| `params` | no | May contain `{"$ref": "<step_id>"}` wherever a literal could go |
| `extract` | no | Path applied to this step's response; the result is what `$ref` yields. `.` walks dict keys; trailing `[]` flattens a list. Example: `Reservations[].Instances[].InstanceId` |
| `depends_on` | no | Extra explicit dependencies (in addition to those implied by `$ref`) |

Rules:

- Steps run in dependency order (topological). Unknown references or cycles produce `dag_error`.
- A `$ref` object must be exactly `{"$ref": "id"}` (no extra keys) to be substituted.
- No expressions, no code evaluation.
- If no `extract` is given, the whole response is what a `$ref` yields.

Per-account result `data`:

```json
{"steps": {"<id>": {"response": {...}, "extracted": ...}}}
```

## Diagnostics tools

| Tool | Returns |
|---|---|
| `config_show` | `source_path`, `default_org`, `cache_ttl_hours`, `log_level`, `performance{}`, `orgs{}` (no secrets exist in config) |
| `config_validate` | Re-reads the file and reports `valid`, `source_path`, `orgs`; does **not** apply changes to the running server |
| `perf_stats` | `executor` pool sizes, `broker` (`hits`, `misses`, `cached_sessions`), `accounts_cache` (`role_hits`, `role_misses`) |

> A running server does not reload config on its own. After editing `config.toml`, restart the server in
> your client. `config_validate` only checks that the new file is valid.

## Response shapes

### Fan-out response (`aws_query`, `run_dag`)

```json
{
  "target_count": 3,
  "ok_count": 2,
  "failed_count": 1,
  "results": [
    {"org": "org1", "account_id": "111122223333", "account_name": "a-prod",
     "ok": true, "role": "ReadOnlyAccess", "data": { }},
    {"org": "org1", "account_id": "444455556666", "account_name": "b-prod",
     "ok": false, "role": "ReadOnlyAccess", "error": "ec2:DescribeInstances failed (...): ..."}
  ]
}
```

- `results` is sorted by `(org, account_name, account_id)`.
- `role` is absent if the account failed before a role was chosen.
- Empty selection: `target_count: 0` plus `warning: "No accounts matched the given selectors."`

### Error shape

```json
{"error": "No valid SSO session for: org1 (no cached token). Run login(org=\"org1\") to sign in, then retry."}
```

Unexpected internal errors add: `"hint": "This is a bug in aws-sso-mcp; the server log has the full traceback."`

### Output limits

| Limit | Value | Marker |
|---|---|---|
| Max string length | 4000 chars | `...[truncated]` |
| Max list length | 500 items | `...[N more items truncated]` |
| Pagination ceiling | `max_items` | `_truncated` key |
