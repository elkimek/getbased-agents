# getbased-dashboard

Web dashboard for [getbased-agents](https://github.com/elkimek/getbased-agents) — a single page where you manage your knowledge libraries, generate paste-ready MCP client configs, and see what your agent actually called.

## What you get

- **Knowledge** — list/create/rename/delete libraries, drag-drop ingest, quick search, per-source chunk stats. Runs against your local [`getbased-rag`](../rag) server.
- **MCP** — env/defaults viewer, one-click config generator for Claude Desktop / Hermes / Claude Code / Cursor, "Test MCP" button that spawns the stdio server and lists tools.
- **Activity** — recent tool calls, per-tool counts, error rate, P50/P95 latency. Arguments are NOT logged by default.

## Run it

```bash
pipx install getbased-dashboard
getbased-dashboard serve          # default: http://127.0.0.1:8323
```

The dashboard expects a `getbased-rag` server at `http://127.0.0.1:8322` and reuses rag's API key (found at `$XDG_DATA_HOME/getbased/lens/api_key`). On first visit the UI asks you to paste the key; it's stored in `localStorage` on your machine.

## Architecture

```
  Browser                Dashboard              Rag server            MCP subprocess
  (localhost)   ↔        (localhost)    ↔       (localhost)           (on-demand stdio)
  UI only                /api/* proxy           /query,/libraries,    tools/list, tools/call
                         + MCP test/spawn       /ingest,/stats
```

The dashboard does not store data — it's a thin orchestration layer over rag + mcp. Delete the dashboard and your knowledge base is untouched.

## Config

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_HOST` | `127.0.0.1` | Bind host. Loopback-only by default — expose to LAN at your own risk. |
| `DASHBOARD_PORT` | `8323` | Bind port |
| `LENS_URL` | `http://127.0.0.1:8322` | Where the rag server lives |
| `LENS_API_KEY_FILE` | `$XDG_DATA_HOME/getbased/lens/api_key` (with legacy fallback to `~/.hermes/rag/lens_api_key`) | Shared bearer token — same one MCP reads |
| `DASHBOARD_ACTIVITY_LOG` | `$XDG_STATE_HOME/getbased/mcp/activity.jsonl` | JSONL path the MCP writes to; dashboard tails it |

## License

GPL-3.0-only.
