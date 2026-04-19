"""Tests for the knowledge-proxy API. Each test mocks the upstream rag
server via respx and asserts the dashboard forwards the request with the
right method, path, bearer, and surfaces rag's response or error back to
the browser unchanged (modulo detail normalisation)."""

from __future__ import annotations

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import ConnectError, Response

RAG_BASE = "http://lens.test:8322"
AUTH = {"Authorization": "Bearer test-dashboard-key"}


# ─── Auth propagation — every knowledge endpoint requires bearer ─────

def test_libraries_list_requires_auth(client: TestClient) -> None:
    r = client.get("/api/knowledge/libraries")
    assert r.status_code == 401


def test_search_requires_auth(client: TestClient) -> None:
    r = client.post("/api/knowledge/search", json={"query": "x"})
    assert r.status_code == 401


# ─── Library CRUD proxy ──────────────────────────────────────────────

@respx.mock
def test_libraries_list_forwards(client: TestClient) -> None:
    route = respx.get(f"{RAG_BASE}/libraries").mock(
        return_value=Response(200, json={
            "activeId": "a", "libraries": [{"id": "a", "name": "Main"}],
        })
    )
    r = client.get("/api/knowledge/libraries", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["activeId"] == "a"
    # Outbound carried dashboard's bearer
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-dashboard-key"


@respx.mock
def test_libraries_create_forwards_body(client: TestClient) -> None:
    route = respx.post(f"{RAG_BASE}/libraries").mock(
        return_value=Response(200, json={
            "library": {"id": "new", "name": "Research"},
            "state": {"activeId": "a", "libraries": []},
        })
    )
    r = client.post("/api/knowledge/libraries", headers=AUTH, json={"name": "Research"})
    assert r.status_code == 200
    # Body verbatim
    import json as _json
    body = _json.loads(route.calls[0].request.content)
    assert body == {"name": "Research"}


@respx.mock
def test_activate_library_forwards_id(client: TestClient) -> None:
    respx.post(f"{RAG_BASE}/libraries/xyz/activate").mock(
        return_value=Response(200, json={"activeId": "xyz", "libraries": []})
    )
    r = client.post("/api/knowledge/libraries/xyz/activate", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["activeId"] == "xyz"


@respx.mock
def test_rename_library_forwards_patch(client: TestClient) -> None:
    route = respx.patch(f"{RAG_BASE}/libraries/abc").mock(
        return_value=Response(200, json={
            "library": {"id": "abc", "name": "Renamed"},
            "state": {"activeId": "abc", "libraries": []},
        })
    )
    r = client.patch(
        "/api/knowledge/libraries/abc", headers=AUTH, json={"name": "Renamed"}
    )
    assert r.status_code == 200
    assert route.called


@respx.mock
def test_delete_library_forwards(client: TestClient) -> None:
    respx.delete(f"{RAG_BASE}/libraries/gone").mock(
        return_value=Response(200, json={"activeId": "", "libraries": []})
    )
    r = client.delete("/api/knowledge/libraries/gone", headers=AUTH)
    assert r.status_code == 200


# ─── Search proxy — stitches on protocol version ─────────────────────

@respx.mock
def test_search_adds_version_field(client: TestClient) -> None:
    """Frontend sends {query, top_k}; dashboard forwards {version: 1, ...}
    so the UI doesn't have to know about rag's protocol version."""
    route = respx.post(f"{RAG_BASE}/query").mock(
        return_value=Response(200, json={"chunks": [{"text": "hi", "source": "x"}]})
    )
    r = client.post(
        "/api/knowledge/search",
        headers=AUTH,
        json={"query": "omega-3", "top_k": 3},
    )
    assert r.status_code == 200
    import json as _json
    body = _json.loads(route.calls[0].request.content)
    assert body == {"version": 1, "query": "omega-3", "top_k": 3}


@respx.mock
def test_search_defaults_top_k(client: TestClient) -> None:
    route = respx.post(f"{RAG_BASE}/query").mock(
        return_value=Response(200, json={"chunks": []})
    )
    r = client.post("/api/knowledge/search", headers=AUTH, json={"query": "x"})
    assert r.status_code == 200
    import json as _json
    body = _json.loads(route.calls[0].request.content)
    assert body["top_k"] == 5


# ─── Stats + sources management ──────────────────────────────────────

@respx.mock
def test_stats_forwards(client: TestClient) -> None:
    respx.get(f"{RAG_BASE}/stats").mock(
        return_value=Response(200, json={"total_chunks": 10, "documents": []})
    )
    r = client.get("/api/knowledge/stats", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["total_chunks"] == 10


@respx.mock
def test_delete_source_preserves_path(client: TestClient) -> None:
    """Source names can contain slashes (nested paths). The `:path`
    converter means everything after /sources/ goes upstream verbatim."""
    route = respx.delete(f"{RAG_BASE}/sources/docs/paper.pdf").mock(
        return_value=Response(200, json={"deleted_chunks": 5})
    )
    r = client.delete("/api/knowledge/sources/docs/paper.pdf", headers=AUTH)
    assert r.status_code == 200
    assert route.called


# ─── Error propagation ───────────────────────────────────────────────

@respx.mock
def test_rag_unreachable_returns_502(client: TestClient) -> None:
    respx.get(f"{RAG_BASE}/libraries").mock(side_effect=ConnectError("refused"))
    r = client.get("/api/knowledge/libraries", headers=AUTH)
    assert r.status_code == 502
    assert "not reachable" in r.json()["detail"]


@respx.mock
def test_rag_error_body_is_surfaced(client: TestClient) -> None:
    """rag emits {"error": "..."} via its exception_handler. Dashboard
    extracts the string and forwards it as `detail` so FastAPI's default
    envelope stays consistent."""
    respx.post(f"{RAG_BASE}/libraries/bogus/activate").mock(
        return_value=Response(404, json={"error": "Library not found"})
    )
    r = client.post("/api/knowledge/libraries/bogus/activate", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["detail"] == "Library not found"


# ─── Ingest upload proxy ─────────────────────────────────────────────

@respx.mock
def test_ingest_forwards_multipart(client: TestClient) -> None:
    route = respx.post(f"{RAG_BASE}/ingest").mock(
        return_value=Response(200, json={
            "files_seen": 1, "chunks_indexed": 3, "skipped": [],
        })
    )
    r = client.post(
        "/api/knowledge/ingest",
        headers=AUTH,
        files=[("files", ("notes.md", b"# hello\nworld" * 100, "text/markdown"))],
    )
    assert r.status_code == 200, r.text
    assert r.json()["chunks_indexed"] == 3
    # Dashboard forwarded its bearer, not the browser's Authorization blob
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-dashboard-key"


@respx.mock
def test_ingest_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/api/knowledge/ingest",
        files=[("files", ("x.md", b"x", "text/markdown"))],
    )
    assert r.status_code == 401
