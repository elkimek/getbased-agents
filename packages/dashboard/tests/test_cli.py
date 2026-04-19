"""Tests for the getbased-dashboard CLI — specifically `serve` startup
output, which now prints a magic login URL so users don't need to
paste the bearer manually. If the URL breaks (wrong format, missing
key, stale host/port), the whole zero-paste flow falls apart.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from getbased_dashboard.cli import app


def test_serve_prints_one_click_login_url(
    tmp_path: Path, monkeypatch
) -> None:
    """With a valid api_key file on disk, `getbased-dashboard serve`
    should print a clickable URL with the key as a query param.
    uvicorn itself is stubbed out so the test doesn't block on the
    server starting."""
    key_file = tmp_path / "api_key"
    key_file.write_text("test-bearer-xyz-123")
    monkeypatch.setenv("LENS_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("DASHBOARD_PORT", "9321")

    runner = CliRunner()
    # Patch uvicorn.run so the command returns after printing, instead
    # of actually starting a server.
    with patch("getbased_dashboard.cli.uvicorn.run", return_value=None):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "getbased-dashboard → http://127.0.0.1:9321" in result.output
    assert "Open the dashboard with one click" in result.output
    # Exact URL format the frontend captures
    assert "http://127.0.0.1:9321/?key=test-bearer-xyz-123" in result.output


def test_serve_warns_when_no_key_on_disk(tmp_path: Path, monkeypatch) -> None:
    """Fresh install before `lens serve` has ever run — no api_key
    file yet. Serve should come up (so rag can write a key later and
    dashboard will find it next restart) but warn instead of printing
    a broken login URL."""
    missing = tmp_path / "never-created-yet"
    monkeypatch.setenv("LENS_API_KEY_FILE", str(missing))
    monkeypatch.setenv("DASHBOARD_PORT", "9322")

    runner = CliRunner()
    with patch("getbased_dashboard.cli.uvicorn.run", return_value=None):
        result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    # Warning instead of a broken key-less URL
    assert "no key found" in result.output
    assert "?key=" not in result.output, "Must not emit a login URL with empty key"


def test_info_command_shows_config(tmp_path: Path, monkeypatch) -> None:
    key_file = tmp_path / "api_key"
    key_file.write_text("probe-key")
    monkeypatch.setenv("LENS_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("LENS_URL", "http://127.0.0.1:8322")

    runner = CliRunner()
    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0
    assert "lens_url:" in result.output
    assert "api_key_present: True" in result.output
    # Never leaks the actual key in `info` output — that's what the UI's
    # authed show/copy affordance is for.
    assert "probe-key" not in result.output
