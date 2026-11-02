# Worked examples

Each example shows the same thing three ways: what you **say**, the **tool call** the assistant
makes, and the **response** you get back. Use it to learn what a sentence actually does, to debug a
surprising result, or to call the tools directly from your own code.

Account ids here are placeholders. Replace them with your own.

## Contents

- [How to read these](#how-to-read-these)
- [1. Log in and look around](#1-log-in-and-look-around)
- [2. Find accounts](#2-find-accounts)
- [3. Check which role will be used](#3-check-which-role-will-be-used)
- [4. One account, one call](#4-one-account-one-call)
- [5. Every account in an environment](#5-every-account-in-an-environment)
- [6. Narrowing with several selectors](#6-narrowing-with-several-selectors)
- [7. Across several orgs](#7-across-several-orgs)
- [8. Searching every org's directory](#8-searching-every-orgs-directory)
- [9. Passing AWS filters and pagination limits](#9-passing-aws-filters-and-pagination-limits)
- [10. Multi-step: a DAG](#10-multi-step-a-dag)
- [11. Forcing a specific role](#11-forcing-a-specific-role)
- [12. Making a change](#12-making-a-change)
- [13. Reading a partial failure](#13-reading-a-partial-failure)
- [14. Diagnostics](#14-diagnostics)

## How to read these

- **You say** - plain English, typed into your chat client in agent mode.
- **Tool call** - the JSON arguments the assistant sends. You never type this, but seeing it tells
  you exactly what happened.
- **Response** - trimmed for readability. The full shapes are in
  [tools-reference.md](tools-reference.md#response-shapes).

`service` and `operation` follow the AWS API, exactly as botocore names them: `ec2` /
`DescribeInstances`, `s3` / `ListBuckets`, `rds` / `DescribeDBInstances`. `params` is the API's own
request shape.

## 1. Log in and look around

**You say:** *What orgs are configured?*

```json
{ "tool": "orgs_list", "arguments": {} }
```

```json
{
  "active_org": "corp",
  "orgs": [
    { "name": "corp",    "start_url": "https://corp.awsapps.com/start",    "sso_region": "us-east-1" },
    { "name": "sandbox", "start_url": "https://sandbox.awsapps.com/start", "sso_region": "eu-west-1" }
  ]
}
```

**You say:** *Log in to corp.* - the only tool that opens a browser.

```json
{ "tool": "login", "arguments": { "org": "corp" } }
```

```json
{ "org": "corp", "valid": true, "expires_at": "2026-11-03T02:14:09Z" }
```

**You say:** *Is my token still valid?* - checks without logging in, never opens a browser.

```json
{ "tool": "token_status", "arguments": { "org": "corp" } }
```

```json
{ "org": "corp", "valid": false, "expires_at": "2026-11-02T18:30:00Z", "reason": "expiring within 5 minutes" }
```

## 2. Find accounts

**You say:** *Find accounts with "payments" in the name.*

```json
{ "tool": "search_accounts", "arguments": { "name_contains": "payments" } }
```

```json
{
  "matches": [
    { "org": "corp", "account_id": "111111111111", "account_name": "payments-dev" },
    { "org": "corp", "account_id": "222222222222", "account_name": "payments-prod" }
  ],
  "truncated": false
}
```

The substring is case-insensitive and behaves **identically** to the `name_contains` selector on a
query, so what you see here is exactly what a query would target.

## 3. Check which role will be used

Do this before a wide or mutating run. It makes no AWS service call.

**You say:** *Which role would you use for payments-prod, and why?*

```json
{
  "tool": "explain_role_selection",
  "arguments": { "account_id": "222222222222", "account_name": "payments-prod" }
}
```

```json
{
  "resolved": true,
  "role": "ReadOnlyAccess",
  "source": "environment",
  "environment": "prod",
  "granted_roles": ["ReadOnlyAccess", "SecurityAudit"]
}
```

`source` tells you *why*: an explicit override you named, an `environment` rule from your TOML, or
the sole role granted on the account.

## 4. One account, one call

**You say:** *List EC2 instances in us-east-1 in account payments-prod.*

```json
{
  "tool": "aws_query",
  "arguments": {
    "service": "ec2",
    "operation": "DescribeInstances",
    "region": "us-east-1",
    "account_name": "payments-prod"
  }
}
```

```json
{
  "results": [
    {
      "org": "corp",
      "account_id": "222222222222",
      "account_name": "payments-prod",
      "role": "ReadOnlyAccess",
      "ok": true,
      "data": { "Reservations": [ "..." ] }
    }
  ],
  "target_count": 1,
  "ok_count": 1,
  "failed_count": 0
}
```

**The region is mandatory.** There is no default, and it is never inferred from your config - the
`region` in a `[portals.*]` table is where Identity Center lives, not where you query.

## 5. Every account in an environment

**You say:** *List stopped EC2 instances in us-east-1 across every prod account.*

```json
{
  "tool": "aws_query",
  "arguments": {
    "service": "ec2",
    "operation": "DescribeInstances",
    "region": "us-east-1",
    "environment": "prod",
    "params": {
      "Filters": [{ "Name": "instance-state-name", "Values": ["stopped"] }]
    }
  }
}
```

`environment: "prod"` resolves through the `[portals.corp.environments.prod]` rules in your config -
both *which accounts* match and *which role* is assumed on each.

Results come back **sorted**, and one account failing never aborts the rest.

## 6. Narrowing with several selectors

Selectors combine with **AND**. Exclusions are applied last and always win.

**You say:** *In every account with "payments" in the name except payments-sandbox, list S3 buckets.*

```json
{
  "tool": "aws_query",
  "arguments": {
    "service": "s3",
    "operation": "ListBuckets",
    "region": "us-east-1",
    "name_contains": "payments",
    "exclude_account_names": ["payments-sandbox"]
  }
}
```

With **no** selector at all, every account in the active org is targeted - subject to the
`max_accounts` cap (default 200).

## 7. Across several orgs

Cross-org is an **explicit opt-in**. A query never widens beyond the active org by itself.

**You say:** *Across corp and sandbox, list running RDS instances in eu-west-1.*

```json
{
  "tool": "aws_query",
  "arguments": {
    "service": "rds",
    "operation": "DescribeDBInstances",
    "region": "eu-west-1",
    "orgs": ["corp", "sandbox"]
  }
}
```

```json
{
  "results": [
    { "org": "corp",    "account_name": "payments-prod", "ok": true,  "role": "ReadOnlyAccess",
      "data": { "DBInstances": [ "..." ] } },
    { "org": "sandbox", "account_name": "sbx-data",      "ok": false,
      "error": "No valid SSO session for: sandbox (expired)" }
  ],
  "target_count": 2,
  "ok_count": 1,
  "failed_count": 1
}
```

Notes that matter:

- You must be **logged in to every org** in the list. They are separate Identity Center instances;
  a missing session shows up as a per-account error, not a crash.
- `orgs` combines with the other selectors, so `orgs: ["corp", "sandbox"]` plus
  `environment: "prod"` means *prod accounts in either org*.
- An exact `account_name` must be unambiguous across the orgs in scope. If both orgs contain a
  `logging` account, use `account_ids` instead.

## 8. Searching every org's directory

**You say:** *Search all orgs for accounts named like "logging".*

```json
{
  "tool": "search_accounts",
  "arguments": { "name_contains": "logging", "all_orgs": true }
}
```

Same flag on `refresh_accounts`, when you want to rebuild every org's cache after a grant change:

```json
{ "tool": "refresh_accounts", "arguments": { "all_orgs": true } }
```

## 9. Passing AWS filters and pagination limits

`params` is passed straight to the AWS API, so anything the API accepts works.

**You say:** *Show me EC2 instances tagged Environment=production in us-west-2 in every prod
account, up to 5000 per account.*

```json
{
  "tool": "aws_query",
  "arguments": {
    "service": "ec2",
    "operation": "DescribeInstances",
    "region": "us-west-2",
    "environment": "prod",
    "params": {
      "Filters": [{ "Name": "tag:Environment", "Values": ["production"] }]
    },
    "max_items": 5000
  }
}
```

| Knob | Default | What it protects |
|---|---|---|
| `max_items` | 1000 per account | Your context window, against one huge account |
| `max_accounts` | 200 | Blast radius, against a mistyped selector |

Both can be raised per call; the defaults live in the `[performance]` table of your config. When a
result is cut short you get an explicit truncation marker rather than silently missing rows.

## 10. Multi-step: a DAG

When step two needs step one's output, describe the *chain* and `run_dag` is used.

**You say:** *In every prod account in us-east-1, find stopped EC2 instances, then show the EBS
volumes attached to them.*

```json
{
  "tool": "run_dag",
  "arguments": {
    "region": "us-east-1",
    "environment": "prod",
    "steps": [
      {
        "id": "stopped",
        "service": "ec2",
        "operation": "DescribeInstances",
        "params": { "Filters": [{ "Name": "instance-state-name", "Values": ["stopped"] }] }
      },
      {
        "id": "volumes",
        "service": "ec2",
        "operation": "DescribeVolumes",
        "depends_on": ["stopped"]
      }
    ]
  }
}
```

The DAG runs **per account**, so step two only ever sees step one's output from the same account.

If the plan comes back malformed you get `dag_error`. Ask to see it first:

> *Show me the DAG you would run, but do not run it yet.*

## 11. Forcing a specific role

**You say:** *Using the SecurityAudit role, list IAM users in every prod account.*

```json
{
  "tool": "aws_query",
  "arguments": {
    "service": "iam",
    "operation": "ListUsers",
    "region": "us-east-1",
    "environment": "prod",
    "role": "SecurityAudit"
  }
}
```

`role` overrides the TOML rules, but it cannot invent access: the role must actually be granted to
you on every selected account, or those accounts come back as errors.

## 12. Making a change

There is no local allow-list. Anything your role permits is dispatched - **IAM is the only gate**.
Be deliberate.

**You say:** *Stop instance i-0abc123 in us-east-1 in account payments-dev.*

```json
{
  "tool": "aws_query",
  "arguments": {
    "service": "ec2",
    "operation": "StopInstances",
    "region": "us-east-1",
    "account_name": "payments-dev",
    "params": { "InstanceIds": ["i-0abc123"] }
  }
}
```

Good habits:

1. Name the exact account. Never use `environment` or an empty selector for a mutation.
2. Run the read-only equivalent first (`DescribeInstances`) and check the result.
3. Expect your client to ask for confirmation - leave that prompt enabled.

Every attempt is appended to `audit.jsonl`, with mutating calls flagged `"write": true`.

## 13. Reading a partial failure

A wide query usually returns a mix. That is normal, not an error.

```json
{
  "results": [
    { "account_name": "payments-prod", "ok": true,  "role": "ReadOnlyAccess",
      "data": { "Reservations": [] } },
    { "account_name": "payments-dev",  "ok": false, "role": "ReadOnlyAccess",
      "error": "AccessDenied: not authorized to perform ec2:DescribeInstances" },
    { "account_name": "legacy-prod",   "ok": false,
      "error": "no granted role matched the rules for environment 'prod'" }
  ],
  "target_count": 3,
  "ok_count": 1,
  "failed_count": 2
}
```

| Error | What it means | What to do |
|---|---|---|
| `AccessDenied` | Your role is real but lacks that API permission | Ask your AWS admin, or pick another role |
| No role matched | The account's granted roles do not match your `[environments]` rules | Add a `fallback`, or run `list_roles` on it |
| `No valid SSO session` | Not logged in to that org, or the token lapsed | Log in to that org |
| Timed out | The account exceeded `per_target_timeout_seconds` | Narrow the query, or raise the timeout |

## 14. Diagnostics

| You say | Tool | Tells you |
|---|---|---|
| *Show my configuration.* | `config_show` | The resolved config (it contains no secrets by design) |
| *Is my config valid?* | `config_validate` | Parse and validation result |
| *How fresh is the account cache?* | `cache_status` | Per-org account count, last sync, staleness |
| *Refresh the account list.* | `refresh_accounts` | Forces a rebuild, ignoring the TTL |
| *Show performance stats.* | `perf_stats` | Cache hit rates and credential broker counters |

See [tools-reference.md](tools-reference.md) for every argument of every tool, and
[prompt-guide.md](prompt-guide.md) for more ways to phrase a request.
