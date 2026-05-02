# Contributing to getbased-mcp

## Dev setup

```bash
git clone https://github.com/elkimek/getbased-agents
cd getbased-agents/packages/mcp
cd getbased-mcp
uv sync --extra test
uv run pytest
```

## Tests

Two layers:

- **Unit tests** (`tests/test_tools.py`) — every tool exercised against mocked HTTP with `respx`. Fast, hermetic, ~2 s. Runs in CI on every push + PR.
- **Smoke test** (`scripts/smoke.py`) — manual integration against a live Lens server + optional sync gateway. Uses real environment variables, exercises the real transport. Not in CI.

The cross-repo integration test (MCP ↔ RAG ↔ sync gateway) lives in the [getbased-agent-stack](https://github.com/elkimek/getbased-agents/tree/main/packages/stack) meta-package — running `uv run pytest` there starts a real `lens serve` subprocess and calls every MCP tool against it.

Every new tool should get:
1. A unit test in `tests/test_tools.py` (happy path + at least one error path)
2. A step in `scripts/smoke.py` that exercises it against a real server

## Releases

1. Bump `version` in `pyproject.toml`
2. Update the README tool table if tools changed
3. Open a PR — CI runs tests on Python 3.10/3.11/3.12
4. Merge, then:
   ```bash
   git tag -a v0.X.0 -m "v0.X.0 — summary"
   git push origin v0.X.0
   ```
5. Bump the matching pin in [getbased-agent-stack](https://github.com/elkimek/getbased-agents/tree/main/packages/stack)'s `pyproject.toml` so the meta-package pulls the new MCP

## Protocol compatibility

The MCP doesn't own the RAG protocol — it's a client. If the `getbased-rag` server adds new endpoints (e.g. a new library management op), tool coverage goes:

1. Add the new endpoint to the RAG server + tests
2. Add an MCP tool that calls the endpoint + unit test with `respx`
3. Update the smoke script in both repos + the integration test in agent-stack
4. Bump both sibling versions in lockstep, then bump the meta

Drift between the two is the most common source of "it worked locally but not in the preview" bugs. The `respx`-mocked unit tests are the first line of defence; the integration test in agent-stack is the second.

## Style

- `async def` for anything that touches the network, plain functions for parsing
- Every helper returns `{"error": ...}` on failure — tool callsites forward errors to the MCP client verbatim so users see actionable messages
- No exceptions escape into the MCP transport — every tool has a try/except shell

## Licence

AGPL-3.0-or-later.
