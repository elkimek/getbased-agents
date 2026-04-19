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
    assert "not reachable" in r.json()["error"]


@respx.mock
def test_rag_error_body_is_surfaced(client: TestClient) -> None:
    """rag emits {"error": "..."} via its exception_handler. Dashboard
    normalises to the same envelope so the browser only has one shape
    to parse."""
    respx.post(f"{RAG_BASE}/libraries/bogus/activate").mock(
        return_value=Response(404, json={"error": "Library not found"})
    )
    r = client.post("/api/knowledge/libraries/bogus/activate", headers=AUTH)
    assert r.status_code == 404
    assert r.json() == {"error": "Library not found"}


def test_validation_errors_come_back_as_flat_string(client: TestClient) -> None:
    """FastAPI default would return {"detail": [{"msg": "...", "loc": [...]}...]}.
    That serialises to "[object Object]" in the browser when we
    `new Error(err.detail)`. Dashboard's exception handler flattens
    validation details into a single string so the UI always gets a
    readable message."""
    # POST /api/knowledge/search with missing body → Pydantic rejects
    r = client.post(
        "/api/knowledge/search", headers=AUTH, content=""
    )
    assert r.status_code == 422
    body = r.json()
    assert "error" in body
    assert isinstance(body["error"], str)
    # The human-readable message should name what's wrong
    assert len(body["error"]) > 0


@respx.mock
def test_rag_422_validation_error_flattens_through_proxy(client: TestClient) -> None:
    """When rag returns a 422 with its default `{detail: [{...}]}` shape
    (rag's exception_handler only wraps HTTPException, not Pydantic
    RequestValidationError), the dashboard's own handler receives that
    dict as `exc.detail`. A naive flattener would str(dict) it — we need
    to recurse into the `detail` key and render the inner list as a
    readable line. Regression for the H3 partial that ran all the way
    through proxy + upstream validation."""
    # Simulate rag returning the default FastAPI validation envelope.
    respx.post(f"{RAG_BASE}/query").mock(
        return_value=Response(
            422,
            json={
                "detail": [
                    {
                        "type": "string_too_short",
                        "loc": ["body", "query"],
                        "msg": "String should have at least 1 character",
                    }
                ]
            },
        )
    )
    r = client.post(
        "/api/knowledge/search", headers=AUTH, json={"query": "", "top_k": 3}
    )
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["error"], str)
    # Should contain the human-readable field-level message, not a dict repr
    assert "body.query" in body["error"]
    assert "at least 1 character" in body["error"]
    # And should NOT contain the raw dict representation
    assert "'detail'" not in body["error"]
    assert "'type'" not in body["error"]


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


def test_ingest_rejects_oversize_before_reaching_rag(
    client: TestClient, monkeypatch
) -> None:
    """CRITICAL regression: dashboard must enforce the byte cap BEFORE
    buffering the full upload. Previously we read every byte into RAM
    then forwarded to rag — a rogue client could OOM the dashboard by
    uploading multi-GB files and rag's own cap never got a say. Now
    the streaming read checks total_bytes per chunk and raises 413
    before the disk write finishes."""
    # Set a tiny dashboard-side cap, independent of rag.
    from getbased_dashboard.api import knowledge as kn_api

    monkeypatch.setattr(kn_api, "_MAX_INGEST_BYTES", 1024)  # 1 KB
    # If the check runs before forwarding, rag is never called — so no
    # respx mock is needed. Confirm by asserting 413 directly.
    payload = b"A" * 4096  # 4x the cap
    r = client.post(
        "/api/knowledge/ingest",
        headers=AUTH,
        files=[("files", ("big.md", payload, "text/markdown"))],
    )
    assert r.status_code == 413
    assert "exceeds" in r.json()["error"]


@respx.mock
def test_ingest_streams_ndjson_events_when_accept_requested(
    client: TestClient,
) -> None:
    """Browser sends `Accept: application/x-ndjson`; dashboard forwards
    the Accept header upstream and pipes each line of rag's stream back
    to the client verbatim. UI's fetch+reader loop sees start/file/result
    events identical to what rag emitted."""
    ndjson_body = (
        '{"event":"start","total":2}\n'
        '{"event":"file","index":1,"total":2,"source":"a.md","chunks":3}\n'
        '{"event":"file","index":2,"total":2,"source":"b.md","chunks":5}\n'
        '{"event":"result","files_seen":2,"chunks_indexed":8,"skipped":[]}\n'
    )
    respx.post(f"{RAG_BASE}/ingest").mock(
        return_value=Response(
            200, text=ndjson_body, headers={"content-type": "application/x-ndjson"}
        )
    )
    r = client.post(
        "/api/knowledge/ingest",
        headers={**AUTH, "Accept": "application/x-ndjson"},
        files=[
            ("files", ("a.md", b"hi" * 200, "text/markdown")),
            ("files", ("b.md", b"yo" * 200, "text/markdown")),
        ],
    )
    assert r.status_code == 200
    # The dashboard should have passed each line through
    lines = [l for l in r.text.splitlines() if l.strip()]
    import json as _json

    events = [_json.loads(l) for l in lines]
    assert events[0]["event"] == "start"
    assert events[0]["total"] == 2
    assert events[-1]["event"] == "result"
    assert events[-1]["chunks_indexed"] == 8


@respx.mock
def test_ingest_stream_rewrites_upstream_error_as_event(
    client: TestClient,
) -> None:
    """If rag returns 4xx/5xx when the client is streaming, we still emit
    an `error` event in the NDJSON body so the UI's reader loop has one
    consistent code path."""
    respx.post(f"{RAG_BASE}/ingest").mock(
        return_value=Response(
            500, json={"error": "rag fell over"}, headers={"content-type": "application/json"}
        )
    )
    r = client.post(
        "/api/knowledge/ingest",
        headers={**AUTH, "Accept": "application/x-ndjson"},
        files=[("files", ("x.md", b"hi" * 200, "text/markdown"))],
    )
    # Dashboard still returns 200 so the browser gets the stream body;
    # error is conveyed as an NDJSON event.
    assert r.status_code == 200
    import json as _json

    lines = [l for l in r.text.splitlines() if l.strip()]
    events = [_json.loads(l) for l in lines]
    assert any(e.get("event") == "error" for e in events), events
    err = next(e for e in events if e.get("event") == "error")
    assert "rag fell over" in err["message"]


@respx.mock
def test_ingest_sanitises_filename_at_dashboard_layer(
    client: TestClient,
) -> None:
    """Defence-in-depth: dashboard strips path components from the
    upload's filename before forwarding, so a traversal basename never
    reaches rag even if rag's own sanitisation ever regresses."""
    captured = {}

    def _capture(request):
        # respx gives us the request object; inspect the multipart body
        # to verify the filename that went upstream.
        captured["body"] = request.content
        return Response(
            200, json={"files_seen": 1, "chunks_indexed": 1, "skipped": []}
        )

    respx.post(f"{RAG_BASE}/ingest").mock(side_effect=_capture)
    r = client.post(
        "/api/knowledge/ingest",
        headers=AUTH,
        files=[("files", ("../../../etc/pwned.md", b"# ok\n" + b"x " * 100, "text/markdown"))],
    )
    assert r.status_code == 200, r.text
    # The forwarded multipart should carry only the basename — no
    # "../" sequences reach rag.
    assert b"pwned.md" in captured["body"]
    assert b"../" not in captured["body"]
