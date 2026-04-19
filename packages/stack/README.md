# getbased-agent-stack

Meta-package bundling the full [getbased](https://getbased.health) agent stack into one install: the MCP adapter, the RAG engine, the browser dashboard, a thin discovery CLI, a hardened systemd unit, and example configs for Claude Code + Hermes.

Part of the [getbased-agents monorepo](https://github.com/elkimek/getbased-agents).

## Install

```bash
pipx install "getbased-agent-stack[full]"
```

Pulls:

- [`getbased-mcp`](https://github.com/elkimek/getbased-agents/tree/main/packages/mcp) — stdio MCP server that Claude Code / Hermes / OpenClaw spawn
- [`getbased-rag`](https://github.com/elkimek/getbased-agents/tree/main/packages/rag) — local RAG knowledge server (FastAPI + Qdrant + MiniLM/BGE)
- [`getbased-dashboard`](https://github.com/elkimek/getbased-agents/tree/main/packages/dashboard) — web UI for library management, MCP config generation, and agent-activity inspection
- The `getbased-stack` discovery CLI
- `[full]` extra: PDF/DOCX parsers + ONNX runtime for hardware-accelerated embeddings

Total install: ~500 MB (the ML deps dominate). Smaller installs available — `pipx install getbased-mcp` (10 MB, agent only), `pipx install "getbased-rag[full]"` (RAG only), `pipx install getbased-dashboard` (UI + MCP; pulls rag if you want the Knowledge tab working).

## Quickstart

```bash
# 1. Start the RAG server — local Qdrant DB + MiniLM embedder
lens serve                               # blocks; serves on 127.0.0.1:8322
lens key                                 # prints the bearer token

# 2. Start the dashboard in another terminal
getbased-dashboard serve                 # serves on 127.0.0.1:8323

# 3. Open http://127.0.0.1:8323 in your browser, paste the lens key
#    Create libraries, drag-drop files to ingest (live chunks/sec pill),
#    run the MCP Test button to verify your agent path

# 4. Wire the MCP into your AI client
#    The dashboard's MCP tab generates paste-ready config blocks for
#    Claude Desktop, Claude Code, Cursor, Cline, and Hermes.
```

Both the RAG server and the getbased PWA talk to the same `lens` instance — point the PWA at `http://127.0.0.1:8322` under **Settings → AI → Knowledge Base → External server** and the same corpus feeds the browser chat, the dashboard, and any MCP-connected agent.

## Running as a systemd service

```bash
cp systemd/getbased-rag.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now getbased-rag
```

The unit is hardened (`ProtectSystem=strict`, `NoNewPrivileges`, `RestrictAddressFamilies`, etc.); run `systemd-analyze security getbased-rag` to see the score.

## Architecture

```
Claude Code / Hermes / OpenClaw          Browser
        │ MCP (stdio)                       │ HTTP
        ▼                                   ▼
  getbased-mcp                       getbased-dashboard   (localhost:8323)
   │        │                          │             │
   │ HTTP   │ HTTP                     │ proxies     │ spawns stdio for Test
   ▼        ▼                          ▼             ▼
sync GW   getbased-rag  ◀──────────────┘       getbased-mcp
          (localhost:8322)
```

The MCP holds no persistent state; it's a thin translator between MCP tool calls and two HTTP backends:

- `sync.getbased.health/api/context` — read-only lab summary pushed by your PWA session (via Agent Access token)
- `localhost:8322` (getbased-rag) — your local research library

The dashboard is likewise stateless — it proxies rag for Knowledge operations, imports `getbased_mcp` to introspect env/config, and spawns the MCP binary on demand to verify it works.

## Version compatibility

| Stack | mcp | rag | dashboard | Protocol |
|---|---|---|---|---|
| 0.1.x | ≥0.2.0 | ≥0.1.0 | — | v1 (multi-library) |
| 0.2.x | ≥0.2.2 | ≥0.6.0 | ≥0.5.0 | v1 (+ streaming ingest, per-library models) |

Bump the meta's major when sibling protocols break; bump siblings freely for normal features.

## Development

This package is the meta — the interesting code lives in sibling packages. See the [monorepo root README](https://github.com/elkimek/getbased-agents#development) for workspace setup.

The integration test (`tests/test_integration.py`) spins up `lens serve` in a subprocess, ingests a fixture, and exercises every MCP tool round-trip. Catches drift between the siblings the way the v1.21 catch-up drift would have been caught if the test existed then. The dashboard has its own test suite (`cd packages/dashboard && uv run pytest`) covering the proxy, modal logic, stdio probe, and activity-log handling.

## Related docs

- [packages/stack/CONTRIBUTING.md](CONTRIBUTING.md) — when to bump the meta vs a sibling
- [packages/stack/SECURITY.md](SECURITY.md) — threat model, scope, sibling pointers

## Licence

GPL-3.0-only, matching the siblings.
