"""Shared fixtures for MCP tool tests.

Every test runs against a mocked httpx transport — no real gateway, no
real Lens server. Keeps tests fast and hermetic, and catches protocol
drift if either backend's response shape changes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated Lens API key file — the module reads this at import time via
    env var defaults, so we have to monkeypatch + reload to get a clean
    pickup per test."""
    key_file = tmp_path / "api_key"
    key_file.write_text("test-lens-key")
    monkeypatch.setenv("LENS_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("LENS_URL", "http://lens.test:8322")
    monkeypatch.setenv("GETBASED_TOKEN", "test-gateway-token")
    monkeypatch.setenv("GETBASED_GATEWAY", "https://gateway.test")
    return key_file


@pytest.fixture
def gm(tmp_key_file: Path):
    """Reload the module under test so module-level env reads pick up the
    test overrides. Without this the globals (TOKEN, LENS_URL) stay set
    from whatever was in the real environment at import time."""
    import importlib

    import getbased_mcp

    importlib.reload(getbased_mcp)
    return getbased_mcp
