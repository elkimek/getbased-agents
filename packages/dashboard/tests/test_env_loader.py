"""Tests for _maybe_load_user_env in getbased_dashboard.config.

Same contract as the other two: no GETBASED_STACK_MANAGED flag → no read,
no mutation. Protects any existing dashboard invocation (including the one
currently running on Hermes VM via nohup) from surprise env changes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from getbased_dashboard.config import DashboardConfig, _maybe_load_user_env


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "getbased"
    d.mkdir()
    path = d / "env"
    path.write_text("DASHBOARD_HOST=from_file.example\n")
    return path


def test_noop_without_managed_flag(env_file, monkeypatch):
    monkeypatch.delenv("GETBASED_STACK_MANAGED", raising=False)
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)

    _maybe_load_user_env()

    assert "DASHBOARD_HOST" not in os.environ


def test_loads_when_managed(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)

    _maybe_load_user_env()

    assert os.environ["DASHBOARD_HOST"] == "from_file.example"


def test_explicit_env_wins(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.setenv("DASHBOARD_HOST", "explicit.example")

    _maybe_load_user_env()

    assert os.environ["DASHBOARD_HOST"] == "explicit.example"


def test_escape_hatch(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.setenv("GETBASED_NO_ENV_FILE", "1")
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)

    _maybe_load_user_env()

    assert "DASHBOARD_HOST" not in os.environ


def test_missing_file_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")

    _maybe_load_user_env()


def test_from_env_invokes_loader(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)

    cfg = DashboardConfig.from_env()

    assert cfg.host == "from_file.example"


def test_from_env_parity_without_flag(env_file, monkeypatch):
    monkeypatch.delenv("GETBASED_STACK_MANAGED", raising=False)
    monkeypatch.setenv("DASHBOARD_HOST", "explicit.example")

    cfg = DashboardConfig.from_env()

    assert cfg.host == "explicit.example"
