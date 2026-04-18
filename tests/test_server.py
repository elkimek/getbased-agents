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
