"""FastAPI HTTP server implementing the Lens RAG endpoint contract.

Endpoints:
  POST /query              — bearer-auth'd RAG search, returns top-k passages
  GET  /stats              — active library: per-source chunk counts
  DELETE /sources/{source} — active library: drop one source
  DELETE /sources          — active library: drop everything

  GET  /libraries          — list libraries + active id
  POST /libraries          — create library
  POST /libraries/{id}/activate — set active
  PATCH /libraries/{id}    — rename
  DELETE /libraries/{id}   — delete (drops qdrant collection)

  GET  /health             — public health probe
  GET  /                   — public banner
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .api_key import get_or_create_api_key
from .config import LensConfig
from .embedder import create_embedder
from .registry import LEGACY_COLLECTION, Registry
from .store import QdrantBackend, Store

log = logging.getLogger("lens.server")


# Bounds: queries are single search terms / phrases — 4 KB is plenty for
# real use and caps a tokenizer-DoS at a fixed ceiling. Library names are
# short user-facing labels; 120 chars is generous.
class QueryRequest(BaseModel):
    version: int = 1
    query: str = Field(..., min_length=1, max_length=4096)
    top_k: int = Field(default=5, ge=1, le=100)


class Chunk(BaseModel):
    text: str
    source: str = ""
    score: Optional[float] = None


class QueryResponse(BaseModel):
    chunks: list[Chunk]


class LibraryCreateRequest(BaseModel):
    name: str = Field(default="Untitled", max_length=120)


class LibraryRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


def create_app(config: LensConfig) -> FastAPI:
    """Build the FastAPI app with config-driven dependencies."""
    config.ensure_dirs()
    api_key = get_or_create_api_key(config.api_key_file)
    embedder_holder: dict = {"obj": None}
    backend_holder: dict = {"obj": None}
    registry = Registry(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("Starting Lens server on %s:%d", config.host, config.port)
        log.info("Data dir: %s", config.data_dir)
        log.info("API key file: %s", config.api_key_file)
        # Bootstrap libraries. If the user has an existing "knowledge"
        # collection from pre-1.21 (single-library days), migrate its
        # contents into a fresh "Default" library so they don't lose
        # their indexed documents.
        _bootstrap_libraries()
        yield
        # Shutdown — nothing to clean up (Qdrant local closes on GC)

    def _bootstrap_libraries() -> None:
        state = registry.list()
        if state["libraries"]:
            return
        default_id = registry.ensure_default()
        # Check if legacy collection has data. If so, rename-migrate it into
        # the new library's collection name. We can't rename qdrant
        # collections directly, but on first migrate we can just keep the
        # legacy data discoverable — point the "Default" library at the
        # legacy collection name for one-time continuity.
        try:
            backend = _get_backend()
            names = backend.list_collection_names()
            if LEGACY_COLLECTION in names:
                legacy_store = Store(config, collection=LEGACY_COLLECTION, backend=backend)
                legacy_count = legacy_store.count()
                if legacy_count > 0:
                    # Copy all points from legacy → library collection.
                    new_collection = registry.collection_for(default_id)
                    log.info(
                        "Migrating %d legacy chunks → library %s (collection %s)",
                        legacy_count, default_id, new_collection,
                    )
                    _copy_collection(backend, LEGACY_COLLECTION, new_collection)
                    try:
                        backend.client().delete_collection(LEGACY_COLLECTION)
                        log.info("Dropped legacy collection %s", LEGACY_COLLECTION)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Dropping legacy collection failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("Library bootstrap migration failed: %s", e)

    def _copy_collection(backend: QdrantBackend, src: str, dst: str) -> None:
        from qdrant_client.models import Distance, PointStruct, VectorParams

        client = backend.client()
        info = client.get_collection(src)
        dim = int(info.config.params.vectors.size)
        try:
            client.get_collection(dst)
        except Exception:
            client.create_collection(
                collection_name=dst,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        offset = None
        total = 0
        while True:
            points, offset = client.scroll(
                collection_name=src,
                with_payload=True,
                with_vectors=True,
                limit=256,
                offset=offset,
            )
            if not points:
                break
            structs = [
                PointStruct(id=p.id, vector=p.vector, payload=p.payload or {})
                for p in points
            ]
            client.upsert(collection_name=dst, points=structs)
            total += len(structs)
            if offset is None:
                break
        log.info("Copied %d points from %s → %s", total, src, dst)

    app = FastAPI(
        title="getbased-lens",
        version="0.3.0",
        lifespan=lifespan,
    )
    # CORS: only the getbased PWA origins need browser access to this
    # server; the MCP and Hermes talk from Python, no CORS check at all.
    # Users running their own domain can add it via LENS_CORS_ORIGINS
    # (comma-separated). `*` was the old default; tightening here so a
    # random webpage visited by the user can't make authenticated
    # localhost requests with a leaked bearer token.
    _default_origins = [
        "https://getbased.health",
        "https://app.getbased.health",
        "http://localhost",
        "http://127.0.0.1",
    ]
    _extra_origins = [
        o.strip() for o in os.environ.get("LENS_CORS_ORIGINS", "").split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_default_origins + _extra_origins,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):[0-9]+$|^http://.*\.onion$",
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def _get_backend() -> QdrantBackend:
        if backend_holder["obj"] is None:
            backend_holder["obj"] = QdrantBackend(config)
        return backend_holder["obj"]

    def get_embedder():
        if embedder_holder["obj"] is None:
            log.info("Lazy-loading embedder…")
            embedder_holder["obj"] = create_embedder(config)
        return embedder_holder["obj"]

    def active_store() -> Store:
        """Return a Store bound to the currently active library's collection.

        Ensures at least one library exists. All user-facing data endpoints
        go through this; library-management endpoints use the registry
        directly."""
        registry.ensure_default()
        collection = registry.active_collection()
        return Store(config, collection=collection, backend=_get_backend())

    def require_auth(authorization: Optional[str]) -> None:
        if not authorization:
            raise HTTPException(401, "Missing Authorization header")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Authorization must be Bearer scheme")
        token = authorization.removeprefix("Bearer ").strip()
        # Constant-time comparison — the server is typically on localhost so
        # this is defensive rather than load-bearing, but secrets.compare_digest
        # costs nothing and rules out the class of bug entirely.
        if not secrets.compare_digest(token, api_key):
            raise HTTPException(401, "Invalid API key")

    @app.get("/")
    async def root():
        return {
            "name": "getbased-lens",
            "version": "0.3.0",
            "endpoints": [
                "/health", "/query", "/stats", "/sources/{source}",
                "/libraries",
            ],
        }

    @app.get("/stats")
    async def stats_endpoint(authorization: Optional[str] = Header(default=None)):
        """Per-source chunk counts for the active library."""
        require_auth(authorization)
        store = active_store()
        try:
            sources = store.list_sources()
            total = sum(int(s.get("chunks", 0)) for s in sources)
            return {"total_chunks": total, "documents": sources}
        except Exception:
            log.exception("Stats failed")
            raise HTTPException(500, "Stats failed — see server logs")

    @app.delete("/sources/{source:path}")
    async def delete_source_endpoint(
        source: str,
        authorization: Optional[str] = Header(default=None),
    ):
        """Delete every chunk for a given source in the active library."""
        require_auth(authorization)
        store = active_store()
        try:
            deleted = store.delete_by_source(source)
            return {"deleted_chunks": int(deleted)}
        except Exception:
            log.exception("Delete failed")
            raise HTTPException(500, "Delete failed — see server logs")

    @app.delete("/sources")
    async def clear_endpoint(authorization: Optional[str] = Header(default=None)):
        """Drop the active library's collection contents."""
        require_auth(authorization)
        store = active_store()
        try:
            cleared = store.clear()
            return {"deleted_chunks": int(cleared)}
        except Exception:
            log.exception("Clear failed")
            raise HTTPException(500, "Clear failed — see server logs")

    @app.get("/info")
    async def info_endpoint(authorization: Optional[str] = Header(default=None)):
        """Backend introspection: embedding engine, model, dimension,
        reranker state, active library + chunk count. Intended for UI
        "what's running" badges. Bearer-authed because the engine config
        is mildly interesting to an attacker (narrows down what's
        locally exploitable)."""
        require_auth(authorization)
        # Embedder — may be lazy-loaded. Ask for its info without
        # forcing a load; `loaded: false` is a useful UI signal on
        # first request.
        embedder = get_embedder()
        try:
            emb_info = embedder.info()
        except Exception:
            emb_info = {
                "engine": type(embedder).__name__,
                "model": config.embedding_model,
                "dimension": None,
                "loaded": False,
            }

        # Active library — avoid touching the embedder / store if not
        # already alive. We query via the registry + backend directly.
        active_library: dict = {}
        total_chunks = 0
        try:
            registry.ensure_default()
            state = registry.list()
            active_id = state.get("activeId", "")
            for lib in state.get("libraries", []):
                if lib.get("id") == active_id:
                    active_library = {
                        "id": lib.get("id"),
                        "name": lib.get("name"),
                    }
                    break
            if active_id:
                store = Store(
                    config,
                    collection=registry.collection_for(active_id),
                    backend=_get_backend(),
                )
                total_chunks = int(store.count())
        except Exception as e:  # noqa: BLE001
            log.debug("info: active-library probe failed: %s", e)

        return {
            "version": "0.3.0",
            "embedder": emb_info,
            "similarity_floor": config.similarity_floor,
            "reranker": bool(config.reranker),
            "max_chunks": config.max_chunks,
            "active_library": active_library,
            "active_chunks": total_chunks,
        }

    @app.get("/health")
    async def health():
        # Don't force-load the embedder for health — report what we know.
        # Don't fail the probe if the registry is empty (first-run): report
        # rag_ready=False and chunks=0 so the UI can still render its
        # "Set up engine" state.
        try:
            store = active_store()
            count = store.count()
            rag_ready = count > 0
        except Exception as e:
            log.warning("Health check store query failed: %s", e)
            count = 0
            rag_ready = False
        return {"status": "ok", "rag_ready": rag_ready, "chunks": count}

    @app.post("/query", response_model=QueryResponse)
    async def query_endpoint(
        req: QueryRequest,
        authorization: Optional[str] = Header(default=None),
    ):
        require_auth(authorization)

        if req.version != 1:
            raise HTTPException(400, f"Unsupported version: {req.version}")
        if not req.query or not req.query.strip():
            raise HTTPException(400, "Empty query")

        top_k = max(1, min(config.max_chunks, int(req.top_k)))
        embedder = get_embedder()
        store = active_store()

        # Encode query
        try:
            vectors = embedder.encode([req.query.strip()])
            qvec = vectors[0]
        except Exception:
            log.exception("Embedding failed")
            raise HTTPException(500, "Embedding failed — see server logs")

        # Search
        try:
            results = store.search(qvec, top_k=top_k, score_threshold=config.similarity_floor)
        except Exception:
            log.exception("Vector search failed")
            raise HTTPException(500, "Search failed — see server logs")

        # Truncate per response constraints
        chunks = [
            Chunk(
                text=r["text"][: config.max_chunk_chars],
                source=r["source"][: config.max_source_chars],
                score=r.get("score"),
            )
            for r in results
        ]
        return QueryResponse(chunks=chunks)

    # Upload size ceiling — protects disk + memory when the dashboard
    # (or any UI) uploads files. Generous for docs, tight enough that a
    # runaway client can't fill the temp partition. Overridable via env
    # so power users with large corpora can raise it deliberately.
    _MAX_INGEST_BYTES = int(
        os.environ.get("LENS_MAX_INGEST_BYTES", str(256 * 1024 * 1024))
    )

    @app.post("/ingest")
    async def ingest_endpoint(
        request: Request,
        files: list[UploadFile] = File(...),
        authorization: Optional[str] = Header(default=None),
    ):
        """Ingest uploaded files into the active library.

        Accepts `multipart/form-data` with one or more `files` parts.
        Each uploaded file is written to a short-lived temp directory,
        run through the same pipeline as `lens ingest <path>`, then
        the temp dir is discarded.

        Response shape depends on `Accept`:
          - Default (JSON): single-shot summary dict (backward compatible)
          - `application/x-ndjson`: line-delimited JSON progress stream,
            one event per line. Terminates with a summary event
            {"event": "result", ...}. Lets UIs draw a live progress bar
            over a long-running ingest instead of staring at a spinner.

        Directory layout is flat — the uploaded basename becomes the
        source field on chunks, matching how `lens ingest <file>` names
        them.
        """
        require_auth(authorization)
        if not files:
            raise HTTPException(400, "No files uploaded")

        accept = (request.headers.get("accept") or "").lower()
        want_stream = "application/x-ndjson" in accept

        # Create tempdir WITHOUT a `with` block — streaming needs the
        # directory to outlive the handler return. Cleanup is explicit,
        # either at end-of-handler (single-shot path) or in the generator's
        # finally (streaming path). The older `with tempfile.TemporaryDirectory`
        # pattern deleted the dir before the response generator ran, which
        # either crashed ingest (FileNotFoundError) or forced us to
        # pre-buffer every event — defeating streaming.
        tmpdir = tempfile.mkdtemp(prefix="lens-ingest-")
        tmp_path = Path(tmpdir)

        def _cleanup_tmpdir() -> None:
            import shutil as _shutil

            _shutil.rmtree(tmpdir, ignore_errors=True)

        try:
            total_bytes = 0
            written_any = False
            for upload in files:
                # Strip any directory components — treat the client-supplied
                # name as untrusted. `os.path.basename` on "..\\foo.pdf"
                # does the right thing across platforms.
                raw_name = upload.filename or ""
                name = os.path.basename(raw_name.replace("\\", "/"))
                if not name or name in (".", ".."):
                    continue
                dest = tmp_path / name
                with dest.open("wb") as out:
                    while True:
                        chunk = await upload.read(64 * 1024)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > _MAX_INGEST_BYTES:
                            raise HTTPException(
                                413,
                                f"Upload exceeds {_MAX_INGEST_BYTES} bytes",
                            )
                        out.write(chunk)
                written_any = True

            if not written_any:
                raise HTTPException(400, "No valid files in upload")
        except HTTPException:
            _cleanup_tmpdir()
            raise
        except Exception:
            _cleanup_tmpdir()
            raise

        # Import lazily — ingest pulls in heavy deps (embedder, optional
        # PDF/DOCX parsers) that we don't want to pay for on server
        # import if ingest is never called.
        from .ingest import ingest_path

        # Reuse the server's live store + embedder + backend so we don't
        # race the server's QdrantBackend for the local Qdrant file lock.
        store_obj = active_store()
        embedder_obj = get_embedder()
        backend_obj = _get_backend()

        if want_stream:
            import asyncio as _asyncio

            queue: _asyncio.Queue = _asyncio.Queue()
            _SENTINEL = object()
            loop = _asyncio.get_running_loop()

            def _sync_on_event(evt: dict) -> None:
                # Called from the worker thread. Hand the event to the
                # asyncio loop's queue without blocking it.
                loop.call_soon_threadsafe(queue.put_nowait, evt)

            async def _run_ingest() -> None:
                try:
                    result = await _asyncio.to_thread(
                        ingest_path,
                        config,
                        tmp_path,
                        False,  # emit_progress (stdout) — use callback
                        store_obj,
                        embedder_obj,
                        backend_obj,
                        _sync_on_event,
                    )
                    queue.put_nowait({"event": "result", **result})
                except FileNotFoundError as e:
                    queue.put_nowait({"event": "error", "message": str(e)})
                except Exception:
                    log.exception("Streaming ingest failed")
                    queue.put_nowait(
                        {"event": "error", "message": "ingest failed — see server logs"}
                    )
                finally:
                    queue.put_nowait(_SENTINEL)

            async def _ndjson_gen():
                task = _asyncio.create_task(_run_ingest())
                try:
                    while True:
                        item = await queue.get()
                        if item is _SENTINEL:
                            break
                        yield (json.dumps(item) + "\n").encode()
                finally:
                    # If the client disconnected early, cancel + wait.
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (_asyncio.CancelledError, Exception):
                            pass
                    _cleanup_tmpdir()

            return StreamingResponse(
                _ndjson_gen(), media_type="application/x-ndjson"
            )

        # Default: single-shot JSON (backward compatible).
        try:
            result = ingest_path(
                config,
                tmp_path,
                store=store_obj,
                embedder=embedder_obj,
                backend=backend_obj,
            )
        except FileNotFoundError as e:
            raise HTTPException(400, str(e))
        except Exception:
            log.exception("Ingest failed")
            raise HTTPException(500, "Ingest failed — see server logs")
        finally:
            _cleanup_tmpdir()

        return result

    # ── Library management ─────────────────────────────────────────────

    @app.get("/libraries")
    async def libraries_list(authorization: Optional[str] = Header(default=None)):
        require_auth(authorization)
        registry.ensure_default()
        return registry.list()

    @app.post("/libraries")
    async def libraries_create(
        req: LibraryCreateRequest,
        authorization: Optional[str] = Header(default=None),
    ):
        require_auth(authorization)
        try:
            lib = registry.create(req.name)
        except ValueError as e:
            # Registry raises ValueError for duplicate names — surface
            # as 409 Conflict so callers can distinguish "already exists"
            # from validation errors.
            raise HTTPException(409, str(e))
        return {"library": lib, "state": registry.list()}

    @app.post("/libraries/{library_id}/activate")
    async def libraries_activate(
        library_id: str,
        authorization: Optional[str] = Header(default=None),
    ):
        require_auth(authorization)
        try:
            registry.activate(library_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return registry.list()

    @app.patch("/libraries/{library_id}")
    async def libraries_rename(
        library_id: str,
        req: LibraryRenameRequest,
        authorization: Optional[str] = Header(default=None),
    ):
        require_auth(authorization)
        try:
            lib = registry.rename(library_id, req.name)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"library": lib, "state": registry.list()}

    @app.delete("/libraries/{library_id}")
    async def libraries_delete(
        library_id: str,
        authorization: Optional[str] = Header(default=None),
    ):
        require_auth(authorization)
        # Drop the collection + the registry entry. If this was the last
        # library, ensure_default() on next access will spawn a fresh
        # "Default" so the user isn't stranded without one.
        collection = registry.collection_for(library_id)
        store = Store(config, collection=collection, backend=_get_backend())
        try:
            store.drop()
        except Exception as e:  # noqa: BLE001
            log.warning("Dropping collection during delete_library failed: %s", e)
        try:
            registry.delete(library_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return registry.list()

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    return app


def run_server(config: LensConfig) -> None:
    """Blocking entry point — start the uvicorn server with the given config."""
    import uvicorn

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=False,
    )
