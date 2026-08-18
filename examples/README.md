# Running aws-sso-mcp

This folder contains a working example configuration and step-by-step
instructions for running the server locally and wiring it into an MCP
client.

## 1. Install dependencies

From the repo root:

```bash
uv sync
```

## 2. Create your config

Copy the example and edit it with your real org(s):

```bash
mkdir -p ~/.config/aws-sso-mcp
cp examples/config.example.toml ~/.config/aws-sso-mcp/config.toml
```

Open `~/.config/aws-sso-mcp/config.toml` and fill in, per org:

* `start_url` - your IAM Identity Center (SSO) start URL
* `region` - the region hosting that org's SSO/SSO-OIDC endpoint (not a
  default query region - every AWS call always requires its own
  `region=`)
* `[portals.<org>.environments.<env>]` tables - name-pattern -> role
  rules used to deterministically pick which IAM role is assumed per
  account (the model never chooses a role itself)

See the comments in `config.example.toml` for the full shape, including
a two-org example.

The config file resolution order is:

1. An explicit path passed programmatically (mainly for tests)
2. `$AWS_SSO_MCP_CONFIG`
3. `~/.config/aws-sso-mcp/config.toml` (default)

Cache/token state (never secrets in the config itself) lives under
`~/.aws_sso/mcp/` by default, overridable with `$AWS_SSO_MCP_STATE_DIR`.

## 3. Run it standalone (sanity check)

```bash
uv run aws-sso-mcp
```

This starts the server on **stdio** and blocks, waiting for an MCP
client to connect - `Ctrl+C` to stop. This step just proves the config
loads and the binary starts; a real client is needed to call tools.

## 4. Register with an MCP client

Point your client at the `uv run aws-sso-mcp` command, run from the repo
directory. Example client config (VS Code / Claude Desktop style):

```json
{
  "mcpServers": {
    "aws-sso-mcp": {
      "command": "uv",
      "args": ["run", "aws-sso-mcp"],
      "cwd": "/absolute/path/to/aws-sso-mcp"
    }
  }
}
```

If you want to point at a non-default config/state location instead of
`~/.config/aws-sso-mcp/config.toml`, add an `env` block:

```json
{
  "mcpServers": {
    "aws-sso-mcp": {
      "command": "uv",
      "args": ["run", "aws-sso-mcp"],
      "cwd": "/absolute/path/to/aws-sso-mcp",
      "env": {
        "AWS_SSO_MCP_CONFIG": "/absolute/path/to/config.toml",
        "AWS_SSO_MCP_STATE_DIR": "/absolute/path/to/state-dir"
      }
    }
  }
}
```

## 5. Using it in VS Code (GitHub Copilot Chat)

VS Code's MCP support uses a `servers` key (not `mcpServers`) and can be
scoped per-workspace or per-user.

**Workspace-scoped (recommended for this repo):** create
`.vscode/mcp.json` in the project you want to query AWS from:

```json
{
  "servers": {
    "aws-sso-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "aws-sso-mcp"],
      "cwd": "/absolute/path/to/aws-sso-mcp"
    }
  }
}
```

**User-scoped (available in every workspace):** run
**Command Palette -> "MCP: Open User Configuration"** and add the same
`aws-sso-mcp` entry under `servers`, or run
**Command Palette -> "MCP: Add Server..."** and choose "Command (stdio)",
then fill in `uv` / `run aws-sso-mcp` / the repo path when prompted.

If your config/state live somewhere other than the defaults, add an
`env` block exactly as in step 4:

```json
{
  "servers": {
    "aws-sso-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "aws-sso-mcp"],
      "cwd": "/absolute/path/to/aws-sso-mcp",
      "env": {
        "AWS_SSO_MCP_CONFIG": "/absolute/path/to/config.toml",
        "AWS_SSO_MCP_STATE_DIR": "/absolute/path/to/state-dir"
      }
    }
  }
}
```

After saving `mcp.json`, VS Code shows an inline **Start** action above
the `aws-sso-mcp` entry - click it (or reload the window) to launch the
server. Once running:

* Open the Copilot Chat panel and switch to **Agent mode** (tools are
  only available in Agent mode, not Ask/Edit mode).
* Click the **tools icon** in the chat input to confirm `aws-sso-mcp`'s
  tools (`aws_query`, `run_dag`, `login`, `use_org`, ...) are listed and
  enabled.
* Chat naturally, e.g. *"login to org1"* then *"list stopped EC2
  instances across every prod account"* - Copilot will call the tools
  for you and prompt for confirmation before each tool run (VS Code asks
  per-tool/per-session approval by default).
* Use **Command Palette -> "MCP: Show Output"** / the "Show Output"
  action on the server entry to view server logs if something looks
  wrong (e.g. a config error surfaces there immediately on start).

## 6. First use, in chat

1. `login to org1` (or whichever alias you used) - opens a browser for
   SSO device authorization once; the token is cached until it expires.
2. `what orgs are configured`, `switch to org2`, `is my token still valid`
   - session/org management.
3. `find accounts with -prod in the name`, `list my roles on account
   123456789012` - account/role lookups (auto-refreshes the cached
   account directory on a 12h threshold, or force it with
   `refresh_accounts`).
4. Natural-language AWS questions, e.g. *"list all stopped EC2 instances
   across every prod account in org1"* - the model translates this into
   the generic `aws_query` tool call; account/role selection is always
   deterministic, never chosen by the model itself. Multi-step questions
   use `run_dag` to chain calls per account.

Only read-only AWS calls (`Describe*`/`List*`/`Get*`/`Head*` plus a small
explicit allowlist) are ever permitted - a mutating call is rejected
immediately, before any network activity.

## 7. Verify it end-to-end without a full client (optional)

A minimal Python MCP client smoke test, useful when debugging:

```bash
uv run python - <<'PY'
import asyncio, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = dict(os.environ)
    env["AWS_SSO_MCP_CONFIG"] = os.path.expanduser("~/.config/aws-sso-mcp/config.toml")
    params = StdioServerParameters(command="uv", args=["run", "aws-sso-mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", sorted(t.name for t in tools.tools))
            result = await session.call_tool("orgs_list", {})
            print(result.content[0].text)

asyncio.run(main())
PY
```
