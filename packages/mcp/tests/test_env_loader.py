"""Tests for _maybe_load_user_env in getbased_mcp.

This is the critical guardrail for existing deployments like Hermes: without
GETBASED_STACK_MANAGED=1, the loader MUST be a no-op — no filesystem access,
no env mutation. Hermes doesn't set the flag and doesn't use the shared env
file, so these tests protect its MCP behavior from any regression.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG_CONFIG_HOME with a getbased/env file containing a
    sentinel value. Tests decide whether the loader sees it by toggling
    GETBASED_STACK_MANAGED / GETBASED_NO_ENV_FILE."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "getbased"
    d.mkdir()
    path = d / "env"
    path.write_text("GETBASED_TOKEN=from_file\nLENS_URL=http://from-file:9999\n")
    return path


def _load(monkeypatch: pytest.MonkeyPatch):
    """Freshly import and return the loader function. Reload ensures we
    don't accidentally share state between tests."""
    import importlib

    import getbased_mcp

    importlib.reload(getbased_mcp)
    return getbased_mcp._maybe_load_user_env


def test_noop_without_managed_flag(env_file, monkeypatch):
    """Hermes-safety contract: no flag → no read, no mutation."""
    monkeypatch.delenv("GETBASED_STACK_MANAGED", raising=False)
    monkeypatch.delenv("GETBASED_TOKEN", raising=False)
    monkeypatch.delenv("LENS_URL", raising=False)

    loader = _load(monkeypatch)
    loader()

    assert "GETBASED_TOKEN" not in os.environ
    assert "LENS_URL" not in os.environ


def test_loads_when_managed(env_file, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.delenv("GETBASED_TOKEN", raising=False)
    monkeypatch.delenv("LENS_URL", raising=False)

    loader = _load(monkeypatch)
    loader()

    assert os.environ["GETBASED_TOKEN"] == "from_file"
    assert os.environ["LENS_URL"] == "http://from-file:9999"


def test_explicit_env_wins_over_file(env_file, monkeypatch):
    """setdefault semantics — user/systemd-provided env must not be
    overridden by the shared file."""
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.setenv("GETBASED_TOKEN", "from_shell")

    loader = _load(monkeypatch)
    loader()

    assert os.environ["GETBASED_TOKEN"] == "from_shell"


def test_escape_hatch(env_file, monkeypatch):
    """GETBASED_NO_ENV_FILE=1 bails even when managed — debugging/override use."""
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.setenv("GETBASED_NO_ENV_FILE", "1")
    monkeypatch.delenv("GETBASED_TOKEN", raising=False)

    loader = _load(monkeypatch)
    loader()

    assert "GETBASED_TOKEN" not in os.environ


def test_missing_file_silent(tmp_path, monkeypatch):
    """No env file at the expected path → no error, no mutation."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.delenv("GETBASED_TOKEN", raising=False)

    loader = _load(monkeypatch)
    loader()  # must not raise

    assert "GETBASED_TOKEN" not in os.environ


def test_malformed_lines_skipped(tmp_path, monkeypatch):
    """A typo'd env file must not crash MCP startup."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "getbased"
    d.mkdir()
    (d / "env").write_text(
        "# a comment\n"
        "\n"
        "NO_EQUALS_SIGN\n"
        "GOOD_VAR=ok\n"
        "   =missing_key\n"
    )
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    monkeypatch.delenv("GOOD_VAR", raising=False)

    loader = _load(monkeypatch)
    loader()

    assert os.environ["GOOD_VAR"] == "ok"


def test_quoted_values_unwrapped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "getbased"
    d.mkdir()
    (d / "env").write_text('DOUBLE="dv"\nSINGLE=\'sv\'\nBARE=bv\n')
    monkeypatch.setenv("GETBASED_STACK_MANAGED", "1")
    for k in ("DOUBLE", "SINGLE", "BARE"):
        monkeypatch.delenv(k, raising=False)

    loader = _load(monkeypatch)
    loader()

    assert os.environ["DOUBLE"] == "dv"
    assert os.environ["SINGLE"] == "sv"
    assert os.environ["BARE"] == "bv"


def test_module_import_parity_without_flag(monkeypatch, tmp_path):
    """Smoke: importing getbased_mcp without the managed flag must not
    read or mutate anything related to the shared env file. This is the
    end-to-end Hermes contract — if this ever fails, we risk regressing
    a live deployment."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "getbased"
    d.mkdir()
    # Would poison LENS_URL if the loader ran
    (d / "env").write_text("LENS_URL=http://POISON:1234\n")

    monkeypatch.delenv("GETBASED_STACK_MANAGED", raising=False)
    monkeypatch.setenv("LENS_URL", "http://explicit:8322")
    monkeypatch.setenv("GETBASED_TOKEN", "real")

    import importlib

    import getbased_mcp

    importlib.reload(getbased_mcp)

    # Explicit env must survive; poison must not have been loaded
    assert getbased_mcp.LENS_URL == "http://explicit:8322"
    assert "POISON" not in getbased_mcp.LENS_URL
