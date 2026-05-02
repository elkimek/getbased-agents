# getbased-agents

Monorepo for the [getbased](https://getbased.health) agent ecosystem — MCP server, RAG backend, web dashboard, and a meta-package that wires them together.

| Package | PyPI | Role | Contents |
|---|---|---|---|
| [`getbased-mcp`](packages/mcp/) | [`getbased-mcp`](https://pypi.org/project/getbased-mcp/) | MCP adapter for Claude Code / Hermes / OpenClaw / any MCP client | stdio ↔ HTTP |
| [`getbased-rag`](packages/rag/) | [`getbased-rag`](https://pypi.org/project/getbased-rag/) | Local RAG knowledge server. Also the PWA's "External server" Knowledge Base backend | FastAPI + Qdrant + MiniLM/BGE |
| [`getbased-dashboard`](packages/dashboard/) | [`getbased-dashboard`](https://pypi.org/project/getbased-dashboard/) | Browser UI: manage knowledge libraries, generate MCP client configs, see agent activity | FastAPI + vanilla JS |
| [`getbased-agent-stack`](packages/stack/) | [`getbased-agent-stack`](https://pypi.org/project/getbased-agent-stack/) | Meta-package pinning all three siblings | thin CLI + systemd unit + example configs |

```
Claude Code / Hermes / OpenClaw           Browser
        │ MCP (stdio)                       │ HTTP
        ▼                                   ▼
  getbased-mcp                    getbased-dashboard  (localhost:8323)
   │        │                        │             │
   │ HTTP   │ HTTP                   │ proxies     │ spawns stdio
   ▼        ▼                        ▼             ▼
sync GW   getbased-rag  ◄──────────┘         getbased-mcp
          (localhost:8322)
```

## Install

Linux, zero-friction — `curl | bash`:

```bash
curl -sSL https://getbased.health/install.sh | bash
```

The script auto-detects `uv` or `pipx` (install either one first if you have neither), installs `getbased-agent-stack[full]` with sibling binaries exposed, runs `getbased-stack init --yes`, and starts `getbased-rag` + `getbased-dashboard` as systemd user services. [Read it first](https://github.com/elkimek/get-based-site/blob/main/install.sh) if you're cautious — `curl -sSL https://getbased.health/install.sh.sha256 | sha256sum -c` verifies the published hash.

Manual install (macOS, Windows, WSL1, or if you'd rather not run a shell script):

```bash
# pipx — --include-deps exposes lens, getbased-dashboard, getbased-mcp on PATH
pipx install --include-deps "getbased-agent-stack[full]"

# or uv (0.11+)
uv tool install \
  --with-executables-from getbased-rag \
  --with-executables-from getbased-dashboard \
  --with-executables-from getbased-mcp \
  "getbased-agent-stack[full]"
```

Or pick the piece you actually need:

```bash
pipx install getbased-mcp            # agents for lab data only, no RAG  (~10 MB)
pipx install "getbased-rag[full]"    # RAG backend for the PWA, no agents (~500 MB)
pipx install getbased-dashboard      # web UI; pulls the MCP dep alongside it
```

## Quickstart (manual)

```bash
pipx install --include-deps "getbased-agent-stack[full]"
getbased-stack init --yes                  # non-interactive: token skipped, API key
                                           # auto-generated, systemd units installed
                                           # and started. Drop --yes for the wizard.
getbased-stack mcp-config claude-desktop   # paste the snippet into your MCP client
```

`--include-deps` exposes `getbased-mcp`, `lens`, and `getbased-dashboard` alongside `getbased-stack` on your PATH — without it, pipx hides the sibling binaries inside the venv. See [`packages/stack/README.md`](packages/stack/README.md) for the full flow including linger-for-headless and token rotation.

## Development

```bash
git clone https://github.com/elkimek/getbased-agents
cd getbased-agents
uv sync --all-packages --all-extras
```

Each package runs its own tests from its own directory:

```bash
(cd packages/mcp && uv run pytest)       # 33 unit tests, respx-mocked HTTP
(cd packages/rag && uv run pytest)       # 51 tests, FastAPI TestClient + fake embedder
(cd packages/dashboard && uv run pytest) # 64 tests, respx-mocked rag + real-subprocess MCP probe
(cd packages/stack && uv run pytest)     # 2 integration tests: real lens subprocess + real MCP tool calls
```

CI runs the same matrix on Python 3.10/3.11/3.12 (unit) + 3.12 (integration) on every push and PR.

Per-package details:
- [packages/mcp/README.md](packages/mcp/README.md) + [CONTRIBUTING](packages/mcp/CONTRIBUTING.md) + [SECURITY](packages/mcp/SECURITY.md)
- [packages/rag/README.md](packages/rag/README.md) + [CONTRIBUTING](packages/rag/CONTRIBUTING.md) + [SECURITY](packages/rag/SECURITY.md)
- [packages/dashboard/README.md](packages/dashboard/README.md)
- [packages/stack/README.md](packages/stack/README.md) + [CONTRIBUTING](packages/stack/CONTRIBUTING.md) + [SECURITY](packages/stack/SECURITY.md)

## Releases

All four packages publish to PyPI automatically on tag push. Bump a version, commit, tag with `vX.Y.Z` or `<pkg>-vX.Y.Z`, and push the tag — the [publish workflow](.github/workflows/publish.yml) builds every package, uploads the bumped ones, and `skip-existing`s the rest.

Full step-by-step in [RELEASING.md](RELEASING.md). Meta-package bump policy is in [packages/stack/CONTRIBUTING.md](packages/stack/CONTRIBUTING.md#when-to-bump-this-repo).

## Repo history

This repo was formed by merging three previously-separate repos. History is preserved via `git subtree add`:

- `elkimek/getbased-mcp` → `packages/mcp/` (archived)
- `elkimek/getbased-rag` → `packages/rag/` (archived)
- `elkimek/getbased-agent-stack` → `packages/stack/` + root scaffolding (renamed to this repo)

`packages/dashboard/` is new in this repo, not inherited from an archive.

PyPI package names stay the same — the merge is repo-layout only.

## Licence

AGPL-3.0-or-later, consistent across all four packages. See [LICENSE](LICENSE).

If you run a modified version of any of these services on a network, AGPLv3 §13 requires you to offer your users the corresponding source.
