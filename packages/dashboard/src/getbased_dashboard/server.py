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

import platform as _platform
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__ as _PKG_VERSION
from .config import DashboardConfig

_WEB_DIR = Path(__file__).parent / "web"


def _require_auth(request: Request, config: DashboardConfig) -> None:
    """Bearer check — same key rag + mcp use. Matches against the file
    on disk, not a cached copy, so rotating the key doesn't require a
    dashboard restart. Uses secrets.compare_digest to avoid timing-based
    leakage — matches the pattern rag already uses."""
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
    token = header[len("Bearer ") :].strip()
    if not secrets.compare_digest(token, key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def create_app(config: DashboardConfig | None = None) -> FastAPI:
    """Build a fresh FastAPI app. Tests pass a custom config; normal
    startup uses DashboardConfig.from_env()."""
    cfg = config or DashboardConfig.from_env()
    app = FastAPI(
        title="getbased-dashboard",
        description="Web UI for getbased-agents.",
        version=_PKG_VERSION,
    )
    app.state.config = cfg

    # Normalise error envelopes to `{error: <string>}` so the frontend has
    # one response shape to parse. rag uses the same convention; without
    # this handler the dashboard would ship FastAPI's default
    # `{detail: ...}` shape — and when detail is a list (Pydantic
    # validation errors), `new Error(err.detail)` in the browser would
    # render as "[object Object]".
    def _flatten_detail(detail) -> str:
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            # Pydantic / FastAPI validation errors are a list of dicts with
            # `msg` + `loc` fields. Concatenate into a human-readable line.
            parts: list[str] = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", []))
                    msg = item.get("msg", "invalid")
                    parts.append(f"{loc}: {msg}" if loc else msg)
                else:
                    parts.append(str(item))
            return "; ".join(parts) or "validation failed"
        if isinstance(detail, dict):
            # FastAPI's default validation envelope is {"detail": [{...}]}.
            # When we proxy a 422 from rag (which doesn't register its own
            # RequestValidationError handler), that whole dict lands here
            # as `exc.detail`. Recurse one level into the `detail` key so
            # the user sees "body.query: String should have at least 1
            # character" instead of a Python-repr'd dict.
            if "detail" in detail:
                return _flatten_detail(detail["detail"])
            if "error" in detail:
                return _flatten_detail(detail["error"])
            if "msg" in detail:
                return str(detail["msg"])
        return str(detail)

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(_: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": _flatten_detail(exc.detail)},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": _flatten_detail(exc.errors())},
        )

    @app.get("/api/health")
    async def health() -> dict:
        """Unauthenticated liveness check — returns whether we can see
        an API key on disk. Useful for the frontend's first-load 'does
        rag look wired up?' state before the user has entered a key."""
        return {
            "ok": True,
            "version": _PKG_VERSION,
            "lens_url": cfg.lens_url,
            "has_api_key": bool(cfg.read_api_key()),
            "api_key_file": str(cfg.api_key_file),
            # OS hint so the frontend can show platform-appropriate
            # absolute paths in the MCP config filename hints — users
            # otherwise have to guess where `claude_desktop_config.json`
            # actually lives on their system.
            "platform": _platform.system().lower(),
        }

    @app.get("/api/auth/check")
    async def auth_check(request: Request) -> dict:
        """Authenticated probe the UI calls after the user enters a key,
        to confirm the key matches before enabling the rest of the UI."""
        _require_auth(request, cfg)
        return {"ok": True}

    @app.get("/api/auth/api-key")
    async def reveal_api_key(request: Request) -> dict:
        """Return the rag API key in plaintext so the UI can surface a
        show/copy affordance. Authed — the caller is already holding
        the key (they typed it at the auth gate), so this isn't an
        escalation; it's a convenience for pasting the same key into
        the PWA's External server field or a client MCP config.

        The key is read fresh from disk per request, matching the
        bearer-check pattern. If the user rotates the file without a
        restart, the revealed value tracks the current on-disk secret."""
        _require_auth(request, cfg)
        return {
            "api_key": cfg.read_api_key(),
            "api_key_file": str(cfg.api_key_file),
        }

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
