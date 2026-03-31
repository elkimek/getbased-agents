# getbased MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes blood work data from [getbased](https://getbased.health) as tools. Works with any MCP-compatible client (Claude Code, Hermes, Claude Desktop, etc.).

## How it works

```
getbased (browser)
  ├── your data, your mnemonic
  ├── generates a read-only token
  └── pushes lab context to gateway on every save

Context Gateway (sync.getbased.health/api/context)
  └── stores context text behind token auth

This MCP Server (on your machine)
  ├── fetches context with the token
  └── exposes it as tools to any MCP client
```

Your mnemonic never leaves your browser. The MCP server receives the same lab context text the getbased AI chat uses — not raw data.

## Tools

| Tool | Description |
|---|---|
| `getbased_lab_context` | Full lab summary with biomarkers, context cards, supplements, goals |
| `getbased_section` | Get a specific section (e.g. hormones, lipids) or list all available sections |
| `getbased_list_profiles` | List available profiles |

### getbased_section

Query-aware context: pull just the section you need instead of the full dump. Saves tokens and allows deeper analysis of specific areas.

```
# No args — returns section index with names, updated dates, and line counts
getbased_section()

# With section name — returns just that section's content
getbased_section(section="hormones")
```

Section names are matched by prefix, so `hormones` matches `hormones updated:2026-03-13`.

## Setup

### 1. Enable messenger access in getbased

Go to **Settings > Data > Messenger Access** and toggle it on. Copy the read-only token.

### 2. Configure your MCP client

#### Claude Code / Claude Desktop

Add to your MCP config (`~/.claude/claude_desktop_config.json` or similar):

```json
{
  "mcpServers": {
    "getbased": {
      "command": "python3",
      "args": ["/path/to/getbased_mcp.py"],
      "env": {
        "GETBASED_TOKEN": "your-token-here"
      }
    }
  }
}
```

#### Hermes Agent

```bash
hermes mcp add getbased \
  --command python3 \
  --args /path/to/getbased_mcp.py
```

Then set `GETBASED_TOKEN` in `~/.hermes/.env` or in the MCP server's `env` config in `config.yaml`:

```yaml
mcp_servers:
  getbased:
    command: python3
    args: [/path/to/getbased_mcp.py]
    env:
      GETBASED_TOKEN: your-token-here
```

### 3. Use it

Ask about your labs in any connected conversation:

> "How's my vitamin D?"
> "What markers are out of range?"
> "Summarize my latest blood work"

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GETBASED_TOKEN` | Yes | Read-only token from getbased Settings > Data > Messenger Access |
| `GETBASED_GATEWAY` | No | Context gateway URL (default: `https://sync.getbased.health`) |

## Security

- **Read-only**: the token grants access to lab context text only — no raw data, no write access
- **Self-hosted**: the MCP server runs on your own machine
- **Revocable**: regenerate the token in getbased to revoke access instantly
- **No mnemonic exposure**: the token is independent of your sync mnemonic

## License

GPL-3.0
