# getbased-rag

A standalone RAG knowledge server — the backend that used to ship inside the getbased Electron desktop app, now just Python. Point any client (the getbased PWA's *External server* lens backend, or your own) at it.

- **Stack**: FastAPI + Uvicorn · Qdrant (embedded local mode) · sentence-transformers / ONNX Runtime
- **Default port**: 8322, loopback only
- **Auth**: Bearer token, auto-generated on first start
- **Stores**: every library is its own Qdrant collection under the data dir

---

## Install

Requires Python ≥ 3.10.

```bash
pipx install "git+https://github.com/elkimek/getbased-rag.git[full]"
```

Or from source:

```bash
git clone https://github.com/elkimek/getbased-rag.git
cd getbased-rag
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full]"
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
curl -s -H "Authorization: Bearer $(lens key)" http://127.0.0.1:8322/stats
```

Ingest a file or a directory:

```bash
lens ingest ~/Documents/research
lens stats
```

---

## Wiring into the getbased PWA

In the PWA: **Settings → AI → Knowledge Base → External server**

| Field | Value |
|---|---|
| URL | `http://127.0.0.1:8322` |
| API key | output of `lens key` |

Click **Save**, then **Test connection**. `rag_ready: false` is expected before you ingest anything.

### Agent access (Claude Code, Hermes, OpenClaw, etc.)

Pair this server with [getbased-mcp](https://github.com/elkimek/getbased-mcp) to expose `knowledge_search`, `knowledge_list_libraries`, `knowledge_activate_library`, and `knowledge_stats` as MCP tools. Any MCP-compatible client — Claude Code, Hermes Agent, OpenClaw — can then search your ingested library over any transport the client supports (stdio, SSE, websocket). Typical setup: run both the lens server and getbased-mcp on the same VM, point MCP's `LENS_URL` at `http://localhost:8322`.

---

## Configuration

Every setting is an environment variable. Defaults in parentheses.

| Variable | Purpose |
|---|---|
| `LENS_HOST` (`127.0.0.1`) | Bind interface. Change to `0.0.0.0` only if you really want LAN access |
| `LENS_PORT` (`8322`) | TCP port |
| `LENS_DATA_DIR` (platform default) | Where Qdrant DB, API key, and model cache live |
| `LENS_EMBEDDING_MODEL` (`sentence-transformers/all-MiniLM-L6-v2`) | HF model id |
| `LENS_SIMILARITY_FLOOR` (`0.55`) | Minimum cosine score for a returned chunk |
| `LENS_ONNX_PROVIDER` (auto) | `cuda` \| `rocm` \| `openvino` \| `coreml` \| `cpu` |
| `LENS_RERANKER` (`false`) | Enable reranking of top candidates |
| `LENS_CHUNK_MAX_SIZE` (`800`) | Max chunk size in characters |

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

All endpoints (except `/` and `/health`) require `Authorization: Bearer <key>`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + `rag_ready` + chunk count. Public |
| `POST` | `/query` | `{ query, top_k }` → top-k chunks from the active library |
| `GET` | `/stats` | Per-source chunk counts for the active library |
| `DELETE` | `/sources/{source}` | Drop one source from the active library |
| `DELETE` | `/sources` | Clear the active library |
| `GET` | `/libraries` | List libraries + active id |
| `POST` | `/libraries` | Create a new library |
| `POST` | `/libraries/{id}/activate` | Set active |
| `PATCH` | `/libraries/{id}` | Rename |
| `DELETE` | `/libraries/{id}` | Delete (drops Qdrant collection) |

---

## Security notes

- Default bind is `127.0.0.1` — queries never leak to the LAN unless you explicitly set `LENS_HOST=0.0.0.0`.
- The API key file is mode `0600` and never exposed over HTTP. Use `lens key` locally to read it.
- If you expose the server to a LAN or the internet, front it with a reverse proxy that terminates TLS and rate-limits.

---

## CLI

```
lens serve            Start the HTTP server (default)
lens ingest <path>    Index files into the local store
lens stats            List indexed sources + chunk counts
lens delete <source>  Drop chunks belonging to one source
lens clear            Wipe the active library
lens info             Show config + API key
lens key              Print the API key (creates one if missing)
```

---

## Roadmap

- [ ] Docker image (on request)
- [ ] Configurable embedding model tier (small / base / large) with guardrails
- [ ] OpenAPI-generated client for the PWA `external-server` backend
- [ ] Optional multi-tenant mode (per-owner API keys + library quotas)

---

## License

TBD — private repo during bring-up. Will land here once published.

---

## Lineage

This repo is the Python portion lifted out of [getbased](https://github.com/elkimek/getbased) after the Electron desktop app was retired. The PWA's `external-server` lens backend speaks this same HTTP contract unchanged.
