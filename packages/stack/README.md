# getbased-agent-stack

Meta-package bundling the full [getbased](https://getbased.health) agent stack into one install: the MCP adapter, the RAG engine, the browser dashboard, an orchestration CLI (`init` / `install` / `mcp-config`), hardened systemd units for rag + dashboard, and paste-ready configs for Claude Desktop/Code, Cursor, Cline, and Hermes.

Part of the [getbased-agents monorepo](https://github.com/elkimek/getbased-agents).

## Install

```bash
pipx install --include-deps "getbased-agent-stack[full]"
```

The `--include-deps` flag is required — it exposes `getbased-mcp`, `lens`, and `getbased-dashboard` alongside `getbased-stack` on your PATH. Without it, pipx only links the stack's own entry point and the MCP/rag/dashboard binaries stay hidden inside the venv.

`uv` users: install each package as its own tool instead, since `uv tool` has no `--include-deps` equivalent yet:

```bash
uv tool install getbased-mcp
uv tool install "getbased-rag[full]"
uv tool install getbased-dashboard
uv tool install "getbased-agent-stack[full]"
```

Pulls:

- [`getbased-mcp`](https://github.com/elkimek/getbased-agents/tree/main/packages/mcp) — stdio MCP server that Claude Code / Hermes / OpenClaw spawn
- [`getbased-rag`](https://github.com/elkimek/getbased-agents/tree/main/packages/rag) — local RAG knowledge server (FastAPI + Qdrant + MiniLM/BGE)
- [`getbased-dashboard`](https://github.com/elkimek/getbased-agents/tree/main/packages/dashboard) — web UI for library management, MCP config generation, and agent-activity inspection
- The `getbased-stack` discovery CLI
- `[full]` extra: PDF/DOCX parsers + ONNX runtime for hardware-accelerated embeddings

Total install: ~500 MB (the ML deps dominate). Smaller installs available — `pipx install getbased-mcp` (10 MB, agent only), `pipx install "getbased-rag[full]"` (RAG only), `pipx install getbased-dashboard` (UI + MCP; pulls rag if you want the Knowledge tab working).

## Quickstart — one command

```bash
getbased-stack init
```

The wizard (~30 seconds):

1. Prompts for your `GETBASED_TOKEN` (skip if you don't use sync)
2. Generates a rag API key if one doesn't exist
3. Writes `~/.config/getbased/env` (mode 0600) — the shared config file
4. Installs systemd user units for rag + dashboard, enables them, starts them

Then paste one line into your MCP client:

```bash
getbased-stack mcp-config claude-desktop   # or: claude-code, cursor, cline, hermes
```

The snippet carries only `GETBASED_STACK_MANAGED=1` in its env block. No secrets in client configs — every MCP spawn reads the shared env file and loads the token + rag URL + API key path from there.

Open the dashboard:

```
http://127.0.0.1:8323
```

Login URL with bearer key:

```bash
getbased-dashboard login-url   # prints http://127.0.0.1:8323/?key=...
```

Upload docs, create libraries, manage sources, and test the MCP probe from the web UI. Rotate the sync token from the CLI (see below) or by editing `~/.config/getbased/env` directly.

### Surviving reboot on headless hosts

User systemd services stop on logout. On a headless server (no GUI session), they won't come back at boot unless you enable linger once:

```bash
sudo loginctl enable-linger $USER
```

`getbased-stack init` prints this reminder when it detects a headless environment. On a laptop with a GUI login, linger is nice-to-have — services start when you log in.

### Other commands

```bash
getbased-stack status          # env file, unit state, linger
getbased-stack set GETBASED_TOKEN=new   # rotate the token
getbased-stack install         # re-apply unit files after package upgrade
getbased-stack uninstall       # stop + disable + remove units
```

### Migrating from an older install

If you have a hand-rolled setup (standalone `lens-rag.service`, hermes-style `~/.hermes/config.yaml` with MCP env), **leave it alone** — `getbased-stack init` only writes new paths and installs new unit names (`getbased-rag.service`, `getbased-dashboard.service`), so it can coexist. The opt-in loader in every binary is gated on `GETBASED_STACK_MANAGED=1`; without that flag set, every binary behaves exactly as before.

If you're running the existing Hermes VM deployment on this host, don't run `init` there. Your `~/.hermes/config.yaml` continues to supply env explicitly; nothing from this package touches it.

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
| 0.4.x | ≥0.2.3 | ≥0.7.1 | ≥0.6.1 | v1 (+ shared env file, `getbased-stack init`, systemd units) |

Bump the meta's major when sibling protocols break; bump siblings freely for normal features.

## Development

This package is the meta — the interesting code lives in sibling packages. See the [monorepo root README](https://github.com/elkimek/getbased-agents#development) for workspace setup.

The integration test (`tests/test_integration.py`) spins up `lens serve` in a subprocess, ingests a fixture, and exercises every MCP tool round-trip. Catches drift between the siblings the way the v1.21 catch-up drift would have been caught if the test existed then. The dashboard has its own test suite (`cd packages/dashboard && uv run pytest`) covering the proxy, modal logic, stdio probe, and activity-log handling.

## Related docs

- [packages/stack/CONTRIBUTING.md](CONTRIBUTING.md) — when to bump the meta vs a sibling
- [packages/stack/SECURITY.md](SECURITY.md) — threat model, scope, sibling pointers

## Licence

GPL-3.0-only, matching the siblings.
