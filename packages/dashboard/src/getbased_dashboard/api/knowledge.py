"""Knowledge tab API — thin proxy to getbased-rag.

We don't replicate rag's data model here; we just forward the browser's
bearer-authed requests to rag's endpoints, with one layer of timeout +
error normalisation so the frontend sees consistent JSON shapes.

All endpoints require the dashboard bearer token (same value as rag's).
Dashboard validates the browser's token, then uses the same key to
authenticate its upstream call to rag.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from ..config import DashboardConfig
from ..server import _require_auth

_KNOWLEDGE_TIMEOUT = 30.0
_INGEST_TIMEOUT = 300.0  # real-model ingest can run minutes on large files

# Size cap enforced at the dashboard hop. Mirrors rag's default so users
# see one consistent ceiling regardless of which layer catches an oversize
# upload. Configurable via env so power users with large corpora can
# raise it on both services deliberately. Note: rag has its own cap that
# ultimately bounds on-disk writes — this dashboard-side cap prevents
# full buffering into RAM before we reach rag.
_MAX_INGEST_BYTES = int(
    os.environ.get("DASHBOARD_MAX_INGEST_BYTES", str(256 * 1024 * 1024))
)
# Stream chunk size — 64 KB is plenty for network forwarding and keeps
# per-request memory predictable.
_STREAM_CHUNK = 64 * 1024


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

    @router.get("/info")
    async def info(request: Request):
        """Proxy rag's /info for the Knowledge tab's engine badge."""
        return await _proxy_json(request, "GET", "/info")

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
        """Forward a multipart upload to rag's /ingest.

        Streams the upload to a temp file in chunks, enforcing a byte
        cap as we read — keeps the dashboard from buffering multi-GB
        uploads into RAM while letting rag see the size quickly.
        Filenames are basename-sanitised at the dashboard layer too.

        When the browser sends `Accept: application/x-ndjson`, we open
        a streaming request to rag and pipe each progress line through
        as it arrives. Otherwise: single-shot JSON.
        """
        cfg = _cfg(request)
        _require_auth(request, cfg)
        key = cfg.read_api_key()

        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")

        # Create tempdir without a `with` block — streaming needs the
        # directory to outlive the handler return. Cleanup is explicit,
        # either at end-of-handler (single-shot path) or in the generator's
        # finally (streaming path).
        tmpdir = tempfile.mkdtemp(prefix="gbd-ingest-")
        tmp_path = Path(tmpdir)

        def _cleanup_tmpdir() -> None:
            import shutil as _shutil

            _shutil.rmtree(tmpdir, ignore_errors=True)

        try:
            total_bytes = 0
            saved: list[tuple[str, Path, str]] = []

            for upload in files:
                raw_name = (upload.filename or "").replace("\\", "/")
                safe_name = os.path.basename(raw_name)
                if not safe_name or safe_name in (".", ".."):
                    continue
                dest = tmp_path / safe_name
                with dest.open("wb") as out:
                    while True:
                        chunk = await upload.read(_STREAM_CHUNK)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > _MAX_INGEST_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail=f"Upload exceeds {_MAX_INGEST_BYTES} bytes",
                            )
                        out.write(chunk)
                saved.append(
                    (
                        safe_name,
                        dest,
                        upload.content_type or "application/octet-stream",
                    )
                )

            if not saved:
                raise HTTPException(status_code=400, detail="No valid files in upload")
        except HTTPException:
            _cleanup_tmpdir()
            raise
        except Exception:
            _cleanup_tmpdir()
            raise

        accept = (request.headers.get("accept") or "").lower()
        want_stream = "application/x-ndjson" in accept

        if want_stream:
            async def _pipe_stream():
                """Pipe rag's NDJSON stream through to the browser. Each
                line from rag's `r.aiter_raw()` yields as it arrives —
                the browser sees progress events live instead of waiting
                for ingest to complete. Temp dir + file handles are kept
                alive here and cleaned in the finally."""
                fhs: list = []
                multipart: list[tuple[str, tuple[str, object, str]]] = []
                client = httpx.AsyncClient(timeout=_INGEST_TIMEOUT)
                try:
                    for name, disk_path, mime in saved:
                        fh = disk_path.open("rb")
                        fhs.append(fh)
                        multipart.append(("files", (name, fh, mime)))

                    try:
                        async with client.stream(
                            "POST",
                            f"{cfg.lens_url}/ingest",
                            headers={
                                "Authorization": f"Bearer {key}",
                                "Accept": "application/x-ndjson",
                            },
                            files=multipart,
                        ) as r:
                            if r.status_code >= 400:
                                body_bytes = b""
                                async for c in r.aiter_bytes():
                                    body_bytes += c
                                    if len(body_bytes) > 16 * 1024:
                                        break
                                try:
                                    body_obj = json.loads(body_bytes.decode())
                                except Exception:
                                    body_obj = {
                                        "error": body_bytes.decode(errors="replace")
                                        or f"rag returned {r.status_code}"
                                    }
                                msg = (
                                    body_obj.get("error")
                                    if isinstance(body_obj, dict)
                                    else str(body_obj)
                                ) or f"rag returned {r.status_code}"
                                yield (
                                    json.dumps(
                                        {
                                            "event": "error",
                                            "message": msg,
                                            "status": r.status_code,
                                        }
                                    )
                                    + "\n"
                                ).encode()
                                return
                            # Pass through each chunk verbatim — rag
                            # emits one JSON object per line.
                            async for c in r.aiter_raw():
                                if c:
                                    yield c
                    except httpx.ConnectError:
                        yield (
                            json.dumps(
                                {
                                    "event": "error",
                                    "message": f"rag server not reachable at {cfg.lens_url}",
                                }
                            )
                            + "\n"
                        ).encode()
                    except httpx.RequestError as e:
                        yield (
                            json.dumps(
                                {
                                    "event": "error",
                                    "message": f"ingest request failed: {e}",
                                }
                            )
                            + "\n"
                        ).encode()
                finally:
                    await client.aclose()
                    for fh in fhs:
                        try:
                            fh.close()
                        except Exception:
                            pass
                    _cleanup_tmpdir()

            return StreamingResponse(
                _pipe_stream(), media_type="application/x-ndjson"
            )

        # Default: single-shot JSON path — open handles, POST, close,
        # return the summary dict.
        try:
            fhs: list = []
            multipart: list[tuple[str, tuple[str, object, str]]] = []
            try:
                for name, disk_path, mime in saved:
                    fh = disk_path.open("rb")
                    fhs.append(fh)
                    multipart.append(("files", (name, fh, mime)))

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
                    raise HTTPException(
                        status_code=502, detail=f"ingest request failed: {e}"
                    )
            finally:
                for fh in fhs:
                    try:
                        fh.close()
                    except Exception:
                        pass

            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = {"error": r.text or f"rag returned {r.status_code}"}
                raise HTTPException(
                    status_code=r.status_code, detail=body.get("error") or body
                )
            return r.json() if r.content else {}
        finally:
            _cleanup_tmpdir()

    app.include_router(router)
