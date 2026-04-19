"""Tests for _maybe_load_user_env in lens.config.

Mirrors the mcp test for parity. The rag package is less Hermes-critical
(Hermes runs the legacy /home/elkim/kruse-corpus/lens_server.py, not this
one) but the same safety rules apply: no flag → no read.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from lens.config import LensConfig, _maybe_load_user_env


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "getbased"
    d.mkdir()
    path = d / "env"
    path.write_text("LENS_HOST=from_file.example\n")
    return path


def test_noop_without_managed_flag(env_file, monkeypatch):
    monkeypatch.delenv("GETBASED_STACK_MANAGED", raising=False)
    monkeypatch.delenv("LENS_HOST", raising=False)

    _maybe_load_user_env()

    assert "LENS_HOST" not in os.environ


def test_loads_when_managed(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.delenv("LENS_HOST", raising=False)

    _maybe_load_user_env()

    assert os.environ["LENS_HOST"] == "from_file.example"


def test_explicit_env_wins(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.setenv("LENS_HOST", "from_shell.example")

    _maybe_load_user_env()

    assert os.environ["LENS_HOST"] == "from_shell.example"


def test_escape_hatch(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.setenv("GETBASED_NO_ENV_FILE", "1")
    monkeypatch.delenv("LENS_HOST", raising=False)

    _maybe_load_user_env()

    assert "LENS_HOST" not in os.environ


def test_missing_file_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")

    _maybe_load_user_env()  # must not raise


def test_from_env_invokes_loader(env_file, monkeypatch):
    """LensConfig.from_env() is the single entry point for env-driven
    configuration; it must wire the loader in."""
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.delenv("LENS_HOST", raising=False)

    cfg = LensConfig.from_env()

    assert cfg.host == "from_file.example"


def test_from_env_parity_without_flag(env_file, monkeypatch):
    """Hermes contract: without the flag, from_env() ignores the shared
    file entirely and uses only explicit env + defaults."""
    monkeypatch.delenv("GETBASED_STACK_MANAGED", raising=False)
    monkeypatch.setenv("LENS_HOST", "explicit.example")

    cfg = LensConfig.from_env()

    assert cfg.host == "explicit.example"
