"""Unit tests for every MCP tool with mocked HTTP backends.

Uses respx to intercept httpx calls. Verifies:
- Each tool sends the correct HTTP method, path, and auth header
- Each tool correctly parses happy-path responses
- Errors from the backend are surfaced as user-visible strings (not raised)
- Unset prereqs (missing token, missing key) return a helpful message
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response


# ═══════════════════════════════════════════════════════════════════════
# Blood-work tools (sync gateway → /api/context)
# ═══════════════════════════════════════════════════════════════════════

GATEWAY_CONTEXT_URL = "https://gateway.test/api/context"
LENS_URL_PREFIX = "http://lens.test:8322"


@pytest.mark.asyncio
@respx.mock
async def test_getbased_list_profiles_happy(gm) -> None:
    respx.get(GATEWAY_CONTEXT_URL).mock(return_value=Response(200, json={
        "profiles": [{"id": "abc", "name": "Main"}, {"id": "def", "name": "Family"}],
    }))
    out = await gm.getbased_list_profiles()
    assert "abc  Main" in out
    assert "def  Family" in out


@pytest.mark.asyncio
@respx.mock
async def test_getbased_list_profiles_empty(gm) -> None:
    respx.get(GATEWAY_CONTEXT_URL).mock(return_value=Response(200, json={"profiles": []}))
    out = await gm.getbased_list_profiles()
    assert out == "No profiles found"


@pytest.mark.asyncio
async def test_getbased_list_profiles_no_token(gm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "TOKEN", "")
    out = await gm.getbased_list_profiles()
    assert "GETBASED_TOKEN not set" in out


@pytest.mark.asyncio
@respx.mock
async def test_getbased_lab_context_happy(gm) -> None:
    respx.get(GATEWAY_CONTEXT_URL).mock(return_value=Response(200, json={
        "profileId": "abc",
        "updatedAt": "2026-04-18T12:00:00Z",
        "context": "[section:hormones]\ntestosterone: 18.4 nmol/L\n[/section:hormones]",
    }))
    out = await gm.getbased_lab_context()
    assert "Profile: abc" in out
    assert "Updated: 2026-04-18" in out
    assert "testosterone" in out


@pytest.mark.asyncio
@respx.mock
async def test_getbased_lab_context_gateway_error(gm) -> None:
    respx.get(GATEWAY_CONTEXT_URL).mock(return_value=Response(500))
    out = await gm.getbased_lab_context()
    assert "Error" in out
    assert "500" in out


@pytest.mark.asyncio
@respx.mock
async def test_getbased_section_lists_index_when_no_arg(gm) -> None:
    respx.get(GATEWAY_CONTEXT_URL).mock(return_value=Response(200, json={
        "context": (
            "[section:hormones updated:2026-04-01]\nfoo\nbar\n[/section:hormones]\n"
            "[section:lipids]\nbaz\n[/section:lipids]"
        ),
    }))
    out = await gm.getbased_section()
    assert "Available sections" in out
    assert "hormones updated:2026-04-01" in out
    assert "lipids" in out


@pytest.mark.asyncio
@respx.mock
async def test_getbased_section_returns_prefix_match(gm) -> None:
    respx.get(GATEWAY_CONTEXT_URL).mock(return_value=Response(200, json={
        "context": "[section:hormones updated:2026-04-01]\nTSH 1.9 uIU/mL\n[/section:hormones]",
    }))
    out = await gm.getbased_section(section="hormones")
    assert "TSH 1.9 uIU/mL" in out
    assert "hormones updated:2026-04-01" in out


@pytest.mark.asyncio
@respx.mock
async def test_getbased_section_not_found(gm) -> None:
    respx.get(GATEWAY_CONTEXT_URL).mock(return_value=Response(200, json={
        "context": "[section:hormones]\nfoo\n[/section:hormones]",
    }))
    out = await gm.getbased_section(section="does-not-exist")
    assert "not found" in out
    assert "hormones" in out


# ═══════════════════════════════════════════════════════════════════════
# Knowledge-base tools (Lens RAG server)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_knowledge_search_happy(gm) -> None:
    route = respx.post(f"{LENS_URL_PREFIX}/query").mock(return_value=Response(200, json={
        "chunks": [
            {"text": "Vitamin D is a secosteroid hormone", "source": "notes.md", "score": 0.82},
            {"text": "UVB converts 7-dehydrocholesterol", "source": "mech.md", "score": 0.71},
        ],
    }))
    out = await gm.knowledge_search(query="vitamin D", n_results=2)
    # Outbound request carried the correct payload + bearer
    assert route.called
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer test-lens-key"
    import json
    body = json.loads(req.content)
    assert body == {"version": 1, "query": "vitamin D", "top_k": 2}
    # Response rendered
    assert "[1] notes.md" in out
    assert "Vitamin D is a secosteroid" in out
    assert "[2] mech.md" in out


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_search_clamps_n_results(gm) -> None:
    route = respx.post(f"{LENS_URL_PREFIX}/query").mock(return_value=Response(200, json={"chunks": []}))
    # n_results=99 → clamped to 10
    await gm.knowledge_search(query="x", n_results=99)
    import json
    body = json.loads(route.calls[0].request.content)
    assert body["top_k"] == 10
    # n_results=0 → clamped to 1
    await gm.knowledge_search(query="x", n_results=0)
    body = json.loads(route.calls[1].request.content)
    assert body["top_k"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_search_empty_results(gm) -> None:
    respx.post(f"{LENS_URL_PREFIX}/query").mock(return_value=Response(200, json={"chunks": []}))
    out = await gm.knowledge_search(query="anything")
    assert "No results found" in out


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_search_lens_down(gm) -> None:
    respx.post(f"{LENS_URL_PREFIX}/query").mock(side_effect=__import__("httpx").ConnectError("connection refused"))
    out = await gm.knowledge_search(query="x")
    assert "Knowledge search error" in out
    assert "not reachable" in out


@pytest.mark.asyncio
async def test_knowledge_search_no_key(gm, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point LENS_API_KEY_FILE at a file that doesn't exist.
    monkeypatch.setattr(gm, "LENS_API_KEY_FILE", "/nonexistent/path")
    out = await gm.knowledge_search(query="x")
    assert "Knowledge search error" in out
    assert "not found" in out


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_list_libraries_happy(gm) -> None:
    respx.get(f"{LENS_URL_PREFIX}/libraries").mock(return_value=Response(200, json={
        "activeId": "lib1",
        "libraries": [
            {"id": "lib1", "name": "Research"},
            {"id": "lib2", "name": "Guides"},
        ],
    }))
    out = await gm.knowledge_list_libraries()
    assert "lib1  Research  (active)" in out
    assert "lib2  Guides" in out
    assert "(active)" not in out.split("lib2")[1]  # only lib1 is active


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_list_libraries_empty(gm) -> None:
    respx.get(f"{LENS_URL_PREFIX}/libraries").mock(return_value=Response(200, json={
        "activeId": "",
        "libraries": [],
    }))
    out = await gm.knowledge_list_libraries()
    assert "No libraries found" in out


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_activate_library_happy(gm) -> None:
    route = respx.post(f"{LENS_URL_PREFIX}/libraries/lib2/activate").mock(
        return_value=Response(200, json={
            "activeId": "lib2",
            "libraries": [{"id": "lib1", "name": "R"}, {"id": "lib2", "name": "Guides"}],
        })
    )
    out = await gm.knowledge_activate_library(library_id="lib2")
    assert route.called
    assert "Active library is now" in out
    assert "Guides" in out


@pytest.mark.asyncio
async def test_knowledge_activate_library_requires_id(gm) -> None:
    out = await gm.knowledge_activate_library(library_id="")
    assert "library_id is required" in out


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_activate_library_404(gm) -> None:
    respx.post(f"{LENS_URL_PREFIX}/libraries/bogus/activate").mock(
        return_value=Response(404, json={"error": "Library not found"})
    )
    out = await gm.knowledge_activate_library(library_id="bogus")
    assert "Activate library error" in out
    assert "404" in out


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_stats_happy(gm) -> None:
    respx.get(f"{LENS_URL_PREFIX}/stats").mock(return_value=Response(200, json={
        "total_chunks": 1234,
        "documents": [
            {"source": "paper-A.pdf", "chunks": 800},
            {"source": "paper-B.pdf", "chunks": 434},
        ],
    }))
    out = await gm.knowledge_stats()
    assert "Total chunks: 1234" in out
    assert "800  paper-A.pdf" in out
    assert "434  paper-B.pdf" in out


@pytest.mark.asyncio
@respx.mock
async def test_knowledge_stats_empty_library(gm) -> None:
    respx.get(f"{LENS_URL_PREFIX}/stats").mock(return_value=Response(200, json={
        "total_chunks": 0,
        "documents": [],
    }))
    out = await gm.knowledge_stats()
    assert "Active library is empty" in out


# ═══════════════════════════════════════════════════════════════════════
# getbased_lens_config — returns a paste-ready URL + key block
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_getbased_lens_config_happy(gm) -> None:
    out = await gm.getbased_lens_config()
    assert "http://lens.test:8322/query" in out
    assert "test-lens-key" in out
    assert "Knowledge Base → External server" in out


@pytest.mark.asyncio
async def test_getbased_lens_config_no_key(gm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gm, "LENS_API_KEY_FILE", "/nonexistent/path")
    out = await gm.getbased_lens_config()
    assert "Lens API key not found" in out
