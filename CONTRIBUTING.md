# Contributing to getbased-rag

## Dev setup

```bash
git clone https://github.com/elkimek/getbased-rag
cd getbased-rag
uv sync --extra test --extra full
uv run pytest
```

`--extra full` pulls the PDF/DOCX parsers and ONNX runtime so `lens ingest` can process real documents. Drop it if you only want to run server tests against text fixtures.

## Tests

`tests/test_server.py` — 18 tests against FastAPI TestClient with a deterministic fake embedder (no MiniLM download in CI) and an isolated Qdrant tmp dir. Covers:

- Public probes (`/` and `/health`)
- Auth failures (missing header, wrong scheme, wrong key)
- `/query` validation + happy path
- `/stats` empty + populated
- `/sources` delete-one + clear-all
- `/libraries` full CRUD lifecycle + isolation
- 404 paths

Runs in ~2 s on CI across Python 3.10/3.11/3.12.

The cross-repo integration test (real `lens serve` subprocess + MCP tool calls against it) lives in the [getbased-agent-stack](https://github.com/elkimek/getbased-agent-stack) meta-package.

Every new HTTP endpoint should get a unit test covering the happy path, the auth-failure path, and at least one error path (404, 400, or 500).

## Adding an embedding backend

1. Add a new `Embedder` subclass to `src/lens/embedder.py`
2. Extend `create_embedder()` to select it based on config
3. Add the package to a new `[project.optional-dependencies]` group
4. Add a `conftest.py` fixture that stubs out any heavy startup cost

## Releases

1. Bump `version` in `pyproject.toml`
2. Update the HTTP API table in the README if endpoints changed
3. Open a PR — CI runs tests on Python 3.10/3.11/3.12
4. Merge, then:
   ```bash
   git tag -a v0.X.0 -m "v0.X.0 — summary"
   git push origin v0.X.0
   ```
5. Bump the matching pin in [getbased-agent-stack](https://github.com/elkimek/getbased-agent-stack)'s `pyproject.toml`
6. If the HTTP protocol changed incompatibly, also bump `getbased-mcp` and the PWA's Knowledge Base external-server client

## Protocol compatibility

The `/query`, `/stats`, `/libraries` contracts are shared with:

- The getbased PWA's "External server" Knowledge Base backend (see `docs/guide/interpretive-lens.md` in the main repo)
- [getbased-mcp](https://github.com/elkimek/getbased-mcp)'s `knowledge_*` tools

When changing request/response shapes, bump the `version` field in the request model and keep the old path supported for at least one release cycle so clients can migrate.

## Style

- FastAPI dependencies preferred over globals where possible — the existing `embedder_holder` / `backend_holder` closure pattern is legacy; new endpoints should lean on Dependencies
- `require_auth(authorization)` as a pre-hook on every non-public endpoint
- Every endpoint has a docstring describing what it returns; shapes live in Pydantic models

## Licence

GPL-3.0-only.
