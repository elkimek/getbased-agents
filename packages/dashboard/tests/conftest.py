"""Shared fixtures. Every test gets an isolated tmp API key file and a
fresh FastAPI client — no shared state between tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from getbased_dashboard.config import DashboardConfig
from getbased_dashboard.server import create_app


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """API key file with a known token. Dashboard reads this for every
    authed request — matches the model used in production (no in-memory
    key cache, so key rotation via file edit works without restart)."""
    path = tmp_path / "api_key"
    path.write_text("test-dashboard-key")
    return path


@pytest.fixture
def cfg(key_file: Path, tmp_path: Path) -> DashboardConfig:
    return DashboardConfig(
        host="127.0.0.1",
        port=8323,
        lens_url="http://lens.test:8322",
        api_key_file=key_file,
        activity_log=tmp_path / "activity.jsonl",
    )


@pytest.fixture
def client(cfg: DashboardConfig) -> TestClient:
    return TestClient(create_app(cfg))
