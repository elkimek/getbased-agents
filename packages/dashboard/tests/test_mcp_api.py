"""Tests for the MCP tab API. Config generator is pure logic — tested
by inspecting the returned content. Env viewer re-imports getbased_mcp
with overridden env; we verify the module's resolved defaults surface
correctly. Stdio tester is covered by monkeypatching the subprocess
factory so we don't depend on the MCP binary actually being on PATH."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-dashboard-key"}


# ─── /api/mcp/env ────────────────────────────────────────────────────

def test_env_requires_auth(client: TestClient) -> None:
    assert client.get("/api/mcp/env").status_code == 401


def test_env_reports_lens_url_and_key_presence(
    client: TestClient, key_file: Path
) -> None:
    r = client.get("/api/mcp/env", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["lens_url"] == "http://lens.test:8322"
    # The key file was populated by the fixture; we should see present=True
    assert body["lens_api_key_present"] is True
    # Token isn't set in tests — should report absent without revealing value
    assert body["getbased_token_present"] is False
    assert "getbased_token" not in body  # never echo the secret itself


# ─── /api/mcp/config — per-client templates ──────────────────────────

def test_config_requires_auth(client: TestClient) -> None:
    assert client.get("/api/mcp/config?client=claude-desktop").status_code == 401


def test_config_claude_desktop_json_shape(client: TestClient) -> None:
    r = client.get("/api/mcp/config?client=claude-desktop", headers=AUTH)
    assert r.status_code == 200
    out = r.json()
    assert out["client"] == "claude-desktop"
    assert "claude_desktop_config.json" in out["filename"]
    body = json.loads(out["content"])
    assert "mcpServers" in body
    entry = body["mcpServers"]["getbased"]
    assert entry["command"]  # something — path or bare name
    assert entry["env"]["LENS_URL"] == "http://lens.test:8322"
    # Token placeholder, not a real value — user supplies this themselves
    assert "<paste" in entry["env"]["GETBASED_TOKEN"]


@pytest.mark.parametrize("client_name", ["claude-code", "cursor", "cline"])
def test_config_other_json_clients_share_shape(
    client: TestClient, client_name: str
) -> None:
    r = client.get(f"/api/mcp/config?client={client_name}", headers=AUTH)
    assert r.status_code == 200
    body = json.loads(r.json()["content"])
    assert "mcpServers" in body
    assert "getbased" in body["mcpServers"]


def test_config_hermes_is_yaml_with_enabled_tools(client: TestClient) -> None:
    r = client.get("/api/mcp/config?client=hermes", headers=AUTH)
    assert r.status_code == 200
    out = r.json()
    assert "config.yaml" in out["filename"]
    txt = out["content"]
    assert "mcp_servers:" in txt
    assert "getbased:" in txt
    # Hermes-specific — explicit tool allowlist
    assert "enabled_tools:" in txt
    assert "- knowledge_search" in txt
    assert "- knowledge_list_libraries" in txt
    # Env block — keys present, token is a placeholder
    assert "GETBASED_TOKEN:" in txt
    assert "paste from getbased" in txt
    assert "LENS_URL:" in txt


def test_config_unknown_client_rejected(client: TestClient) -> None:
    r = client.get("/api/mcp/config?client=notreal", headers=AUTH)
    # FastAPI Literal validation returns 422; our own check returns 400.
    # Either enforces "not a supported client" — accept both.
    assert r.status_code in (400, 422)


# ─── /api/mcp/test — stdio probe ─────────────────────────────────────

def test_test_requires_auth(client: TestClient) -> None:
    assert client.post("/api/mcp/test").status_code == 401


def test_test_reports_tools_when_mcp_responds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch the stdio probe to return a canned response — the real MCP
    CLI isn't guaranteed to be on PATH in all CI environments. The probe
    logic itself (JSON-RPC framing, readline order) is covered by the
    mcp package's own tests."""

    async def fake_probe(cfg, timeout_s: float = 10.0):
        return {
            "ok": True,
            "elapsed_ms": 42,
            "server_info": {"name": "getbased", "version": "1.0"},
            "tools": ["knowledge_search", "getbased_list_profiles"],
        }

    from getbased_dashboard.api import mcp as mcp_api

    monkeypatch.setattr(mcp_api, "_stdio_probe", fake_probe)

    r = client.post("/api/mcp/test", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "knowledge_search" in body["tools"]


def test_test_reports_error_when_mcp_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_probe(cfg, timeout_s: float = 10.0):
        return {"ok": False, "error": "MCP CLI 'getbased-mcp' not found on PATH."}

    from getbased_dashboard.api import mcp as mcp_api

    monkeypatch.setattr(mcp_api, "_stdio_probe", fake_probe)

    r = client.post("/api/mcp/test", headers=AUTH)
    assert r.status_code == 200  # probe succeeded; ok=False inside body
    body = r.json()
    assert body["ok"] is False
    assert "not found" in body["error"]


def test_mcp_command_path_prefers_same_venv_as_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: `shutil.which` searches PATH, but a dashboard started
    via `/path/to/.venv/bin/getbased-dashboard` doesn't have that venv
    on PATH. The MCP sibling binary sits next to the Python that
    launched us — check that neighbourhood first."""
    import sys
    from getbased_dashboard.api import mcp as mcp_api

    # Build a fake venv layout: bin/python + bin/getbased-mcp
    fake_venv_bin = tmp_path / "fakevenv" / "bin"
    fake_venv_bin.mkdir(parents=True)
    fake_python = fake_venv_bin / "python"
    fake_python.write_text("#!/bin/sh\nexec /usr/bin/env python3 \"$@\"")
    fake_python.chmod(0o755)
    fake_mcp = fake_venv_bin / "getbased-mcp"
    fake_mcp.write_text("#!/bin/sh\necho hi")
    fake_mcp.chmod(0o755)

    # Point sys.executable at the fake venv's python, and wipe PATH so
    # shutil.which cannot be the thing that saves us — this isolates the
    # "same venv as dashboard" branch.
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setenv("PATH", "/nonexistent")

    resolved = mcp_api._mcp_command_path()
    assert resolved == str(fake_mcp)


def test_mcp_command_path_falls_back_to_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the sibling binary isn't there, shutil.which kicks in."""
    import sys
    from getbased_dashboard.api import mcp as mcp_api

    bare_bin = tmp_path / "empty" / "bin"
    bare_bin.mkdir(parents=True)
    fake_python = bare_bin / "python"
    fake_python.write_text("")
    fake_python.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake_python))

    # Place getbased-mcp somewhere on PATH
    which_dir = tmp_path / "which-dir"
    which_dir.mkdir()
    which_target = which_dir / "getbased-mcp"
    which_target.write_text("#!/bin/sh\necho hi")
    which_target.chmod(0o755)
    monkeypatch.setenv("PATH", str(which_dir))

    assert mcp_api._mcp_command_path() == str(which_target)
