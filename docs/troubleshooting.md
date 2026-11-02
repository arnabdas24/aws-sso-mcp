# Troubleshooting

Find your symptom, read the cause, apply the fix. Error text is quoted from the code, so you can search
this page for the message you see.

## Contents

- [Quick diagnostic checklist](#quick-diagnostic-checklist)
- [Startup and configuration](#startup-and-configuration)
- [Signing in](#signing-in)
- [Account selection](#account-selection)
- [Roles](#roles)
- [Query results](#query-results)
- [Performance](#performance)
- [Where to look for logs](#where-to-look-for-logs)

## Quick diagnostic checklist

Ask your assistant, in order:

1. `Check that my config file is valid.` (`config_validate`)
2. `Show the status of org1.` (`org_status`): is the token valid? is the cache stale?
3. `Run sts GetCallerIdentity in us-east-1 on account <id>.`: proves credentials work end to end.
4. `Which role would be used for account <name> (<id>)?` (`explain_role_selection`)
5. `Show performance stats.` (`perf_stats`)

## Startup and configuration

| Message or symptom | Cause | Fix |
|---|---|---|
| `Config file not found at ...` | No file at the resolved path | Create it from `examples/config.example.toml`, or set `AWS_SSO_MCP_CONFIG` |
| `... is not valid TOML` | Syntax error | Check quotes and brackets; the message includes the parser's detail |
| `key '...' looks like it may hold a secret` | A key name contains `token`, `secret`, `password`, ... | Rename it. Secrets must never be in this file |
| `[portals.X] is missing a required 'start_url' string` / `'region'` | Incomplete portal | Add the key |
| `default_org='x' does not match any configured [portals.*]` | Typo | Fix the name |
| `invalid regex ...` | A pattern fails to compile when `match = "regex"` | Fix the pattern |
| Server shows as failed to start in the client | Config error or `aws-sso-mcp` not on `PATH` | Run `aws-sso-mcp --version` in a terminal; check the client's MCP output panel |
| Config edits have no effect | Config is loaded once per process | Restart the MCP server in your client |
| The chat client reports protocol or JSON errors | Something wrote to **stdout** | Never `print()` in server code. Use `logger` (stderr) |

## Signing in

| Message or symptom | Cause | Fix |
|---|---|---|
| `No valid SSO session for: org1 (no cached token)` | Never logged in | `Log in to org1.` |
| `... (expired)` | SSO session lifetime passed | Log in again |
| `... (expiring within 5 minutes)` | Token is still valid but too close to expiry to survive a fan-out | Log in again |
| `... (cached token corrupted)` | Token file unreadable | Log in again (rewrites it) |
| `No active org and none specified. Call use_org(org) first...` | No `default_org`, no `org=` | `Switch to org1.` or pass an org |
| `Unknown org 'x'. Configured orgs: ...` | Alias typo | Use `orgs_list` |
| Browser did not open | Headless or restricted host | The URL and user code are logged; open the URL manually and enter the code |
| `The device authorization code expired before the sign-in was approved` | You waited too long | Call `login` again and approve promptly |
| `Timed out waiting for the SSO sign-in to be approved` | Same, deadline reached | Call `login` again |
| `sso-oidc ... device_authorization failed (...): Login failed.` | Wrong `region` or `start_url`, or network | Verify the Identity Center region and URL in the console; check connectivity or proxy |
| A query never opens a browser | **By design** | Run `login` explicitly |

## Account selection

| Message or symptom | Cause | Fix |
|---|---|---|
| `No accounts matched the given selectors.` (warning) | Filter too narrow, or stale cache | Try `search_accounts`; run `refresh_accounts` if accounts are new |
| `No account matched name(s)=... id(s)=...` | An explicit name or id did not exist in scope | Check spelling, the org, or refresh the cache |
| `Account name '...' is ambiguous - found in multiple places` | Same name in several orgs | Add `org=` or use `account_id=` |
| `This selection resolves to N accounts, which exceeds the blast-radius limit of M` | Cap protects you | Narrow it, or pass `max_accounts=N` deliberately |
| `environment` matches nothing | No environment defined, or patterns do not match | Check `[portals.<org>.environments.*]` and the `match` mode |
| New account missing | Cache older than TTL has not refreshed yet | `refresh_accounts` |

## Roles

| Message or symptom | Cause | Fix |
|---|---|---|
| `Could not resolve a role ... no environment mapping matched and multiple roles are granted` | Several roles, no rule | Add an environment rule or pass `role=` |
| `... classified as environment 'prod' but no configured role or fallback pattern matched a granted role` | Rule's `roles` / `fallback` do not appear in your grants | Run `list_roles`; align the rule (also check `match` mode) |
| `... requested role 'X' is not granted on this account` | You forced a role you do not have there | `list_roles` for that account |
| `... no roles are granted to you on this account` | No assignment in Identity Center | Ask your admin |
| `sso GetRoleCredentials failed (...)` | Role revoked, token expired, or throttled | Retry; `list_roles force_refresh=true`; log in again |
| A role was just granted but is not used | Role list cached | `list_roles` with `force_refresh`, or `refresh_accounts` |

## Query results

| Message or symptom | Cause | Fix |
|---|---|---|
| `... failed (AccessDenied...)` or `UnauthorizedOperation` for some accounts | The role lacks permission (IAM is the only gate). The server already retried once with fresh roles | Use a role with the needed permission via `role=`, or accept it |
| `UnknownOperation`: `<service> has no operation named '<op>'` | Wrong casing or name | Use exact PascalCase from the AWS API reference (`DescribeInstances`, not `describe_instances`) |
| `Parameter validation failed` (from botocore) | `params` do not match the operation's model | Check parameter names and types in the AWS docs |
| `A region is required for every AWS call` | `region` empty | Provide one |
| `... failed (InvalidClientTokenId ...)` or `AuthFailure` in a disabled region | Opt-in region not enabled in that account | Choose another region |
| `exceeded the 60s per-account budget` | Slow API or throttling | Raise `per_target_timeout_seconds` (or `0`), or narrow the query |
| `_truncated: Stopped after N items ...` | Pagination ceiling | Add filters, or pass a larger `max_items` |
| `...[truncated]` in a field | A value exceeded 4000 chars | Ask for specific fields |
| `...[N more items truncated]` | A list exceeded 500 items | Filter server-side with `params` |
| `***REDACTED***` appears where you wanted a value | The value looked like a secret | By design. Ask for metadata instead |
| `dag_error: step ... references unknown step(s)` | Typo in `$ref` or `depends_on` | Fix the id |
| `dag_error: dependency cycle detected` | Steps depend on each other | Remove the loop |
| `step N is missing required key(s)` | Step lacks `id`, `service`, or `operation` | Add them |
| `Unexpected <Type>: ...` with a hint about a bug | Internal failure | Check the server log for the traceback; please file an issue |

## Performance

| Symptom | Likely cause | What to try |
|---|---|---|
| First query of the day is slow | Directory refresh (and role lookups) after TTL | Expected once per TTL; run `refresh_accounts` ahead of time |
| Every query is slow | Low `service_workers`, slow regions, or many pages | Raise `service_workers`; narrow selectors; check `perf_stats` |
| Many throttling errors | `credential_workers` too high | Lower it |
| `role_misses` high in `perf_stats` | TTL too short or role churn | Increase `cache_ttl_hours` |
| `broker.misses` high | Short sessions or role changes | Normal at the start of a session |
| Progress stalls | A few slow accounts | They are bounded by the per-account timeout |

## Where to look for logs

- **VS Code:** Command Palette > `MCP: Show Output`, or the "Show Output" action on the server entry.
- **Other clients:** their MCP log panel or log file; the server writes to **stderr**.
- **Chat UI:** INFO and above messages from a running tool are forwarded to the chat.
- **Audit trail:** `<state_dir>/audit.jsonl`, one JSON line per AWS call.
- **Verbose logging:** set `log_level = "DEBUG"` in the config and restart. Parameter *names* (never values)
  are logged per call.

When reporting a bug, include the tool you called, the (redacted) error, and the relevant server log
lines. Never paste tokens or credentials.
