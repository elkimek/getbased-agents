# getbased-agent-stack

One-command install of the full getbased agent stack: an MCP server that exposes your lab data and a local RAG knowledge server that grounds the AI in documents you trust.

## What's in the box

| Component | Role | Upstream |
|---|---|---|
| [`getbased-mcp`](https://github.com/elkimek/getbased-mcp) | MCP adapter — translates tool calls from Claude Code / Hermes / OpenClaw / any MCP client into HTTP requests | stdio |
| [`getbased-rag`](https://github.com/elkimek/getbased-rag) | Local RAG engine — FastAPI + Qdrant + MiniLM. Also the backend for the getbased PWA's "External server" Knowledge Base | HTTP, port 8322 |

```
Claude Code / Hermes / OpenClaw
        │ MCP (stdio)
        ▼
  getbased-mcp   ◀── this meta-package installs both
   │        │
   │ HTTP   │ HTTP
   ▼        ▼
sync GW   getbased-rag
          on localhost:8322
```

The MCP is lightweight (~10 MB). The RAG engine pulls in `sentence-transformers` + `qdrant-client` + optional ONNX Runtime for GPU acceleration (~500 MB). The `[full]` extra pulls the RAG's full parser stack (PDF, DOCX, ZIP) + ONNX so you can serve the same endpoint the PWA's External-server backend uses.

## Install

```bash
pipx install "getbased-agent-stack[full]"
```

Installs both sibling packages at version-compatible pins, plus the `getbased-stack` discovery wrapper. After install the two binaries are on your PATH:

- `lens` — RAG server CLI (`serve`, `ingest`, `stats`, `key`, ...)
- `getbased-mcp` — stdio MCP server (spawned on demand by agent clients)

## Quickstart

### 1. Start the RAG server

```bash
lens key                              # prints the bearer token, generates on first call
lens serve                            # blocks; runs on 127.0.0.1:8322
lens ingest /path/to/papers           # in another shell — indexes your docs
```

Or run it as a systemd user service (see `systemd/getbased-rag.service`):

```bash
mkdir -p ~/.config/systemd/user/
cp $(python -c 'import getbased_agent_stack, pathlib; print(pathlib.Path(getbased_agent_stack.__file__).parent.parent.parent / "systemd" / "getbased-rag.service")') ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now getbased-rag
```

### 2. Wire the MCP into your agent

**Claude Code** — add to `~/.claude/mcp.json` (see `examples/claude-code-mcp.json`):

```json
{
  "mcpServers": {
    "getbased": {
      "command": "getbased-mcp",
      "env": {
        "GETBASED_TOKEN": "your-read-only-token",
        "LENS_URL": "http://localhost:8322"
      }
    }
  }
}
```

**Hermes** — add to `~/.hermes/config.yaml` (see `examples/hermes-mcp.yaml`):

```yaml
mcp_servers:
  getbased:
    command: getbased-mcp
    env:
      GETBASED_TOKEN: "your-read-only-token"
      LENS_URL: "http://localhost:8322"
```

Get the `GETBASED_TOKEN` from getbased PWA → **Settings → Data → Agent Access**.

### 3. Also wire RAG into the PWA (optional)

The same RAG server can feed the getbased PWA's Knowledge Base. In the app, go to **Settings → AI → Knowledge Base → External server** and paste:

| Field | Value |
|---|---|
| URL | `http://127.0.0.1:8322` |
| API key | output of `lens key` |

Now the PWA's chat and the MCP agents all ground answers in the same library.

## Version compatibility

| Stack | mcp | rag | Protocol |
|---|---|---|---|
| 0.1.x | ≥0.2.0 | ≥0.1.0 | v1 (with multi-library) |

Bump the meta-package major when the sibling protocol breaks compatibility. For normal improvements, bump siblings freely — the meta just tracks their `>=` pins.

## Development

```bash
git clone https://github.com/elkimek/getbased-agent-stack
cd getbased-agent-stack
uv sync --extra test --extra full
uv run pytest                         # runs the integration test against a real lens server
```

The integration suite starts `lens serve` in a subprocess, ingests a fixture document, and exercises every MCP tool end-to-end. Catches protocol drift between the siblings (the kind of bug where "MCP v0.1 can't talk to RAG v1.21's multi-library endpoints" would otherwise slip through).

## Licence

GPL-3.0-only, matching the sibling packages.
