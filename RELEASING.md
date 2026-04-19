# Releasing

This monorepo publishes four packages to PyPI:

- `getbased-mcp` — MCP adapter (stdio ↔ HTTP)
- `getbased-rag` — RAG knowledge server
- `getbased-dashboard` — web UI
- `getbased-agent-stack` — meta-package bundling the three above

Publishing is automated via `.github/workflows/publish.yml`, triggered by
git tags. You do **not** need to run `uv publish` by hand — tag-push is
the release trigger.

## Cutting a release

1. **Bump the version** in the package(s) you're releasing.

   ```bash
   # Example: dashboard bug fix
   sed -i 's/^version = "0.6.0"/version = "0.6.1"/' packages/dashboard/pyproject.toml
   ```

   The dashboard reads its version from installed-package metadata
   (`importlib.metadata`), so bumping `pyproject.toml` propagates to
   `/api/health`, the FastAPI OpenAPI metadata, and the MCP
   `clientInfo.version` automatically. Other packages are simpler
   hard-coded strings for now.

   If you're releasing the meta `getbased-agent-stack`, **also** bump
   any sibling minimum-version constraints in
   `packages/stack/pyproject.toml` if they changed.

2. **Commit the bump** and push to `main` (tests will run on PR or
   direct push via `.github/workflows/test.yml`).

   ```bash
   git commit -am "release: dashboard 0.6.1"
   git push origin main
   ```

3. **Tag and push the tag.** The workflow matches `v*` and `*-v*` so
   either scheme works:

   ```bash
   git tag v0.6.1                 # global-looking — simplest for meta releases
   git tag dashboard-v0.6.1       # per-package — reads well across several in a row
   git push origin <tag>
   ```

4. **Watch the workflow** at
   https://github.com/elkimek/getbased-agents/actions/workflows/publish.yml
   (or `gh run watch --exit-status`). It builds all four packages into
   per-project dist dirs and runs four upload steps. Any package whose
   version already exists on PyPI hits `skip-existing` and is a no-op;
   any newly-bumped package lands a wheel.

That's it. No manual `twine upload`, no tokens in your shell, no
`.pypirc` to keep in sync.

## If an upload step fails

### `403 Invalid API Token: OIDC scoped token is not valid for project 'X'`

The project's PyPI Trusted Publisher entry is missing or misconfigured.
Visit `https://pypi.org/manage/project/<name>/settings/publishing/` and
confirm an entry exists with **exactly** these values:

| Field | Value |
|---|---|
| Owner | `elkimek` |
| Repository name | `getbased-agents` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

The most common mistake is pasting the full path
`.github/workflows/publish.yml` or the display name `Publish to PyPI`
into the **Workflow name** field. PyPI wants just `publish.yml`.

If an entry is listed under "Pending trusted publishers" instead of
"Trusted publishers", the project already exists on PyPI and you need
to re-register it as a non-pending publisher (pending entries only fire
once, to create a project that didn't exist yet).

### A step uploaded but `skip-existing` says the version already exists

Expected and harmless. If you meant to publish a new version, you forgot
to bump `pyproject.toml`. Bump it and cut a new tag.

### The workflow didn't fire at all

The tag pattern is `v*` or `*-v*`. Patterns like `release-2026-04-19` or
`v0.6.1-rc1` work (`v*` matches). A tag named `0.6.1` (no `v` prefix)
doesn't match either pattern — add `v` or push it as `foo-v0.6.1`.

## Prerequisites that are already set up

These were wired once and shouldn't need touching:

- **Trusted Publishers on PyPI** for all four projects (see table above)
- **GitHub Actions workflow** at `.github/workflows/publish.yml`
- **`pypi` environment on the repo** — auto-created on first workflow
  run. You can add protection rules (e.g. manual approval, branch
  restriction) at
  https://github.com/elkimek/getbased-agents/settings/environments/pypi

## Why per-project dist subdirs

The publish step uses per-project subdirs (`dist-mcp/`, `dist-rag/`, …)
rather than a single `dist/`. This is deliberate: Trusted Publishers
scope each OIDC → PyPI token handshake to one project. If you upload a
`dist/` containing multiple projects' wheels, PyPI rejects the ones that
don't match the current step's target project with a `400 File already
exists` or a 403. Per-project dirs keep every upload single-project.
