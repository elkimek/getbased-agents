# getbased-rag

> **Installing for the first time?** The [getbased-agent-stack](https://github.com/elkimek/getbased-agents/tree/main/packages/stack) meta-package bundles this server with the MCP that Claude Code / Hermes / OpenClaw talk to, plus [getbased-dashboard](https://github.com/elkimek/getbased-agents/tree/main/packages/dashboard) for a browser UI. One command and you're up.

A standalone RAG knowledge server — the backend that used to ship inside the getbased Electron desktop app, now just Python. Point any client (the getbased PWA's *External server* lens backend, the dashboard, or your own) at it.

- **Stack**: FastAPI + Uvicorn · Qdrant (embedded local mode) · sentence-transformers / ONNX Runtime
- **Default port**: 8322, loopback only
- **Auth**: Bearer token, auto-generated on first start
- **Stores**: every library is its own Qdrant collection, pinned to its own embedding model at creation

---

## Install

Requires Python ≥ 3.10.

```bash
pipx install "getbased-rag[full]"
```

Or from source:

```bash
git clone https://github.com/elkimek/getbased-agents.git
cd getbased-agents
uv sync --all-packages --all-extras
```

---

## Run

```bash
lens serve
```

First start auto-generates an API key at the data dir (see below), prints the bind address, and lazy-loads the embedding model on the first query (~90 MB download for MiniLM).

Copy the API key out when you need to configure a client:

```bash
lens key
```

Smoke test:

```bash
curl -s http://127.0.0.1:8322/health
curl -s -H "Authorization: Bearer $(lens key)" http://127.0.0.1:8322/info | jq
```

Ingest a file or directory from the CLI:

```bash
lens ingest ~/Documents/research
lens stats
```

Or over HTTP (what the dashboard + PWA use):

```bash
curl -H "Authorization: Bearer $(lens key)" \
  -F "files=@paper.pdf" -F "files=@notes.md" \
  http://127.0.0.1:8322/ingest
```

---

## Per-library embedding models

Every library is pinned to one embedding model at creation time — Qdrant collections are dimension-locked, so you can't swap models on an existing library without re-ingesting. Call `GET /models` for the curated list (MiniLM-L6-v2 · BGE-small/base/large-en · BGE-M3) with dims and download sizes, then pass `embedding_model` on create:

```bash
curl -H "Authorization: Bearer $(lens key)" \
  -H "Content-Type: application/json" \
  -d '{"name":"Research","embedding_model":"BAAI/bge-m3"}' \
  http://127.0.0.1:8322/libraries
```

Libraries on the same model share one embedder instance in memory. Two libraries both on BGE-M3 use ~2 GB total, not 4.

---

## Streaming ingest progress

The HTTP `POST /ingest` endpoint speaks two content types:

- Default (no `Accept`): single-shot JSON summary after the run completes
- `Accept: application/x-ndjson`: newline-delimited JSON progress stream — one `start` event (with total chunks), per-batch `embed` events every ~5 chunks (with current source + index), terminal `result` or `error` event

The dashboard uses the streaming path for its bottom-right pill (chunks/sec rate, cancel button, per-filename status). Cancellation works by client disconnect: aborting the fetch causes the server's worker thread to exit at the next batch boundary with `cancelled: true` in the result. Partial-commit — whatever was embedded before the cancel stays.

---

## Wiring into the getbased PWA

In the PWA: **Settings → AI → Knowledge Base → External server**

| Field | Value |
|---|---|
| URL | `http://127.0.0.1:8322` |
| API key | output of `lens key` |

Click **Save**, then **Test connection**. `rag_ready: false` is expected before you ingest anything.

### Agent access (Claude Code, Hermes, OpenClaw, etc.)

Pair this server with [getbased-mcp](https://github.com/elkimek/getbased-agents/tree/main/packages/mcp) to expose `knowledge_search`, `knowledge_list_libraries`, `knowledge_activate_library`, and `knowledge_stats` as MCP tools. Typical setup: run both the lens server and getbased-mcp on the same VM, point MCP's `LENS_URL` at `http://localhost:8322`.

### Browser UI

Install [getbased-dashboard](https://github.com/elkimek/getbased-agents/tree/main/packages/dashboard) for a web UI on top of this server — library management, drag-drop ingest with live progress pill, search preview, MCP config generator.

---

## Configuration

Every setting is an environment variable. Defaults in parentheses.

| Variable | Purpose |
|---|---|
| `LENS_HOST` (`127.0.0.1`) | Bind interface. Change to `0.0.0.0` only if you really want LAN access |
| `LENS_PORT` (`8322`) | TCP port |
| `LENS_DATA_DIR` (platform default) | Where Qdrant DB, API key, and model cache live |
| `LENS_EMBEDDING_MODEL` (`sentence-transformers/all-MiniLM-L6-v2`) | Default model for new libraries (overridable per library) |
| `LENS_SIMILARITY_FLOOR` (`0.55`) | Minimum cosine score for a returned chunk |
| `LENS_ONNX_PROVIDER` (auto) | `cuda` \| `rocm` \| `openvino` \| `coreml` \| `cpu` |
| `LENS_RERANKER` (`false`) | Enable reranking of top candidates |
| `LENS_MAX_INGEST_BYTES` (`268435456` — 256 MB) | Cap on a single ingest upload's total size |
| `LENS_CHUNK_MAX_SIZE` (`800`) | Max chunk size in characters |
| `LENS_CORS_ORIGINS` (empty) | Comma-separated extra CORS origins to allow, in addition to the PWA + loopback defaults |

Default data dir:

- Linux: `$XDG_DATA_HOME/getbased/lens` or `~/.local/share/getbased/lens`
- macOS: `~/Library/Application Support/getbased/lens`
- Windows: `%APPDATA%\getbased\lens`

A legacy `~/.getbased/lens` is honored if it already exists, so pre-v1.21 installs don't lose their data.

### GPU acceleration

Install the matching `onnxruntime-*` wheel (e.g. `onnxruntime-gpu` for CUDA), then:

```bash
LENS_ONNX_PROVIDER=cuda lens serve
```

---

## HTTP API

All endpoints except `/`, `/health` require `Authorization: Bearer <key>`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + `rag_ready` + chunk count. Public |
| `GET` | `/info` | Embedder engine/model/dim, active library, similarity floor. For UI engine badges |
| `GET` | `/models` | Curated model picker list (id, label, dim, size_mb) plus the server's default |
| `POST` | `/query` | `{ query, top_k }` → top-k chunks from the active library, encoded with that library's model |
| `POST` | `/ingest` | Multipart upload; accepts streaming NDJSON progress when `Accept: application/x-ndjson` |
| `GET` | `/stats` | Per-source chunk counts for the active library |
| `DELETE` | `/sources/{source}` | Drop one source from the active library |
| `DELETE` | `/sources` | Clear the active library's chunks (library stays) |
| `GET` | `/libraries` | List libraries + active id. Each row includes `chunks`, `lastIngestAt`, `embedding_model` |
| `POST` | `/libraries` | `{ name, embedding_model? }` → create. 409 on duplicate name (case-insensitive) |
| `POST` | `/libraries/{id}/activate` | Set active |
| `PATCH` | `/libraries/{id}` | Rename |
| `DELETE` | `/libraries/{id}` | Delete (drops Qdrant collection) |

---

## Security notes

- Default bind is `127.0.0.1` — queries never leak to the LAN unless you explicitly set `LENS_HOST=0.0.0.0`.
- The API key file is mode `0600` and never exposed over HTTP. Use `lens key` locally to read it.
- Bearer comparison uses `secrets.compare_digest` — constant-time, no timing-leak class of bug.
- Upload paths are basename-sanitised server-side (so `../../etc/passwd` can't escape the ingest temp dir).
- Zip uploads are zip-slip-guarded — each archive entry must resolve inside its own per-zip subdirectory AND inside the overall ingest root.
- If you expose the server to a LAN or the internet, front it with a reverse proxy that terminates TLS and rate-limits.

---

## CLI

```
lens serve            Start the HTTP server (default)
lens ingest <path>    Index files into the active library
lens stats            List indexed sources + chunk counts
lens delete <source>  Drop chunks belonging to one source
lens clear            Wipe the active library
lens info             Show config + API key
lens key              Print the API key (creates one if missing)
```

---

## License

GPL-3.0-only.

---

## Lineage

This repo is the Python portion lifted out of [getbased](https://github.com/elkimek/getbased) after the Electron desktop app was retired. The PWA's `external-server` lens backend speaks this same HTTP contract unchanged.
