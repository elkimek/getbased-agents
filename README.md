# getbased-agents

Monorepo for the [getbased](https://getbased.health) agent ecosystem — MCP server, RAG backend, and a meta-package that wires them together.

| Package | PyPI | Role | Contents |
|---|---|---|---|
| [`getbased-mcp`](packages/mcp/) | `getbased-mcp` | MCP adapter for Claude Code / Hermes / OpenClaw / any MCP client | stdio ↔ HTTP |
| [`getbased-rag`](packages/rag/) | `getbased-rag` | Local RAG knowledge server. Also the PWA's "External server" Knowledge Base backend | FastAPI + Qdrant + MiniLM/BGE |
| [`getbased-agent-stack`](packages/stack/) | `getbased-agent-stack` | Meta-package pinning the two siblings | thin CLI + systemd unit + example configs |

```
Claude Code / Hermes / OpenClaw
        │ MCP (stdio)
        ▼
  getbased-mcp
   │        │
   │ HTTP   │ HTTP
   ▼        ▼
sync GW   getbased-rag
          (localhost:8322)
```

## Install

Most users: **one command** via the meta-package:

```bash
pipx install "getbased-agent-stack[full]"
```

Or pick the piece you actually need:

```bash
pipx install getbased-mcp            # agents for lab data only, no RAG  (~10 MB)
pipx install "getbased-rag[full]"    # RAG backend for the PWA, no agents (~500 MB)
```

## Quickstart

See [`packages/stack/README.md`](packages/stack/README.md) — walks through starting the RAG server, wiring the MCP into Claude Code and Hermes, and pointing the PWA at the same backend.

## Development

```bash
git clone https://github.com/elkimek/getbased-agents
cd getbased-agents
uv sync --all-packages --all-extras
```

Each package runs its own tests from its own directory:

```bash
(cd packages/mcp && uv run pytest)     # 22 unit tests, respx-mocked HTTP
(cd packages/rag && uv run pytest)     # 26 tests, FastAPI TestClient + fake embedder
(cd packages/stack && uv run pytest)   # 2 integration tests: real lens subprocess + real MCP tool calls
```

CI runs the same matrix on Python 3.10/3.11/3.12 (unit) + 3.12 (integration) on every push and PR.

Per-package details:
- [packages/mcp/README.md](packages/mcp/README.md) + [CONTRIBUTING](packages/mcp/CONTRIBUTING.md) + [SECURITY](packages/mcp/SECURITY.md)
- [packages/rag/README.md](packages/rag/README.md) + [CONTRIBUTING](packages/rag/CONTRIBUTING.md) + [SECURITY](packages/rag/SECURITY.md)
- [packages/stack/README.md](packages/stack/README.md) + [CONTRIBUTING](packages/stack/CONTRIBUTING.md) + [SECURITY](packages/stack/SECURITY.md)

## Releases

Packages release independently — they have their own PyPI entries, their own version numbers, their own tags. Release process per package is documented in its `CONTRIBUTING.md`.

The meta-package (`getbased-agent-stack`) bumps only when a sibling protocol change requires coordinated install. See [packages/stack/CONTRIBUTING.md](packages/stack/CONTRIBUTING.md#when-to-bump-this-repo).

## Repo history

This repo was formed by merging three previously-separate repos. History is preserved via `git subtree add`:

- `elkimek/getbased-mcp` → `packages/mcp/` (archived)
- `elkimek/getbased-rag` → `packages/rag/` (archived)
- `elkimek/getbased-agent-stack` → `packages/stack/` + root scaffolding (renamed to this repo)

PyPI package names stay the same — the merge is repo-layout only.

## Licence

GPL-3.0-only, consistent across all three packages.
