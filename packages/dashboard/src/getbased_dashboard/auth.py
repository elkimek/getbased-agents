"""Shared dashboard authentication helpers.

Kept outside ``server.py`` so API routers can import the bearer check without
creating an import cycle with the app factory that registers those routers.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from .config import DashboardConfig


def require_auth(request: Request, config: DashboardConfig) -> None:
    """Validate the dashboard bearer token against the current API key file.

    The key is read from disk on every request so rotation does not require a
    dashboard restart. ``compare_digest`` avoids timing-based token leakage.
    """
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
