# getbased-dashboard

Web dashboard for [getbased-agents](https://github.com/elkimek/getbased-agents) — one page that covers knowledge library management, MCP client setup, and agent-activity inspection. Matches the getbased PWA's browser-local lens UX for users running the external-server backend.

---

## What it looks like

Three tabs, single auth gate, one pill for ingest progress that lives outside the tab DOM so it survives navigation.

### Knowledge tab
- Engine badge strip at the top: `ONNX · CPU · MiniLM-L6-v2 · 384d · floor 0.55 · ready`
- Library list with per-row model chip, live chunk count (`12,758 chunks`), relative last-ingested (`indexed 2h ago`), activate/rename/delete
- Create-library form with a model dropdown (MiniLM-L6-v2 · BGE-small/base/large-en · BGE-M3) — dimension + download size shown per option
- Drag-drop ingest with a bottom-right pill: HTML5 `<progress>`, `12,500 / 16,000 · 3.2/s` chunks/sec rate, Cancel (partial commit) + Dismiss (×). 3s auto-dismiss on completion.
- Search preview with score per result
- Sources panel sorted by chunk count desc, Delete-all + per-source delete

### MCP tab
- Env viewer showing what a spawned MCP would see (`LENS_URL`, `LENS_API_KEY_FILE` + present/missing, `GETBASED_TOKEN` set/not set, module path). Tooltips explain the difference between "dashboard's env" and "client's MCP env block"
- Config generator — emits paste-ready blocks for **Claude Desktop**, **Claude Code**, **Cursor**, **Cline**, **Hermes**. JSON for the first four, YAML with `enabled_tools` allowlist for Hermes. Copy-to-clipboard button.
- "Test MCP" — spawns the real `getbased-mcp` binary via stdio, runs `initialize` + `tools/list`, returns elapsed ms + tool names

### Activity tab
- Top-line stat cards (total calls, errors, error rate, tools in use)
- Per-tool table with P50/P95 latency
- Newest-first feed of recent calls, polls every 10s
- Clear log button

---

## Install and run

```bash
pipx install getbased-dashboard
getbased-dashboard serve          # http://127.0.0.1:8323
```

The dashboard expects a [getbased-rag](https://github.com/elkimek/getbased-agents/tree/main/packages/rag) server at `http://127.0.0.1:8322` and reuses rag's API key. On first visit the UI prompts for the bearer key; it's stored in `localStorage` on your machine.

Or as part of the full stack:

```bash
pipx install "getbased-agent-stack[full]"
lens serve                       # in one terminal — the RAG backend
getbased-dashboard serve         # in another — the UI
```

---

## Architecture

```
  Browser                 Dashboard               Rag server            MCP subprocess
  localhost        ↔      localhost         ↔     localhost             on-demand stdio
                          /api/* proxy            /query, /ingest,      tools/list
                          + MCP test spawn        /libraries, /info,    (for Test button)
                          + activity tail         /models, /stats
```

The dashboard holds no data. Delete it and your knowledge base is untouched.

- All `/api/*` routes bearer-auth'd with the same key rag + MCP use (`secrets.compare_digest`, constant-time)
- Error envelope normalised to `{"error": "<string>"}` for both HTTPException and Pydantic validation errors — frontend has one shape to parse
- Upload path streams chunk-by-chunk to a temp file with a byte cap enforced before buffering (no OOM-via-multi-GB-upload)
- Client disconnect propagates: browser aborts fetch → dashboard drops upstream → rag sees disconnect → ingest stops at next batch boundary

---

## Config

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_HOST` | `127.0.0.1` | Bind host. Loopback-only by default — expose to LAN at your own risk |
| `DASHBOARD_PORT` | `8323` | Bind port |
| `LENS_URL` | `http://127.0.0.1:8322` | Where the rag server lives |
| `LENS_API_KEY_FILE` | `$XDG_DATA_HOME/getbased/lens/api_key` (with legacy fallback to `~/.hermes/rag/lens_api_key`) | Shared bearer token — same one MCP reads |
| `DASHBOARD_ACTIVITY_LOG` | `$XDG_STATE_HOME/getbased/mcp/activity.jsonl` | JSONL path the MCP writes to; dashboard tails it |
| `DASHBOARD_MAX_INGEST_BYTES` | `268435456` (256 MB) | Cap on a single upload's total size |
| `GETBASED_TOKEN` | (from env) | Optional. When set, the MCP tab's env viewer reads "set" and the config generator bakes it into the env blocks. Typically you leave it unset locally and set it in your AI client's MCP config |

---

## CLI

```
getbased-dashboard serve      Start the web server
getbased-dashboard info       Show resolved config + whether the rag key is on disk
```

---

## Security notes

- Dashboard binds loopback by default. Exposing via `DASHBOARD_HOST=0.0.0.0` means anyone on the LAN can drive your rag with the bearer key
- The bearer key is read fresh from disk on every authed request — rotating the key (rewrite the file) takes effect without a dashboard restart
- Multipart upload filenames are basename-sanitised before forwarding to rag (defence in depth; rag also sanitises)
- Subprocess spawn for the MCP test button reaps the child on timeout, exception, or cancellation — no orphaned processes

---

## License

GPL-3.0-only, matching the rest of the monorepo.
