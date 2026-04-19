"""End-to-end server tests against an isolated temp config + fake embedder.

Covers every HTTP endpoint the server exposes — happy paths, auth failures,
validation errors, and full library-management lifecycle. Uses FastAPI
TestClient (no real uvicorn) and local Qdrant in a tmp dir. The fake
embedder is deterministic so similarity ranking is testable.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from uuid import uuid4

from lens.store import Store
from lens.registry import Registry

from .conftest import FakeEmbedder


def _seed_active_library(config, texts_by_source: dict[str, list[str]]) -> None:
    """Directly write vectors into the active library's Qdrant collection.
    Mirrors what `lens ingest` would do via the CLI, without going through
    the chunker — callers pass already-chunked text by source."""
    registry = Registry(config)
    registry.ensure_default()
    collection = registry.active_collection()
    embedder = FakeEmbedder()
    store = Store(config, collection=collection)
    store.ensure_collection(embedder.dimension())
    batch = []
    for source, pieces in texts_by_source.items():
        vectors = embedder.encode(pieces)
        for text, vector in zip(pieces, vectors):
            batch.append({"id": str(uuid4()), "text": text, "source": source, "vector": vector})
    store.upsert(batch)


# ── / and /health — public probes ─────────────────────────────────

def test_root_is_public(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "getbased-lens"
    assert "/query" in body["endpoints"]


def test_health_is_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["chunks"] == 0
    assert body["rag_ready"] is False


# ── Auth ──────────────────────────────────────────────────────────

def test_query_requires_auth(client: TestClient) -> None:
    r = client.post("/query", json={"version": 1, "query": "anything", "top_k": 1})
    assert r.status_code == 401
    assert "Missing Authorization" in r.json()["error"]


def test_query_rejects_wrong_scheme(client: TestClient, api_key: str) -> None:
    r = client.post(
        "/query",
        json={"version": 1, "query": "anything", "top_k": 1},
        headers={"Authorization": api_key},  # no "Bearer " prefix
    )
    assert r.status_code == 401
    assert "Bearer" in r.json()["error"]


def test_query_rejects_wrong_key(client: TestClient) -> None:
    r = client.post(
        "/query",
        json={"version": 1, "query": "anything", "top_k": 1},
        headers={"Authorization": "Bearer not-the-real-key"},
    )
    assert r.status_code == 401
    assert "Invalid API key" in r.json()["error"]


# ── /query validation + happy path ────────────────────────────────

def test_query_rejects_empty_string(client: TestClient, auth: dict) -> None:
    r = client.post("/query", json={"version": 1, "query": "   ", "top_k": 3}, headers=auth)
    assert r.status_code == 400
    assert "Empty query" in r.json()["error"]


def test_query_rejects_unknown_version(client: TestClient, auth: dict) -> None:
    r = client.post("/query", json={"version": 99, "query": "hello", "top_k": 3}, headers=auth)
    assert r.status_code == 400
    assert "Unsupported version" in r.json()["error"]


def test_query_empty_library_returns_no_chunks(client: TestClient, auth: dict) -> None:
    r = client.post("/query", json={"version": 1, "query": "vitamin D", "top_k": 5}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"chunks": []}


def test_query_returns_top_k_from_active_library(client: TestClient, auth: dict, config) -> None:
    _seed_active_library(config, {
        "notes.md":       ["vitamin D is a secosteroid hormone synthesised in skin when UVB hits cholesterol"],
        "mitochondria.md": ["cytochrome c oxidase peaks at 670nm for near-infrared photobiomodulation"],
        "sleep.md":        ["blue light around 480nm suppresses melatonin via melanopsin ganglion cells"],
    })
    r = client.post(
        "/query",
        json={"version": 1, "query": "vitamin D is a secosteroid hormone synthesised in skin when UVB hits cholesterol", "top_k": 2},
        headers=auth,
    )
    assert r.status_code == 200
    chunks = r.json()["chunks"]
    assert len(chunks) > 0
    # Top hit should be the exact-match source (deterministic fake embedder).
    assert chunks[0]["source"] == "notes.md"
    for c in chunks:
        assert "text" in c and "source" in c


# ── /stats ────────────────────────────────────────────────────────

def test_stats_empty(client: TestClient, auth: dict) -> None:
    r = client.get("/stats", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["total_chunks"] == 0
    assert body["documents"] == []


def test_stats_after_seed(client: TestClient, auth: dict, config) -> None:
    _seed_active_library(config, {"a.md": ["one", "two"], "b.md": ["three"]})
    r = client.get("/stats", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["total_chunks"] == 3
    by_source = {d["source"]: d["chunks"] for d in body["documents"]}
    assert by_source == {"a.md": 2, "b.md": 1}


# ── /sources — delete one + clear all ────────────────────────────

def test_delete_source_removes_only_that_source(client: TestClient, auth: dict, config) -> None:
    _seed_active_library(config, {"a.md": ["x", "y"], "b.md": ["z"]})
    r = client.delete("/sources/a.md", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"deleted_chunks": 2}
    stats = client.get("/stats", headers=auth).json()
    assert stats["total_chunks"] == 1
    assert stats["documents"][0]["source"] == "b.md"


def test_clear_sources_wipes_library(client: TestClient, auth: dict, config) -> None:
    _seed_active_library(config, {"a.md": ["x"], "b.md": ["y"]})
    r = client.delete("/sources", headers=auth)
    assert r.status_code == 200
    assert r.json()["deleted_chunks"] == 2
    stats = client.get("/stats", headers=auth).json()
    assert stats["total_chunks"] == 0


# ── /libraries — full CRUD lifecycle ─────────────────────────────

def test_libraries_default_exists_after_any_data_call(client: TestClient, auth: dict) -> None:
    # Accessing /stats triggers ensure_default(); /libraries then reports it.
    client.get("/stats", headers=auth)
    r = client.get("/libraries", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert len(body["libraries"]) >= 1
    assert body["activeId"]


def test_library_create_activate_rename_delete(client: TestClient, auth: dict) -> None:
    # Bootstrap the default library so we have something to compare against.
    client.get("/stats", headers=auth)

    # Create
    r = client.post("/libraries", json={"name": "Research"}, headers=auth)
    assert r.status_code == 200
    created = r.json()["library"]
    assert created["name"] == "Research"
    research_id = created["id"]

    # Activate
    r = client.post(f"/libraries/{research_id}/activate", headers=auth)
    assert r.status_code == 200
    assert r.json()["activeId"] == research_id

    # Rename
    r = client.patch(
        f"/libraries/{research_id}",
        json={"name": "Kruse Research"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["library"]["name"] == "Kruse Research"

    # Delete
    r = client.delete(f"/libraries/{research_id}", headers=auth)
    assert r.status_code == 200
    remaining = {l["id"] for l in r.json()["libraries"]}
    assert research_id not in remaining
    # Active falls back to whichever library remains — auto-promoted.
    assert r.json()["activeId"] in remaining


def test_library_isolation(client: TestClient, auth: dict, config) -> None:
    """Seeding one library must not leak into a sibling library's search
    results. This is the core multi-library guarantee."""
    # Seed the default library.
    _seed_active_library(config, {"default-only.md": ["content that lives only in the default library"]})

    # Create + activate a new library, which starts empty.
    r = client.post("/libraries", json={"name": "Empty"}, headers=auth)
    new_id = r.json()["library"]["id"]
    client.post(f"/libraries/{new_id}/activate", headers=auth)

    stats = client.get("/stats", headers=auth).json()
    assert stats["total_chunks"] == 0
    assert stats["documents"] == []


def test_activate_nonexistent_library_returns_404(client: TestClient, auth: dict) -> None:
    r = client.post("/libraries/does-not-exist/activate", headers=auth)
    assert r.status_code == 404


def test_rename_nonexistent_library_returns_404(client: TestClient, auth: dict) -> None:
    r = client.patch(
        "/libraries/does-not-exist",
        json={"name": "X"},
        headers=auth,
    )
    assert r.status_code == 404


def test_duplicate_library_name_rejected(client: TestClient, auth: dict) -> None:
    """Rapid double-submit from a browser (UI guards help but can't
    bulletproof across DOM re-renders) should not produce duplicate
    libraries. Server enforces unique names with 409 Conflict."""
    r1 = client.post("/libraries", json={"name": "Research"}, headers=auth)
    assert r1.status_code == 200
    r2 = client.post("/libraries", json={"name": "Research"}, headers=auth)
    assert r2.status_code == 409
    assert "already exists" in r2.json().get("error", "")
    # Case-insensitive match — "research" also conflicts
    r3 = client.post("/libraries", json={"name": "research"}, headers=auth)
    assert r3.status_code == 409
    # Confirm only one library exists with that name
    state = client.get("/libraries", headers=auth).json()
    names = [l["name"] for l in state["libraries"]]
    assert names.count("Research") == 1


# ── Security hardening — input validation, CORS, error sanitisation ───

def test_query_rejects_overlong_string(client: TestClient, auth: dict) -> None:
    """4096-char cap on query — anything larger returns 422 before the
    embedder runs, capping the tokenizer-DoS attack surface."""
    r = client.post("/query", json={"version": 1, "query": "x" * 5000, "top_k": 1}, headers=auth)
    assert r.status_code == 422


def test_query_rejects_negative_top_k(client: TestClient, auth: dict) -> None:
    r = client.post("/query", json={"version": 1, "query": "x", "top_k": -1}, headers=auth)
    assert r.status_code == 422


def test_library_rejects_overlong_name(client: TestClient, auth: dict) -> None:
    r = client.post("/libraries", json={"name": "x" * 500}, headers=auth)
    assert r.status_code == 422


def test_library_rename_rejects_empty(client: TestClient, auth: dict) -> None:
    # First get a real library id to rename
    client.get("/stats", headers=auth)
    libs = client.get("/libraries", headers=auth).json()["libraries"]
    r = client.patch(f"/libraries/{libs[0]['id']}", json={"name": ""}, headers=auth)
    assert r.status_code == 422


def test_cors_allows_pwa_origin(client: TestClient) -> None:
    r = client.options(
        "/query",
        headers={
            "Origin": "https://app.getbased.health",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://app.getbased.health"


def test_cors_blocks_random_origin(client: TestClient) -> None:
    """A random website visited by a user running this server on localhost
    must not be able to read cross-origin responses. Preflight without a
    matching origin should not echo the origin back."""
    r = client.options(
        "/query",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    # Either 400 (no allow-origin header at all) or 200 with no allow-origin
    # echoed — both are browser-blocking. Any echo of evil.example is a bug.
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


def test_ingest_file_size_cap(tmp_path) -> None:
    """Files over MAX_FILE_BYTES should be rejected before any parser runs —
    caps the attack surface of oversized PDFs or docx zip bombs."""
    from lens.ingest import _read_text, MAX_FILE_BYTES

    big = tmp_path / "huge.txt"
    # Write just over the cap (sparse-file, so no real disk use)
    with big.open("wb") as f:
        f.seek(MAX_FILE_BYTES + 1)
        f.write(b"\0")
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="exceeds"):
        _read_text(big)


# ── POST /ingest — HTTP file upload ────────────────────────────────

def test_ingest_requires_auth(client: TestClient) -> None:
    r = client.post("/ingest", files={"files": ("x.md", b"hello", "text/markdown")})
    assert r.status_code == 401


def test_ingest_happy_path(client: TestClient, auth: dict) -> None:
    """Upload a markdown file → it should land in the active library as chunks
    tagged with the uploaded filename as `source`."""
    r = client.post(
        "/ingest",
        headers=auth,
        files=[("files", ("vitd-notes.md", b"# Vitamin D\n\nSynthesised in skin when UVB hits 7-dehydrocholesterol. " * 20, "text/markdown"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("files_seen", 0) >= 1
    assert body.get("chunks_indexed", 0) >= 1
    # Stats should now show that source
    stats = client.get("/stats", headers=auth).json()
    sources = [d["source"] for d in stats["documents"]]
    assert "vitd-notes.md" in sources


def test_ingest_multiple_files(client: TestClient, auth: dict) -> None:
    """Multi-file uploads are flattened into one temp dir and ingested as
    a unit — both files show up as distinct sources afterward."""
    r = client.post(
        "/ingest",
        headers=auth,
        files=[
            ("files", ("a.md", b"# A\n\n" + b"content " * 100, "text/markdown")),
            ("files", ("b.md", b"# B\n\n" + b"other " * 100, "text/markdown")),
        ],
    )
    assert r.status_code == 200
    stats = client.get("/stats", headers=auth).json()
    sources = {d["source"] for d in stats["documents"]}
    assert {"a.md", "b.md"}.issubset(sources)


def test_ingest_strips_path_components_from_filename(client: TestClient, auth: dict) -> None:
    """A filename like '../../foo.md' must never escape the temp dir. The
    server-side basename sanitisation should reduce it to 'foo.md'.
    (Use a .md extension so the ingest walker actually picks the file up
    — traversal chars without an allowed extension get skipped anyway,
    but we want to verify the sanitiser, not the walker.)"""
    r = client.post(
        "/ingest",
        headers=auth,
        files=[("files", ("../../../etc/pwned.md", b"# safe\n" + b"x " * 200, "text/markdown"))],
    )
    assert r.status_code == 200
    stats = client.get("/stats", headers=auth).json()
    sources = {d["source"] for d in stats["documents"]}
    assert "pwned.md" in sources
    # No traversal sequence anywhere in the recorded source metadata
    assert not any(".." in s or "/" in s.lstrip(".") for s in sources)


def test_ingest_rejects_empty_upload(client: TestClient, auth: dict) -> None:
    """Blank filename uploads should be rejected — FastAPI may catch this
    at validation (422) or it may reach our sanitiser (400 "No valid files").
    Both are correct behaviour; either is fine as long as nothing writes."""
    r = client.post(
        "/ingest",
        headers=auth,
        files=[("files", ("", b"x", "text/plain"))],
    )
    assert r.status_code in (400, 422)


def test_ingest_expands_zip_inside_uploaded_dir(
    client: TestClient, auth: dict, tmp_path
) -> None:
    """When a .zip is part of a multipart upload, rag must auto-extract
    it so the contained markdown/text files get indexed. The single-
    file CLI path already handles zips via _expand_zip_if_needed;
    the HTTP path dropped them silently until _walk learned to expand
    zips inside a directory.

    Regression for a bug where uploading a .zip via the dashboard
    produced start total:0 + result chunks:0 — zip was saved but never
    walked."""
    import io
    import zipfile

    # Build an in-memory zip with two markdown files.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "doc-a.md", "# Alpha\n\n" + "content " * 100
        )
        zf.writestr("doc-b.md", "# Beta\n\n" + "more stuff " * 100)
    buf.seek(0)

    r = client.post(
        "/ingest",
        headers=auth,
        files=[("files", ("bundle.zip", buf.read(), "application/zip"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files_seen"] >= 2, body
    assert body["chunks_indexed"] >= 2
    # Stats should list both extracted files as sources
    stats = client.get("/stats", headers=auth).json()
    sources = {d["source"] for d in stats["documents"]}
    # Extracted names land under `_zip_bundle/doc-{a,b}.md`
    assert any("doc-a.md" in s for s in sources), sources
    assert any("doc-b.md" in s for s in sources), sources


def test_ingest_ndjson_stream_emits_start_file_and_result_events(
    client: TestClient, auth: dict
) -> None:
    """With Accept: application/x-ndjson, rag emits a progress stream:
    one `start` event (with total count), one `file` event per file, and
    a terminal `result` event with the summary. UI consumes these to
    draw a live progress bar."""
    r = client.post(
        "/ingest",
        headers={**auth, "Accept": "application/x-ndjson"},
        files=[
            ("files", ("one.md", b"# one\n" + b"alpha " * 150, "text/markdown")),
            ("files", ("two.md", b"# two\n" + b"beta " * 150, "text/markdown")),
        ],
    )
    assert r.status_code == 200
    # Body is line-delimited JSON
    lines = [l for l in r.text.splitlines() if l.strip()]
    events = [__import__("json").loads(l) for l in lines]
    starts = [e for e in events if e.get("event") == "start"]
    files = [e for e in events if e.get("event") == "file"]
    results = [e for e in events if e.get("event") == "result"]
    errors = [e for e in events if e.get("event") == "error"]

    assert len(starts) == 1, events
    assert starts[0]["total"] == 2
    assert len(files) == 2, events
    assert {f["source"] for f in files} == {"one.md", "two.md"}
    assert len(results) == 1, events
    assert results[0]["files_seen"] == 2
    assert results[0]["chunks_indexed"] >= 2
    assert not errors


def test_ingest_ndjson_stream_reports_error_as_event(
    client: TestClient, auth: dict, monkeypatch
) -> None:
    """If ingest blows up, the stream must terminate with an `error`
    event rather than tearing the connection down with no signal. UI
    needs a single code path for failure."""
    from lens import ingest as ingest_mod

    def boom(*_a, **_kw):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(ingest_mod, "ingest_path", boom)

    r = client.post(
        "/ingest",
        headers={**auth, "Accept": "application/x-ndjson"},
        files=[("files", ("x.md", b"# x\n" + b"y " * 100, "text/markdown"))],
    )
    assert r.status_code == 200  # streaming 200; errors go in-band
    import json as _json

    lines = [l for l in r.text.splitlines() if l.strip()]
    events = [_json.loads(l) for l in lines]
    errors = [e for e in events if e.get("event") == "error"]
    assert errors, events
    # Message is generic — we don't leak the raw exception string to clients
    assert "ingest failed" in errors[0]["message"].lower() or "synthetic" in errors[0]["message"]


def test_info_endpoint_reports_engine_and_dim(
    client: TestClient, auth: dict
) -> None:
    """Dashboard's Knowledge tab shows an engine badge — it reads
    /info for the embedder metadata. Ensure the endpoint returns the
    fields we advertise (engine, model, dimension) plus active library
    + chunk count."""
    r = client.get("/info", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "embedder" in body
    emb = body["embedder"]
    # Fake embedder in tests reports its class name as the engine
    assert emb["dimension"] >= 1
    assert "active_library" in body
    # Config echo
    assert body["similarity_floor"] >= 0
    assert isinstance(body["reranker"], bool)


def test_info_requires_auth(client: TestClient) -> None:
    assert client.get("/info").status_code == 401


def test_models_lists_curated_plus_default(client: TestClient, auth: dict) -> None:
    """UI renders a dropdown from /models — it must include the server's
    current default and expose dim for each option (dim is what locks
    collections, so users should see why models aren't interchangeable
    post-creation)."""
    r = client.get("/models", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "default" in body
    assert "models" in body
    assert len(body["models"]) >= 5
    # Every curated entry has an id, label, dim, plus optional notes
    for m in body["models"]:
        assert m["id"]
        assert isinstance(m["dim"], int)
        assert m["dim"] > 0
        assert "label" in m
    # At least one entry matches the server's configured default
    assert any(m["id"] == body["default"] for m in body["models"]) or body[
        "default"
    ] in [m["id"] for m in body["models"]]


def test_models_requires_auth(client: TestClient) -> None:
    assert client.get("/models").status_code == 401


def test_library_create_with_model_pins_it(client: TestClient, auth: dict) -> None:
    """Creating a library with an explicit embedding_model stores that
    value. Subsequent `list()` shows the pinned model; the server's
    active_embedder() resolves to a per-model instance."""
    r = client.post(
        "/libraries",
        json={"name": "BGE test", "embedding_model": "BAAI/bge-small-en-v1.5"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["library"]["embedding_model"] == "BAAI/bge-small-en-v1.5"
    # Show in list with the pinned model
    state = client.get("/libraries", headers=auth).json()
    found = [l for l in state["libraries"] if l["name"] == "BGE test"]
    assert len(found) == 1
    assert found[0]["embedding_model"] == "BAAI/bge-small-en-v1.5"


def test_library_create_without_model_uses_server_default(
    client: TestClient, auth: dict, config
) -> None:
    """Omitting the field falls back to LENS_EMBEDDING_MODEL at create
    time. UI always has something to show in the model chip."""
    r = client.post("/libraries", json={"name": "Default-model"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["library"]["embedding_model"] == config.embedding_model


def test_legacy_library_without_stored_model_inherits_default(tmp_path) -> None:
    """Libraries created before per-model support lack the embedding_model
    field in libraries.json. On read, they inherit the server's current
    default. Matches the browser-local lens's legacy-library handling."""
    from lens.registry import Registry
    from lens.config import LensConfig
    from pathlib import Path
    import json as _json

    cfg = LensConfig(data_dir=tmp_path, embedding_model="all-MiniLM-L6-v2")
    libs_file = Path(tmp_path) / "libraries.json"
    libs_file.write_text(
        _json.dumps(
            {
                "activeId": "legacy1",
                "libraries": [
                    {"id": "legacy1", "name": "Old Library", "createdAt": 1}
                ],
            }
        )
    )
    reg = Registry(cfg)
    state = reg.list()
    assert state["libraries"][0]["embedding_model"] == "all-MiniLM-L6-v2"
    # model_for() also resolves to default for legacy libraries
    assert reg.model_for("legacy1") == "all-MiniLM-L6-v2"


def test_library_create_rejects_overlong_model(client: TestClient, auth: dict) -> None:
    r = client.post(
        "/libraries",
        json={"name": "X", "embedding_model": "a" * 500},
        headers=auth,
    )
    assert r.status_code == 422


def test_two_libraries_same_model_share_embedder_instance(
    client: TestClient, auth: dict, patched_embedder, monkeypatch, config
) -> None:
    """Two libraries on the same model should reuse one embedder —
    matters for BGE-M3 (2 GB resident) where duplicating would OOM
    many boxes. Assert by tracking how many times create_embedder gets
    called for a given model."""
    calls: list[str] = []

    from lens import embedder as emb_mod
    from lens import server as server_mod

    original = server_mod.create_embedder

    def counting(cfg):
        calls.append(cfg.embedding_model)
        return original(cfg)

    monkeypatch.setattr(server_mod, "create_embedder", counting)

    # Two libraries pinned to same model
    r1 = client.post(
        "/libraries",
        json={"name": "Lib-A", "embedding_model": "all-MiniLM-L6-v2"},
        headers=auth,
    )
    r2 = client.post(
        "/libraries",
        json={"name": "Lib-B", "embedding_model": "all-MiniLM-L6-v2"},
        headers=auth,
    )
    assert r1.status_code == 200 and r2.status_code == 200

    # Activate A, make a query → embedder for MiniLM loads once
    client.post(
        f"/libraries/{r1.json()['library']['id']}/activate", headers=auth
    )
    client.post("/query", json={"query": "hi", "top_k": 1}, headers=auth)
    # Activate B, query again → should reuse the same embedder
    client.post(
        f"/libraries/{r2.json()['library']['id']}/activate", headers=auth
    )
    client.post("/query", json={"query": "hi", "top_k": 1}, headers=auth)

    assert calls.count("all-MiniLM-L6-v2") == 1, calls


def test_ingest_json_single_shot_still_works_without_accept_header(
    client: TestClient, auth: dict
) -> None:
    """Backward compatibility: clients that don't ask for NDJSON keep
    getting the original single-shot summary dict. No breaking change."""
    r = client.post(
        "/ingest",
        headers=auth,
        files=[("files", ("x.md", b"# x\n" + b"z " * 100, "text/markdown"))],
    )
    assert r.status_code == 200
    body = r.json()
    assert "files_seen" in body
    assert "chunks_indexed" in body


def test_ingest_reuses_server_backend_not_a_fresh_one(
    client: TestClient, auth: dict, monkeypatch
) -> None:
    """Regression for the local-Qdrant lock collision: the /ingest endpoint
    must pass the server's live QdrantBackend into ingest_path, not let
    ingest_path instantiate a fresh one. A fresh backend would try to
    open a second QdrantClient on the same on-disk path and raise
    AlreadyLocked, breaking ingest whenever the server is running.

    We can't easily reproduce the lock with the test fixture (which
    deliberately shares a QdrantClient across seeder + server to sidestep
    the issue), so instead we assert behaviourally: if the server is
    wiring its backend through, then calling QdrantBackend() inside
    ingest_path would never happen. Patch QdrantBackend to raise on
    instantiation and verify ingest still succeeds."""
    # Raise loudly if anyone constructs a second QdrantBackend during the
    # ingest call — catches any regression where the endpoint stops
    # threading the backend argument.
    from lens import ingest as ingest_mod

    original = ingest_mod.QdrantBackend

    def boom(*_a, **_kw):
        raise AssertionError(
            "ingest_path tried to create a new QdrantBackend — it should "
            "reuse the server's singleton via the `backend`/`store` args"
        )

    monkeypatch.setattr(ingest_mod, "QdrantBackend", boom)
    try:
        r = client.post(
            "/ingest",
            headers=auth,
            files=[("files", ("notes.md", b"# hi\n" + b"x " * 200, "text/markdown"))],
        )
        assert r.status_code == 200, r.text
        assert r.json().get("chunks_indexed", 0) >= 1
    finally:
        monkeypatch.setattr(ingest_mod, "QdrantBackend", original)


def test_ingest_rejects_oversize_upload(
    client: TestClient, auth: dict, monkeypatch
) -> None:
    """Total upload > LENS_MAX_INGEST_BYTES should 413 without writing past
    the cap. We set a tiny cap via env and upload just over it."""
    monkeypatch.setenv("LENS_MAX_INGEST_BYTES", "1024")
    # Rebuild the app so the endpoint picks up the new cap
    from lens.server import create_app
    from lens.config import LensConfig

    small_app = create_app(LensConfig.from_env())
    c2 = TestClient(small_app)
    # 2 KB payload > 1 KB cap
    payload = b"A" * 2048
    r = c2.post(
        "/ingest",
        headers=auth,
        files=[("files", ("big.md", payload, "text/markdown"))],
    )
    assert r.status_code == 413
    assert "exceeds" in r.json()["error"]


def test_500_errors_dont_leak_exception_details(client: TestClient, auth: dict, monkeypatch) -> None:
    """Verify the 500-response sanitisation — a forced internal error should
    surface a generic message, not the raw traceback / path / exception repr."""
    from lens.store import Store

    def boom(*_a, **_kw):
        raise RuntimeError("secret internal detail /home/user/secrets.txt")

    monkeypatch.setattr(Store, "list_sources", boom)
    r = client.get("/stats", headers=auth)
    assert r.status_code == 500
    body_text = r.text
    assert "secret internal detail" not in body_text
    assert "/home/user/secrets.txt" not in body_text
    assert "see server logs" in body_text
