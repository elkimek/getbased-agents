"""CLI-level tests for getbased-stack.

These wire env_file + units + mcp_configs together through the argparse
dispatch. They validate behavior the end-user sees: flag parsing, exit
codes, stdout shape. Every systemctl/loginctl call is still injected via
UnitManager's shell boundary (mocked at the module level)."""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from getbased_agent_stack import cli, env_file, units
from getbased_agent_stack.units import CommandResult


@pytest.fixture
def stack_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate XDG_CONFIG_HOME, XDG_DATA_HOME, and HOME so nothing touches
    the real filesystem. Returns the isolated root."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USER", "tester")
    return tmp_path


@pytest.fixture
def fake_shell(monkeypatch: pytest.MonkeyPatch) -> "list":
    """Replace UnitManager's default shell so no real systemctl runs.
    Returns the command-log list for assertions."""
    calls: "list[list[str]]" = []

    def shell(cmd: "list[str]") -> CommandResult:
        calls.append(cmd)
        # Heuristic defaults: daemon-reload/enable/disable succeed;
        # is-active/is-enabled report 'not running' unless a test stubs.
        if cmd[:2] == ["loginctl", "show-user"]:
            return CommandResult(0, "Linger=no\n", "")
        return CommandResult(0, "", "")

    monkeypatch.setattr(units, "_real_shell", shell)
    return calls


def _run(argv: "list[str]") -> "tuple[int, str, str]":
    """Invoke the CLI with argv, capturing stdout/stderr. Catches the
    SystemExit argparse raises on argument errors — returns its code."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = int(e.code) if e.code is not None else 0
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


# ── help / version ────────────────────────────────────────────────────


def test_bare_invocation_shows_help():
    rc, out, _ = _run([])
    assert rc == 0
    assert "getbased-stack" in out
    assert "init" in out
    assert "install" in out


def test_help_flag():
    rc, out, _ = _run(["--help"])
    assert rc == 0
    assert "init" in out


def test_version_shows_packages(stack_home):
    rc, out, _ = _run(["version"])
    assert rc == 0
    assert "getbased-agent-stack" in out


# ── install / uninstall ───────────────────────────────────────────────


def test_install_writes_units_and_enables(stack_home, fake_shell):
    rc, out, _ = _run(["install"])
    assert rc == 0

    unit_dir = stack_home / "config" / "systemd" / "user"
    assert (unit_dir / "getbased-rag.service").exists()
    assert (unit_dir / "getbased-dashboard.service").exists()

    # daemon-reload, enable --now, and restart all run. The explicit restart
    # matters for re-running install.sh over an active stack after uv replaces
    # the tool environment: old Python processes must not keep serving from a
    # half-replaced venv.
    all_calls = [" ".join(cmd) for cmd in fake_shell]
    assert any("daemon-reload" in c for c in all_calls)
    assert any("enable --now" in c for c in all_calls)
    assert any("restart getbased-rag.service getbased-dashboard.service" in c for c in all_calls)


def test_install_no_enable_flag(stack_home, fake_shell):
    rc, _, _ = _run(["install", "--no-enable"])
    assert rc == 0
    # daemon-reload still ran, enable did not
    enable_calls = [c for c in fake_shell if c[:3] == ["systemctl", "--user", "enable"]]
    assert enable_calls == []


def test_uninstall_removes_units(stack_home, fake_shell):
    # Install first
    _run(["install"])
    unit_dir = stack_home / "config" / "systemd" / "user"
    assert (unit_dir / "getbased-rag.service").exists()

    rc, _, _ = _run(["uninstall"])
    assert rc == 0
    assert not (unit_dir / "getbased-rag.service").exists()
    assert not (unit_dir / "getbased-dashboard.service").exists()


def test_uninstall_delete_env_flag(stack_home, fake_shell):
    # Pre-populate env file
    env_file.write_env_file({"GETBASED_TOKEN": "x"})
    env_path = env_file.env_file_path()
    assert env_path.exists()

    rc, _, _ = _run(["uninstall", "--delete-env"])
    assert rc == 0
    assert not env_path.exists()


def test_uninstall_keeps_env_by_default(stack_home, fake_shell):
    env_file.write_env_file({"GETBASED_TOKEN": "x"})
    env_path = env_file.env_file_path()

    _run(["uninstall"])
    assert env_path.exists(), "env file must survive uninstall without --delete-env"


# ── set ───────────────────────────────────────────────────────────────


def test_set_upserts(stack_home, fake_shell):
    rc, out, _ = _run(["set", "GETBASED_TOKEN=new_token"])
    assert rc == 0
    assert env_file.read_env_file()["GETBASED_TOKEN"] == "new_token"
    assert "updated" in out


def test_set_rejects_no_equals(stack_home):
    rc, _, err = _run(["set", "noequals"])
    assert rc == 2
    assert "KEY=VALUE" in err


# ── status ────────────────────────────────────────────────────────────


def test_status_reports_missing_env(stack_home, fake_shell):
    rc, out, _ = _run(["status"])
    assert rc == 0
    assert "not present" in out


def test_status_masks_secrets(stack_home, fake_shell):
    env_file.write_env_file(
        {"GETBASED_TOKEN": "supersecrettoken123", "LENS_URL": "http://rag"}
    )
    rc, out, _ = _run(["status"])
    assert rc == 0
    # URL is visible; token is masked
    assert "LENS_URL=http://rag" in out
    assert "supersecrettoken123" not in out
    assert "****" in out


def test_status_shows_path_values_verbatim(stack_home, fake_shell):
    """File-path env vars (LENS_API_KEY_FILE, LENS_DATA_DIR) must not be
    masked — they are paths, not secrets. Earlier the KEY-in-name heuristic
    masked LENS_API_KEY_FILE into `****_key`, which hid debugging info."""
    env_file.write_env_file(
        {
            "LENS_API_KEY_FILE": "/home/alice/.local/share/getbased/lens/api_key",
            "LENS_DATA_DIR": "/data/lens",
            "LENS_URL": "http://rag:8322",
        }
    )
    rc, out, _ = _run(["status"])
    assert rc == 0
    assert "/home/alice/.local/share/getbased/lens/api_key" in out
    assert "/data/lens" in out
    assert "****" not in out


# ── mcp-config ────────────────────────────────────────────────────────


def test_mcp_config_claude_desktop(stack_home):
    rc, out, _ = _run(["mcp-config", "claude-desktop"])
    assert rc == 0
    assert '"mcpServers"' in out
    assert '"GETBASED_STACK_MANAGED": "1"' in out


def test_mcp_config_hermes(stack_home):
    rc, out, _ = _run(["mcp-config", "hermes"])
    assert rc == 0
    assert "mcp_servers:" in out
    assert "enabled_tools:" in out


def test_mcp_config_openclaw(stack_home):
    rc, out, _ = _run(["mcp-config", "openclaw"])
    assert rc == 0
    assert '"mcp"' in out
    assert '"servers"' in out
    assert '"GETBASED_STACK_MANAGED": "1"' in out


def test_mcp_config_codex(stack_home):
    rc, out, _ = _run(["mcp-config", "codex"])
    assert rc == 0
    assert "[mcp_servers.getbased]" in out
    assert "[mcp_servers.getbased.env]" in out
    assert 'GETBASED_STACK_MANAGED = "1"' in out


def test_mcp_config_unknown_client():
    rc, _, _ = _run(["mcp-config", "vim"])
    # argparse rejects before our code runs — exit code 2, SystemExit
    # caught by main's fallback → exit 2
    assert rc != 0


# ── connect ───────────────────────────────────────────────────────────


def _setup_blob(token="tok_123", context_key="ctx_456", gateway="https://sync.getbased.health"):
    import base64
    import json

    payload = json.dumps({
        "version": 1,
        "token": token,
        "contextKey": context_key,
        "gateway": gateway,
    }, separators=(",", ":")).encode()
    return "gbsetup_v1_" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def test_connect_hermes_setup_blob_stores_secrets_and_registers_mcp(stack_home, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_ASSUME_HERMES_CHILD", "0")
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: {
        "getbased-mcp": "/usr/local/bin/getbased-mcp",
        "hermes": "/usr/local/bin/hermes",
    }.get(name))

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc, out, err = _run(["connect", "hermes", "--setup", _setup_blob()])

    assert rc == 0, err
    data = env_file.read_env_file()
    assert data["GETBASED_STACK_MANAGED"] == "1"
    assert data["GETBASED_TOKEN"] == "tok_123"
    assert data["GETBASED_AGENT_CONTEXT_KEY"] == "ctx_456"
    assert data["GETBASED_GATEWAY"] == "https://sync.getbased.health"
    assert ([
        "/usr/local/bin/hermes", "mcp", "add", "getbased",
        "--command", "/usr/local/bin/getbased-mcp",
        "--env", "GETBASED_STACK_MANAGED=1",
    ], "y\ny\n") in calls
    assert (["/usr/local/bin/hermes", "mcp", "test", "getbased"], None) in calls
    assert (["/usr/local/bin/hermes", "gateway", "restart"], None) in calls
    assert "Hermes MCP server registered" in out
    assert "Connection test passed" in out
    assert "tok_123" not in out
    assert "ctx_456" not in out


def test_connect_rejects_malformed_setup_blob(stack_home):
    rc, out, err = _run(["connect", "hermes", "--setup", "not-a-setup-code"])
    assert rc == 2
    assert "Invalid setup" in err
    assert env_file.read_env_file() == {}


def test_connect_non_hermes_clients_store_credentials_and_point_to_config(stack_home, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/getbased-mcp" if name == "getbased-mcp" else None)
    rc, out, err = _run(["connect", "openclaw", "--setup", _setup_blob()])
    assert rc == 0, err
    data = env_file.read_env_file()
    assert data["GETBASED_STACK_MANAGED"] == "1"
    assert data["GETBASED_TOKEN"] == "tok_123"
    assert data["GETBASED_AGENT_CONTEXT_KEY"] == "ctx_456"
    assert "getbased-stack mcp-config openclaw" in out
    assert "tok_123" not in out
    assert "ctx_456" not in out


def test_connect_codex_is_an_allowed_setup_target(stack_home, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/getbased-mcp" if name == "getbased-mcp" else None)
    rc, out, err = _run(["connect", "codex", "--setup", _setup_blob()])
    assert rc == 0, err
    assert "getbased-stack mcp-config codex" in out


def test_connect_skips_gateway_restart_when_running_inside_hermes(stack_home, monkeypatch):
    monkeypatch.setenv("GETBASED_STACK_ASSUME_HERMES_CHILD", "1")
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: {
        "getbased-mcp": "/usr/local/bin/getbased-mcp",
        "hermes": "/usr/local/bin/hermes",
    }.get(name))

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc, out, err = _run(["connect", "hermes", "--setup", _setup_blob()])

    assert rc == 0, err
    assert (["/usr/local/bin/hermes", "mcp", "test", "getbased"], None) in calls
    assert all(cmd != ["/usr/local/bin/hermes", "gateway", "restart"] for cmd, _ in calls)
    assert "restart skipped because this setup is running inside Hermes" in out


# ── init (non-interactive via EOF on stdin) ───────────────────────────


def test_init_idempotent_with_empty_input(stack_home, fake_shell, monkeypatch):
    """Feed EOF for every prompt: init should complete using defaults
    without crashing. This validates the wizard is non-blocking when
    running over piped input (e.g. scripted install)."""
    # Simulate no token, default yes-to-install
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")

    rc, out, _ = _run(["init"])
    assert rc == 0
    # Env file created
    path = env_file.env_file_path()
    assert path.exists()
    data = env_file.read_env_file(path)
    assert data.get("GETBASED_STACK_MANAGED") == "1"
    assert "LENS_API_KEY_FILE" in data
    # Unit files on disk
    unit_dir = stack_home / "config" / "systemd" / "user"
    assert (unit_dir / "getbased-rag.service").exists()


def test_init_preserves_existing_token(stack_home, fake_shell, monkeypatch):
    """Re-running init must not wipe an explicitly stored token."""
    env_file.write_env_file(
        {"GETBASED_TOKEN": "preserved_value", "GETBASED_STACK_MANAGED": "1"}
    )
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")

    _run(["init"])
    assert env_file.read_env_file()["GETBASED_TOKEN"] == "preserved_value"


def test_init_does_not_capture_new_token(stack_home, fake_shell, monkeypatch):
    """Agent Access secrets are added explicitly via `getbased-stack set`;
    init no longer prompts for and stores new secret values."""
    inputs = iter([""])  # accept install prompt with default
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(inputs, ""))
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("getpass called")),
    )

    _run(["init"])
    assert "GETBASED_TOKEN" not in env_file.read_env_file()


def test_init_generates_api_key(stack_home, fake_shell, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")

    _run(["init"])
    key_file = Path(env_file.read_env_file()["LENS_API_KEY_FILE"])
    assert key_file.exists()
    key = key_file.read_text().strip()
    assert len(key) >= 32  # secrets.token_urlsafe(32) → ≥43 chars
    # Mode 0600 for secrets
    import stat

    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600


def test_init_reuses_existing_api_key(stack_home, fake_shell, monkeypatch):
    # Pre-create a key
    key_path = stack_home / "data" / "getbased" / "lens" / "api_key"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("preexisting_key\n")
    os.chmod(key_path, 0o600)

    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")

    _run(["init"])
    assert key_path.read_text().strip() == "preexisting_key"


def test_init_yes_flag_skips_all_prompts(stack_home, fake_shell, monkeypatch):
    """`init --yes` must not call input() or getpass() at all. Scripted
    installers (curl | bash) can't service prompts and the EOF fallback
    triggers a Python getpass echo warning that pollutes output.
    Strict assertion: any prompt call fails the test."""
    def _forbid_input(*a, **kw):
        raise AssertionError("input() called under --yes")

    def _forbid_getpass(*a, **kw):
        raise AssertionError("getpass() called under --yes")

    monkeypatch.setattr("builtins.input", _forbid_input)
    monkeypatch.setattr("getpass.getpass", _forbid_getpass)

    rc, out, _ = _run(["init", "--yes"])
    assert rc == 0
    # Banner reflects the mode so the user sees what happened
    assert "non-interactive" in out.lower()
    assert "GETBASED_TOKEN and GETBASED_AGENT_CONTEXT_KEY" in out
    assert "getbased-stack set GETBASED_AGENT_CONTEXT_KEY" in out
    # Env file + units still land
    assert env_file.env_file_path().exists()
    assert (stack_home / "config" / "systemd" / "user" / "getbased-rag.service").exists()


def test_init_yes_installs_units_without_asking(stack_home, fake_shell, monkeypatch):
    """Default for the install-units prompt is Yes, so --yes must also
    install + start. If this regressed to skip, install.sh would
    silently leave services off."""
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")

    _run(["init", "--yes"])
    # UnitManager.install() writes service files under XDG_CONFIG_HOME
    assert (stack_home / "config" / "systemd" / "user" / "getbased-rag.service").exists()
    assert (stack_home / "config" / "systemd" / "user" / "getbased-dashboard.service").exists()


def test_init_yes_survives_missing_systemctl(stack_home, fake_shell, monkeypatch):
    """--yes on a host without systemctl (Docker, macOS, WSL1) must NOT
    crash with an unhandled FileNotFoundError. Unit files still land;
    systemd ops are skipped with a clear message."""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    # Prompts still stubbed defensively — --yes shouldn't call them.
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")

    rc, out, _ = _run(["init", "--yes"])
    assert rc == 0
    # Unit files still written (for re-run on a systemd-enabled host)
    assert (stack_home / "config" / "systemd" / "user" / "getbased-rag.service").exists()
    # Graceful message, not a traceback
    assert "systemctl not available" in out
    assert "Traceback" not in out


def test_init_yes_preserves_existing_token(stack_home, fake_shell, monkeypatch):
    """Non-interactive mode must not nuke a previously-saved token —
    it takes the 'keep current' default, same as pressing Enter."""
    env_file.write_env_file(
        {"GETBASED_TOKEN": "keep_me", "GETBASED_STACK_MANAGED": "1"}
    )
    # No input/getpass expected, but stub defensively in case a future
    # code path adds an unguarded prompt — test still catches it.
    monkeypatch.setattr("builtins.input", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("input under --yes")))
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("getpass under --yes")))

    _run(["init", "-y"])
    assert env_file.read_env_file()["GETBASED_TOKEN"] == "keep_me"


def test_init_is_reentrant(stack_home, fake_shell, monkeypatch):
    """Running init twice in a row must not break anything — second call
    should be a cheap idempotent update, not a destructive rewrite."""
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "")

    _run(["init"])
    key1 = (stack_home / "data" / "getbased" / "lens" / "api_key").read_text()

    _run(["init"])
    key2 = (stack_home / "data" / "getbased" / "lens" / "api_key").read_text()

    assert key1 == key2, "init must not rotate an existing API key"
