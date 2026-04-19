"""Knowledge tab API — thin proxy to getbased-rag.

We don't replicate rag's data model here; we just forward the browser's
bearer-authed requests to rag's endpoints, with one layer of timeout +
error normalisation so the frontend sees consistent JSON shapes.

All endpoints require the dashboard bearer token (same value as rag's).
Dashboard validates the browser's token, then uses the same key to
authenticate its upstream call to rag.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile

from ..config import DashboardConfig
from ..server import _require_auth

_KNOWLEDGE_TIMEOUT = 30.0
_INGEST_TIMEOUT = 300.0  # real-model ingest can run minutes on large files


def _cfg(request: Request) -> DashboardConfig:
    return request.app.state.config


async def _proxy_json(
    request: Request,
    method: str,
    path: str,
    json_body: dict | None = None,
    timeout: float = _KNOWLEDGE_TIMEOUT,
):
    """Forward a JSON call to rag with the dashboard's bearer key.
    Normalises common failure modes into FastAPI HTTPException so the
    frontend sees uniform `{error: ...}` bodies."""
    cfg = _cfg(request)
    _require_auth(request, cfg)
    key = cfg.read_api_key()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(
                method,
                f"{cfg.lens_url}{path}",
                headers={"Authorization": f"Bearer {key}"},
                json=json_body,
            )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"rag server not reachable at {cfg.lens_url}",
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"rag request failed: {e}")
    if r.status_code >= 400:
        # Bubble rag's error body (already JSON with an `error` key per
        # rag's exception handler) with its status code.
        try:
            body = r.json()
        except ValueError:
            body = {"error": r.text or f"rag returned {r.status_code}"}
        raise HTTPException(status_code=r.status_code, detail=body.get("error") or body)
    return r.json() if r.content else {}


def register(app: FastAPI) -> None:
    router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

    @router.get("/libraries")
    async def list_libraries(request: Request):
        return await _proxy_json(request, "GET", "/libraries")

    @router.post("/libraries")
    async def create_library(request: Request, body: dict):
        # Forward the body verbatim — rag's LibraryCreateRequest handles
        # validation. Wrapping with our own pydantic model would add no
        # value and would drift when rag changes its schema.
        return await _proxy_json(request, "POST", "/libraries", json_body=body)

    @router.post("/libraries/{library_id}/activate")
    async def activate_library(request: Request, library_id: str):
        return await _proxy_json(
            request, "POST", f"/libraries/{library_id}/activate"
        )

    @router.patch("/libraries/{library_id}")
    async def rename_library(request: Request, library_id: str, body: dict):
        return await _proxy_json(
            request, "PATCH", f"/libraries/{library_id}", json_body=body
        )

    @router.delete("/libraries/{library_id}")
    async def delete_library(request: Request, library_id: str):
        return await _proxy_json(request, "DELETE", f"/libraries/{library_id}")

    @router.post("/search")
    async def search(request: Request, body: dict):
        """Proxy to rag's /query. Frontend sends {query, top_k}; we stitch
        on the protocol version so the UI stays simpler."""
        payload = {
            "version": 1,
            "query": body.get("query", ""),
            "top_k": int(body.get("top_k", 5)),
        }
        return await _proxy_json(request, "POST", "/query", json_body=payload)

    @router.get("/stats")
    async def stats(request: Request):
        return await _proxy_json(request, "GET", "/stats")

    @router.delete("/sources")
    async def clear_sources(request: Request):
        return await _proxy_json(request, "DELETE", "/sources")

    @router.delete("/sources/{source:path}")
    async def delete_source(request: Request, source: str):
        return await _proxy_json(request, "DELETE", f"/sources/{source}")

    @router.post("/ingest")
    async def ingest(
        request: Request,
        files: list[UploadFile] = File(...),
    ):
        """Forward a multipart upload to rag's /ingest. We can't use the
        JSON proxy here — upload body is streamed multipart/form-data, and
        httpx's `files=` wants (name, content, mimetype) tuples, not
        UploadFile objects. Read each upload fully into memory (bounded
        by rag's LENS_MAX_INGEST_BYTES cap), then forward as a single
        multipart request."""
        cfg = _cfg(request)
        _require_auth(request, cfg)
        key = cfg.read_api_key()

        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")

        # Read all uploads into memory. Fine for typical doc sizes;
        # streaming forward would be nicer but adds complexity for
        # little gain at the scale this dashboard targets (single user,
        # self-hosted, docs not videos).
        multipart: list[tuple[str, tuple[str, bytes, str]]] = []
        for upload in files:
            data = await upload.read()
            multipart.append(
                (
                    "files",
                    (
                        upload.filename or "upload",
                        data,
                        upload.content_type or "application/octet-stream",
                    ),
                )
            )

        try:
            async with httpx.AsyncClient(timeout=_INGEST_TIMEOUT) as client:
                r = await client.post(
                    f"{cfg.lens_url}/ingest",
                    headers={"Authorization": f"Bearer {key}"},
                    files=multipart,
                )
        except httpx.ConnectError:
            raise HTTPException(
                status_code=502,
                detail=f"rag server not reachable at {cfg.lens_url}",
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"ingest request failed: {e}")

        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"error": r.text or f"rag returned {r.status_code}"}
            raise HTTPException(
                status_code=r.status_code, detail=body.get("error") or body
            )
        return r.json() if r.content else {}

    app.include_router(router)
