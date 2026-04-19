"""Smoke tests for the dashboard scaffold: health endpoint, auth gate,
static UI served at /, and the legacy-path fallback for the API key."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from getbased_dashboard.config import DashboardConfig, _resolve_key_file
from getbased_dashboard.server import create_app


# ─── /api/health ────────────────────────────────────────────────────────

def test_health_is_unauthenticated(client: TestClient) -> None:
    """Health must return without a bearer — the frontend calls it before
    it has any key to know whether the server is even alive."""
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["has_api_key"] is True
    assert body["lens_url"] == "http://lens.test:8322"


def test_health_reports_missing_key(tmp_path: Path) -> None:
    cfg = DashboardConfig(api_key_file=tmp_path / "does-not-exist")
    c = TestClient(create_app(cfg))
    body = c.get("/api/health").json()
    assert body["has_api_key"] is False


# ─── /api/auth/check ───────────────────────────────────────────────────

def test_auth_check_requires_bearer(client: TestClient) -> None:
    r = client.get("/api/auth/check")
    assert r.status_code == 401
    assert "Missing Bearer" in r.json()["error"]


def test_auth_check_rejects_wrong_key(client: TestClient) -> None:
    r = client.get("/api/auth/check", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert "Invalid API key" in r.json()["error"]


def test_auth_check_accepts_right_key(client: TestClient) -> None:
    r = client.get(
        "/api/auth/check",
        headers={"Authorization": "Bearer test-dashboard-key"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_reveal_api_key_requires_auth(client: TestClient) -> None:
    assert client.get("/api/auth/api-key").status_code == 401


def test_reveal_api_key_returns_plaintext_when_authed(
    client: TestClient, key_file
) -> None:
    """The UI's show/copy buttons hit this endpoint to reveal the
    bearer token. The caller is already authenticated with the same
    key, so returning it isn't an escalation — just saves them a
    terminal round-trip to run `lens key`."""
    r = client.get(
        "/api/auth/api-key",
        headers={"Authorization": "Bearer test-dashboard-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["api_key"] == "test-dashboard-key"
    assert body["api_key_file"] == str(key_file)


def test_reveal_api_key_tracks_on_disk_rotation(
    client: TestClient, key_file
) -> None:
    """Rotating the key file on disk without restarting the dashboard
    should flow through — the endpoint reads fresh per call."""
    key_file.write_text("rotated-key")
    r = client.get(
        "/api/auth/api-key", headers={"Authorization": "Bearer rotated-key"}
    )
    assert r.status_code == 200
    assert r.json()["api_key"] == "rotated-key"


def test_auth_check_503_when_no_key_on_disk(tmp_path: Path) -> None:
    """If there's no key file at all, dashboard can't authenticate anyone
    — return 503 (service misconfigured) rather than 401 so the frontend
    can distinguish 'wrong key' from 'no rag running yet'."""
    cfg = DashboardConfig(api_key_file=tmp_path / "does-not-exist")
    c = TestClient(create_app(cfg))
    r = c.get("/api/auth/check", headers={"Authorization": "Bearer x"})
    assert r.status_code == 503
    assert "No API key found" in r.json()["error"]


def test_auth_reads_key_file_fresh_each_request(
    key_file: Path, client: TestClient
) -> None:
    """Rotating the key by editing the file should work without a dashboard
    restart. This is the whole reason we re-read the file per-request."""
    key_file.write_text("rotated-key")
    # Old key now fails
    r = client.get(
        "/api/auth/check", headers={"Authorization": "Bearer test-dashboard-key"}
    )
    assert r.status_code == 401
    # New key passes
    r = client.get("/api/auth/check", headers={"Authorization": "Bearer rotated-key"})
    assert r.status_code == 200


# ─── Static UI mount ───────────────────────────────────────────────────

def test_index_html_served_at_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "getbased-dashboard" in r.text
    assert '<nav class="tabs">' in r.text


def test_static_assets_served(client: TestClient) -> None:
    # CSS and JS must ship with the package (see pyproject package-data).
    r = client.get("/styles.css")
    assert r.status_code == 200
    assert "--accent" in r.text
    r = client.get("/app.js")
    assert r.status_code == 200
    assert "bootstrap" in r.text


# ─── Key-file resolution with legacy fallback ──────────────────────────

def test_resolve_key_file_new_location(tmp_path: Path, monkeypatch) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "getbased" / "lens").mkdir(parents=True)
    (xdg / "getbased" / "lens" / "api_key").write_text("new")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(home))
    assert _resolve_key_file() == xdg / "getbased" / "lens" / "api_key"


def test_resolve_key_file_legacy_fallback(tmp_path: Path, monkeypatch) -> None:
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    home = tmp_path / "home"
    (home / ".hermes" / "rag").mkdir(parents=True)
    (home / ".hermes" / "rag" / "lens_api_key").write_text("legacy")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(home))
    assert _resolve_key_file() == home / ".hermes" / "rag" / "lens_api_key"


def test_resolve_key_file_no_files(tmp_path: Path, monkeypatch) -> None:
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(home))
    # Neither path exists — return the new default for a fresh install.
    assert _resolve_key_file() == xdg / "getbased" / "lens" / "api_key"
