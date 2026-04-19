"""FastAPI app — static UI at `/`, JSON API at `/api/*`, bearer auth on
every `/api/*` route. The static UI is served without auth so the user
can load the HTML + JS and enter their key; the JS then sends the key
with every subsequent request.

We bind to 127.0.0.1 by default. Exposing the dashboard to a LAN means
the same bearer is the only thing between anyone on that network and
the user's knowledge base — override DASHBOARD_HOST=0.0.0.0 only if you
know what you're doing.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import DashboardConfig

_WEB_DIR = Path(__file__).parent / "web"


def _require_auth(request: Request, config: DashboardConfig) -> None:
    """Bearer check — same key rag + mcp use. Matches against the file
    on disk, not a cached copy, so rotating the key doesn't require a
    dashboard restart."""
    key = config.read_api_key()
    if not key:
        raise HTTPException(
            status_code=503,
            detail=(
                "No API key found. Start getbased-rag to generate one, "
                f"or set LENS_API_KEY_FILE. Expected: {config.api_key_file}"
            ),
        )
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if header[len("Bearer ") :].strip() != key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def create_app(config: DashboardConfig | None = None) -> FastAPI:
    """Build a fresh FastAPI app. Tests pass a custom config; normal
    startup uses DashboardConfig.from_env()."""
    cfg = config or DashboardConfig.from_env()
    app = FastAPI(
        title="getbased-dashboard",
        description="Web UI for getbased-agents.",
        version="0.1.0",
    )
    app.state.config = cfg

    @app.get("/api/health")
    async def health() -> dict:
        """Unauthenticated liveness check — returns whether we can see
        an API key on disk. Useful for the frontend's first-load 'does
        rag look wired up?' state before the user has entered a key."""
        return {
            "ok": True,
            "version": "0.1.0",
            "lens_url": cfg.lens_url,
            "has_api_key": bool(cfg.read_api_key()),
            "api_key_file": str(cfg.api_key_file),
        }

    @app.get("/api/auth/check")
    async def auth_check(request: Request) -> dict:
        """Authenticated probe the UI calls after the user enters a key,
        to confirm the key matches before enabling the rest of the UI."""
        _require_auth(request, cfg)
        return {"ok": True}

    # Register per-tab API routers. Imported here (inside the factory) so
    # the dashboard doesn't pay the import cost of, say, httpx+multipart
    # when a test builds a bare app to probe auth-only endpoints.
    from .api import activity as activity_api
    from .api import knowledge as knowledge_api
    from .api import mcp as mcp_api

    knowledge_api.register(app)
    mcp_api.register(app)
    activity_api.register(app)

    # Static UI — mount last so it doesn't shadow /api/*.
    if _WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")
    else:
        # In dev installs from source the web/ dir may be empty. Fall
        # back to a placeholder so /api/* still works and the browser
        # sees a clear message instead of a 500.
        @app.get("/")
        async def _placeholder() -> JSONResponse:
            return JSONResponse(
                {"error": "web/ assets not built into this install"},
                status_code=501,
            )

    return app
