# getbased MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes getbased health context and an optional local knowledge base as tools. Works with MCP-compatible clients such as Claude Code, Claude Desktop, Cursor, Cline, Codex CLI, Hermes, and OpenClaw.

> **Installing for the first time?** Use the private setup command from **getbased → Settings → Agent Access**. It installs or updates [getbased-agent-stack](https://github.com/elkimek/getbased-agents/tree/main/packages/stack), stores the token/key locally, and configures the selected client.

## How it works

```
getbased (browser)
  ├── your data, your mnemonic
  ├── generates an Agent Access relay token
  ├── generates a separate Agent Context encryption key
  ├── encrypts the rendered agent context with that context key
  └── pushes ciphertext to sync gateway on every save

Context gateway (sync.getbased.health/api/*)
  ├── stores encrypted context behind token auth
  └── queues encrypted action proposals; it cannot read either plaintext

Local knowledge server (localhost, optional)
  ├── Vector database with embedded chunks
  ├── Embedding model for semantic search
  └── Your curated health knowledge base

This MCP Server (on your machine)
  ├── fetches health context from the context gateway
  ├── encrypts narrow typed action proposals for browser review
  ├── queries the local knowledge server for knowledge-base searches (optional)
  └── exposes everything as tools to any MCP client
```

Your mnemonic never leaves your browser. The gateway receives only encrypted envelopes. This MCP decrypts Agent Context locally and can submit a `sun.session.log` proposal encrypted with the same dedicated key. The MCP schema exposes that exact action and argument shape rather than an open-ended JSON object. Proposal IDs are derived from their random encryption IV, so caller-controlled action text cannot leak through the clear queue identifier. The proposal cannot mutate getbased: the browser validates profile, capability, expiry, schema, and replay state, then waits for explicit user approval.

## Tools

| Tool | Description |
|---|---|
| `getbased_propose_action` | Encrypt and submit a typed `sun.session.log` proposal to the selected profile. The browser must validate and the user must explicitly Apply it before anything changes. |
| `getbased_lab_context` | Full lab summary with biomarkers, context cards, supplements, goals. Pass `profile` to target a specific profile. |
| `getbased_section` | Get a specific section (e.g. hormones, lipids) or list all available sections |
| `getbased_list_profiles` | List available profiles |
| `knowledge_search` | Semantic search across the active library on your knowledge base (requires the local knowledge server). Returns relevant passages with source attribution. |
| `knowledge_list_libraries` | List all knowledge base libraries and show which is active |
| `knowledge_activate_library` | Switch the active library — subsequent searches target the new one until switched again |
| `knowledge_stats` | Per-source chunk counts for the active library — useful for diagnosing missing results |
| `getbased_lens_config` | Show RAG endpoint config for getbased's Knowledge Base (External server) |

### getbased_propose_action

The first write-capable lane is deliberately narrow and proposal-only:

```text
getbased_propose_action(
  action_id="sun.session.log",
  arguments={"durationMinutes": 60, "notes": "Sunbathing"},
  profile="default"
)
```

The tool returns a proposal ID, not an execution result. getbased shows the decrypted proposal under **Manage → Context → Agent proposals**. Only the user's **Apply** click reaches the existing sunlight-session persistence path; **Dismiss** changes nothing.

### getbased_section

Query-aware context: pull just the section you need instead of the full dump. Saves tokens and allows deeper analysis of specific areas.

```
# No args — returns section index with names, updated dates, and line counts
getbased_section()

# With section name — returns just that section's content
getbased_section(section="hormones")

# With profile — query a specific profile
getbased_section(section="hormones", profile="mne8m9hf")
```

Section names are matched by prefix, so `hormones` matches `hormones updated:2026-03-13`.

### knowledge_search

**What is RAG?** Retrieval-Augmented Generation (RAG) is a technique where an AI assistant's responses are grounded in a specific knowledge base. Instead of relying solely on training data, the assistant first searches a curated collection of documents for relevant passages, then uses those passages to inform its answer. This makes the AI's output more accurate, more specific, and traceable to real sources.

The `knowledge_search` tool searches your knowledge base using semantic similarity — meaning it finds passages that match the *meaning* of your query, not just keywords. Results include the passage text and source attribution.

```
# Basic search
knowledge_search(query="blue light DHA mitochondrial damage")

# With result count (1–10, default 5)
knowledge_search(query="MTHFR methylation folate", n_results=5)
```

**Note:** This tool requires the local knowledge server to be running. If it is not running, knowledge search will fail, but the lab-context tools still work.

### Multi-library (v0.2+)

The Lens server (0.2+ of `getbased-rag`) supports multiple libraries — keep research papers, clinical guides, and personal notes in separate collections and switch between them. `knowledge_search` always targets the currently active library.

```
# See what's available and which is active
knowledge_list_libraries()

# Switch. Subsequent knowledge_search calls hit this library until switched again
knowledge_activate_library(library_id="<id-from-list>")

# Confirm what's indexed in the active library
knowledge_stats()
```

## Multi-profile

The gateway stores context per profile ID. To work with multiple profiles:

- Use `getbased_list_profiles` to see available profiles and their IDs
- Pass `profile="id"` to any tool to query a specific profile
- Omit the `profile` param to use the default profile
- Each profile's context is pushed automatically when data is saved or the profile is switched in getbased

## Setup

### Recommended: copy the private setup command

In getbased, open **Settings → Agent Access**, choose your target client, and copy the setup command. It looks like this:

```bash
curl -fsSL https://getbased.health/install.sh | bash -s -- connect <target> --setup 'gbsetup_v1_...'
```

The setup payload is private. Paste it only into a terminal you control.

### Manual setup

If you are not using `getbased-stack connect`, open **Settings → Agent Access** and copy both values:

- **Agent Access token** → `GETBASED_TOKEN` for context reads and encrypted proposal submission
- **Context encryption key** → `GETBASED_AGENT_CONTEXT_KEY` for local decryption

### Set up a local knowledge server (optional, for `knowledge_search`)

The knowledge base runs as a separate service. The easiest path is `getbased-agent-stack[full]`, which installs `getbased-rag` and the browser dashboard. If you are wiring your own server, you need:

- A vector database (e.g. [Qdrant](https://qdrant.tech/), [ChromaDB](https://www.trychroma.com/)) loaded with your document chunks and embeddings
- A FastAPI (or similar) server that accepts `POST /query` with `{version: 1, query: "...", top_k: N}` and returns `{chunks: [{text: "...", source: "..."}]}`
- An embedding model (e.g. [BGE-M3](https://huggingface.co/BAAI/bge-m3)) for semantic search

The local knowledge server handles embedding, similarity search, and filtering. This MCP just sends HTTP queries to it; no models are loaded inside the MCP process.

**Knowledge server contract:**

| Field | Required | Description |
|---|---|---|
| `POST /query` | Yes | Accepts JSON body with `version` (int), `query` (string), `top_k` (int) |
| `Authorization` | Recommended | Bearer token auth |
| `GET /health` | Optional | Returns `{"status": "ok", "rag_ready": bool, "chunks": int}` |
| Response | Yes | `{"chunks": [{"text": "...", "source": "..."}]}` |

### Configure your MCP client manually

#### Claude Code / Claude Desktop

Add to your MCP config (`~/.claude/claude_desktop_config.json` or similar):

```json
{
  "mcpServers": {
    "getbased": {
      "command": "python3",
      "args": ["/path/to/getbased_mcp.py"],
      "env": {
        "GETBASED_TOKEN": "your-token-here",
        "GETBASED_AGENT_CONTEXT_KEY": "your-context-key-here"
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

Then set `GETBASED_TOKEN` and `GETBASED_AGENT_CONTEXT_KEY` in the MCP server's `env` config in `config.yaml`:

```yaml
mcp_servers:
  getbased:
    command: python3
    args: [/path/to/getbased_mcp.py]
    env:
      GETBASED_TOKEN: your-token-here
      GETBASED_AGENT_CONTEXT_KEY: your-context-key-here
```

### 4. Use it

Ask about your labs in any connected conversation:

> "How's my vitamin D?"
> "What markers are out of range?"
> "Summarize my latest blood work"
> "What does the knowledge base say about blue light and DHA?"

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GETBASED_TOKEN` | Yes | Read-only bearer token from getbased Settings → Agent Access; authorizes fetches from the context gateway |
| `GETBASED_AGENT_CONTEXT_KEY` | Yes for encrypted Agent Access payloads | Context encryption key from getbased Settings → Agent Access; decrypts the relay payload locally in this MCP |
| `GETBASED_GATEWAY` | No | Context gateway URL (default: `https://sync.getbased.health`) |
| `GETBASED_AGENT_PROPOSAL_GATEWAY` | No | Proposal relay URL when proposal transport is isolated from the context gateway (defaults to `GETBASED_GATEWAY`) |
| `LENS_URL` | No | Local knowledge server URL (default: `http://localhost:8322`). Overrides `LENS_PORT` |
| `LENS_PORT` | No | Local knowledge server port, only used to build default `LENS_URL` (default: `8322`) |
| `LENS_API_KEY_FILE` | No | Path to the knowledge server API key file. Default: `$XDG_DATA_HOME/getbased/lens/api_key` (getbased-rag's canonical location). If that file doesn't exist but the legacy `~/.hermes/rag/lens_api_key` does, the legacy path is used instead; upgrades from standalone `getbased-mcp` ≤ 0.1.0 keep working without config changes. |
| `LENS_MCP_ACTIVITY_LOG` | No | JSONL path where tool-call activity is appended. Default: `$XDG_STATE_HOME/getbased/mcp/activity.jsonl`. Each record: `{ts, tool, duration_ms, ok, error?}` — arguments are never logged (queries may contain sensitive health info). Set to `off` / `false` / `0` to disable. The [getbased-dashboard](https://github.com/elkimek/getbased-agents/tree/main/packages/dashboard) Activity tab tails this file. |

## Custom Knowledge Source (getbased app)

The same local knowledge server that powers `knowledge_search` for your AI client can also back the in-app AI chat. To connect them:

1. Run `getbased_lens_config` — it returns the endpoint URL, API key, and recommended `top_k`
2. In getbased, open **Knowledge Base** and choose **External server**
3. Paste the endpoint URL, API key, and set `top_k` to 5
4. Enable it — the chat-header Lens badge will light up green when active

Every chat question and Current Focus refresh can now include passages from your knowledge base.

## Troubleshooting

### `knowledge_search` returns "Lens server not reachable"

The local knowledge server is not running. Start it and verify with:

```bash
curl http://localhost:8322/health
```

### `knowledge_search` returns "Lens API key not found"

getbased-rag generates its API key on first start and writes it to `$XDG_DATA_HOME/getbased/lens/api_key` (e.g. `~/.local/share/getbased/lens/api_key` on Linux). If you're upgrading from the standalone `getbased-mcp` ≤ 0.1.0 and your key is at `~/.hermes/rag/lens_api_key`, that legacy path is still auto-detected; no config change needed. If the file is missing entirely, restart the knowledge server and it will create a new one.

### `knowledge_list_libraries` / `knowledge_stats` return "this lens server doesn't expose library management"

The lens server you're pointed at is older than `getbased-rag` 0.1.0 and doesn't implement the `/libraries` or `/stats` endpoints. `knowledge_search` still works against older lens servers since `/query` is protocol-stable. To get library management, either upgrade the lens, or set `LENS_URL` to a library-capable endpoint.

### Blood work tools work but knowledge_search doesn't

That's expected. Blood work tools talk to the context gateway; `knowledge_search` talks to the local knowledge server. If the knowledge server is down, knowledge search will fail, but the lab-context tools continue to work normally.

## Security

- **Read-only**: the token grants access to lab context text only — no raw data, no write access
- **Self-hosted**: the MCP server runs on your own machine
- **Revocable**: regenerate the token in getbased to revoke access instantly
- **No mnemonic exposure**: the token is independent of your sync mnemonic
- **No models in-process**: RAG queries go through the external server — no embedding models loaded in the MCP process

## Related projects

- **[getbased](https://github.com/elkimek/get-based)** — the health dashboard. This MCP reads the same context the in-app AI chat uses, and can query the same external Knowledge Base server configured in the app. The endpoint contract is shared: one server can back both the app and this MCP.

## License

AGPL-3.0-or-later
