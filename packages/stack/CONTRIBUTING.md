# Contributing to getbased-agent-stack

This repo is a thin meta-package — most of the interesting code lives in the two siblings:

- [getbased-mcp](https://github.com/elkimek/getbased-agents/tree/main/packages/mcp) — the MCP adapter (stdio protocol ↔ HTTP)
- [getbased-rag](https://github.com/elkimek/getbased-agents/tree/main/packages/rag) — the RAG backend (FastAPI + Qdrant + embedders)

If the bug is in a tool definition or an HTTP endpoint, contribute there. This repo's job is coordinating versions, shipping example configs, and catching cross-repo protocol drift.

## Dev setup

```bash
git clone https://github.com/elkimek/getbased-agents
cd getbased-agents/packages/stack
cd getbased-agent-stack
uv sync --extra test --extra full
uv run pytest
```

The integration test starts a real `lens serve` subprocess, ingests a fixture document, and exercises every MCP tool end-to-end. First run downloads MiniLM (~90 MB); subsequent runs are cached and take ~10 s.

## When to bump this repo

This meta only needs a release when **one of these is true**:

1. A sibling bumps its major and this meta's pins need to follow
2. A new sibling is added to the stack (new integration points)
3. The systemd unit, example configs, or README structure changes
4. Security-relevant metadata (supply chain, license posture) shifts

For normal feature work in the siblings, **don't bump here.** The `>=x.y.0` pins already pick up compatible releases, and bumping the meta for every sibling release creates release-note noise.

## Release process

1. Update `pyproject.toml` pins if a sibling's `>=` minimum moved
2. Bump `version` in `pyproject.toml` and `src/getbased_agent_stack/__init__.py`
3. Update the version-compatibility table in README if it changed
4. Open a PR — CI runs the full integration test
5. Merge, then:
   ```bash
   git tag -a v0.X.0 -m "v0.X.0 — summary"
   git push origin v0.X.0
   ```

## Testing protocol drift

The scenario this repo exists to catch: **sibling A adds a new endpoint, sibling B doesn't know about it yet**. The integration test covers it by round-tripping every tool against a real server.

If you add a new tool to `getbased-mcp`, the integration test should exercise it here — otherwise drift can hide in "tool works against mock; real server 404s".

## Licence

GPL-3.0-only. Matches both siblings.
