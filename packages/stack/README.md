# getbased-agent-stack

Meta-package bundling the full [getbased](https://getbased.health) agent stack into one install: the MCP adapter, the RAG engine, a thin discovery CLI, a hardened systemd unit, and example configs for Claude Code + Hermes.

Part of the [getbased-agents monorepo](https://github.com/elkimek/getbased-agents).

## Install

```bash
pipx install "getbased-agent-stack[full]"
```

Pulls:

- [`getbased-mcp`](https://github.com/elkimek/getbased-agents/tree/main/packages/mcp) — stdio MCP server that Claude Code / Hermes / OpenClaw spawn
- [`getbased-rag`](https://github.com/elkimek/getbased-agents/tree/main/packages/rag) — local RAG knowledge server (FastAPI + Qdrant + MiniLM)
- The `getbased-stack` discovery CLI
- `[full]` extra: PDF/DOCX parsers + ONNX runtime for hardware-accelerated embeddings

Total install: ~500 MB (the ML deps dominate). If you only want one side, `pipx install getbased-mcp` (10 MB, agent only) or `pipx install "getbased-rag[full]"` (RAG only).

## Quickstart

```bash
# 1. Start the RAG server — uses a local Qdrant DB + MiniLM embedder
lens serve                               # blocks; serves on 127.0.0.1:8322
lens key                                 # prints the bearer token

# 2. Ingest your documents
lens ingest /path/to/papers              # in a separate shell

# 3. Wire the MCP into your agent
#    See examples/claude-code-mcp.json or examples/hermes-mcp.yaml
```

Both the RAG server and the getbased PWA talk to the same `lens` instance — point the PWA at `http://127.0.0.1:8322` under **Settings → AI → Knowledge Base → External server** and the same corpus feeds both.

## Running as a systemd service

```bash
cp systemd/getbased-rag.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now getbased-rag
```

The unit is hardened (`ProtectSystem=strict`, `NoNewPrivileges`, `RestrictAddressFamilies`, etc.); run `systemd-analyze security getbased-rag` to see the score.

## Architecture

```
Claude Code / Hermes / OpenClaw
        │ MCP (stdio)
        ▼
  getbased-mcp        ◀── this package installs both
   │        │
   │ HTTP   │ HTTP
   ▼        ▼
sync GW   getbased-rag
          on localhost:8322
```

The MCP holds no persistent state; it's a thin translator between MCP tool calls and two HTTP backends:

- `sync.getbased.health/api/context` — read-only lab summary pushed by your PWA session (via Agent Access token)
- `localhost:8322` (getbased-rag) — your local research library

## Version compatibility

| Stack | mcp | rag | Protocol |
|---|---|---|---|
| 0.1.x | ≥0.2.0 | ≥0.1.0 | v1 (multi-library) |

Bump the meta's major when sibling protocols break; bump siblings freely for normal features.

## Development

This package is the meta — the interesting code lives in sibling packages. See the [monorepo root README](https://github.com/elkimek/getbased-agents#development) for workspace setup.

The integration test (`tests/test_integration.py`) spins up `lens serve` in a subprocess, ingests a fixture, and exercises every MCP tool round-trip. Catches drift between the siblings the way the v1.21 catch-up drift would have been caught if the test existed then.

## Related docs

- [packages/stack/CONTRIBUTING.md](CONTRIBUTING.md) — when to bump the meta vs a sibling
- [packages/stack/SECURITY.md](SECURITY.md) — threat model, scope, sibling pointers

## Licence

GPL-3.0-only, matching the siblings.
