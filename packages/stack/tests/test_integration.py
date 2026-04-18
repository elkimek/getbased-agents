"""End-to-end integration test — real RAG subprocess + real MCP tool calls.

Each sibling repo has its own hermetic unit tests (mocked HTTP or Qdrant in
tmp dir). This suite exists because neither of those catches protocol drift
between the two: the MCP tests only verify the MCP side, and the RAG tests
only verify the RAG side. Here we stand up an actual `lens serve` process,
read its generated API key, and call every MCP tool against it through the
normal function entry points.

Runs in CI via `pytest`. Skips cleanly if either package isn't installed
(keeps the test module importable even in a partial install).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Iterator

import pytest


# ── Availability guards — skip cleanly if the stack isn't installed ──

def _has(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not (_has("lens") and _has("getbased_mcp")),
    reason="integration test requires both getbased-rag and getbased-mcp installed",
)


# ── Helpers ──────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http_ready(url: str, timeout: float = 60.0) -> None:
    """Poll until the server answers, or fail the test. The first call takes
    a few seconds because the embedder model downloads on cold start."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
        time.sleep(0.5)
    raise RuntimeError(f"server at {url} never came up: {last_err}")


# ── Fixtures ─────────────────────────────────────────────────────────

def _seed_via_cli(data_dir: Path) -> Path:
    """Write a fixture file and run `lens ingest` against it. Must run
    BEFORE `lens serve` starts — Qdrant local storage is exclusive-locked
    by whichever process touches it first, so server+ingest can't share
    a data dir at the same time."""
    fixture_dir = Path(__file__).parent / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "vitamin-d.md").write_text(
        "Vitamin D is a secosteroid hormone synthesised in skin when UVB "
        "hits 7-dehydrocholesterol. " + "filler filler filler " * 20
    )
    env = {
        **os.environ,
        "LENS_DATA_DIR": str(data_dir),
        "LENS_SIMILARITY_FLOOR": "0.0",
    }
    r = subprocess.run(
        ["lens", "ingest", str(fixture_dir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, f"ingest failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
    return fixture_dir


@pytest.fixture(scope="module")
def seeded_lens(tmp_path_factory) -> Iterator[dict]:
    """Seed a fresh tmp data dir via `lens ingest`, THEN start `lens serve`
    against the same dir. The server picks up the already-ingested chunks
    on first library access. Yields {url, key_file, data_dir}."""
    data_dir = tmp_path_factory.mktemp("lens-data")
    _seed_via_cli(data_dir)

    port = _free_port()
    env = {
        **os.environ,
        "LENS_DATA_DIR": str(data_dir),
        "LENS_HOST": "127.0.0.1",
        "LENS_PORT": str(port),
        "LENS_SIMILARITY_FLOOR": "0.0",
    }
    proc = subprocess.Popen(
        ["lens", "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_http_ready(f"http://127.0.0.1:{port}/health", timeout=90)
        yield {
            "url": f"http://127.0.0.1:{port}",
            "key_file": data_dir / "api_key",
            "data_dir": data_dir,
        }
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.returncode is None:
            proc.kill()


@pytest.fixture
def mcp_env(seeded_lens: dict, monkeypatch: pytest.MonkeyPatch):
    """Reload getbased_mcp with env vars pointing at the subprocess server.
    Module-level globals pick up LENS_URL / LENS_API_KEY_FILE at import time,
    so we need a reload after monkeypatching."""
    import importlib

    import getbased_mcp

    monkeypatch.setenv("LENS_URL", seeded_lens["url"])
    monkeypatch.setenv("LENS_API_KEY_FILE", str(seeded_lens["key_file"]))
    # No gateway token — skip blood-work tools. We're only testing the
    # knowledge-base end of the protocol here.
    monkeypatch.setenv("GETBASED_TOKEN", "")
    importlib.reload(getbased_mcp)
    return getbased_mcp


# ── Tests ────────────────────────────────────────────────────────────

@pytest.mark.timeout(180)
def test_full_knowledge_tool_chain(mcp_env) -> None:
    """Run every knowledge-* MCP tool against the real Lens and verify the
    round-trip produces sensible output. This is the integration test that
    would have caught the original 'MCP is one version behind RAG' drift."""

    async def run() -> None:
        # 1. Discover the active library
        list_out = await mcp_env.knowledge_list_libraries()
        assert "Libraries:" in list_out
        assert "(active)" in list_out

        # 2. Inspect its stats — should reflect the ingested document
        stats_out = await mcp_env.knowledge_stats()
        assert "Total chunks:" in stats_out
        assert "vitamin-d.md" in stats_out

        # 3. Search for something the seeded document contains
        search_out = await mcp_env.knowledge_search(query="vitamin D secosteroid hormone", n_results=3)
        assert "[1]" in search_out
        assert "vitamin-d.md" in search_out

        # 4. Config tool returns a paste-ready config
        config_out = await mcp_env.getbased_lens_config()
        assert mcp_env.LENS_URL in config_out
        assert "External server" in config_out

        # 5. Create a second library, activate it, verify switch took effect
        import httpx

        key = mcp_env._read_lens_key()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{mcp_env.LENS_URL}/libraries",
                headers={"Authorization": f"Bearer {key}"},
                json={"name": "Secondary"},
            )
            r.raise_for_status()
            new_id = r.json()["library"]["id"]

        activate_out = await mcp_env.knowledge_activate_library(library_id=new_id)
        assert "Active library is now" in activate_out
        assert "Secondary" in activate_out

        # Stats on the new library: empty
        new_stats = await mcp_env.knowledge_stats()
        assert "Active library is empty" in new_stats

    asyncio.run(run())


@pytest.mark.timeout(30)
def test_error_surfacing_when_lens_unreachable(mcp_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point MCP at a port nothing is listening on; tools should surface a
    friendly 'not reachable' message, not raise."""
    import importlib

    import getbased_mcp

    monkeypatch.setenv("LENS_URL", "http://127.0.0.1:1")  # port 1 is privileged + closed
    importlib.reload(getbased_mcp)

    async def run() -> None:
        out = await getbased_mcp.knowledge_search(query="anything")
        assert "Knowledge search error" in out
        assert "not reachable" in out

    asyncio.run(run())
