# Example files

Working configuration you can copy, plus an end-to-end protocol smoke test.

| File | What it is |
|---|---|
| [config.example.toml](config.example.toml) | Annotated two-org configuration. Copy it to `~/.config/aws-sso-mcp/config.toml` and edit. |

**Installing the server and connecting it to a chat client is covered in
[docs/setup-guide.md](../docs/setup-guide.md)** - VS Code Copilot, Claude Desktop, Claude Code,
Cursor, Windsurf, config and state locations, and the first login.

Every key in the config file is documented in
[docs/configuration.md](../docs/configuration.md).

## Protocol smoke test

Useful when a chat client reports a failure with no detail. This talks MCP to the server directly,
with no client involved, and prints the tool list plus the result of one call:

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

If that prints a tool list, the server and your config are fine and the problem is in the client
registration - go back to [docs/setup-guide.md](../docs/setup-guide.md#3-register-it-with-your-chat-client).
