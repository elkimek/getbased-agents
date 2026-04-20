"""Tests for the systemd UnitManager + linger helpers.

Every systemctl/loginctl call is routed through an injectable `shell`
callable so tests can replay scripted command outputs. No real systemd
is contacted — these tests run on a dev laptop, CI, Windows, anywhere.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from getbased_agent_stack import units
from getbased_agent_stack.units import CommandResult, SERVICE_NAMES, UnitManager


class FakeShell:
    """Records every command + returns scripted responses. Tests match by
    command prefix for flexibility."""

    def __init__(self, responses: "dict[tuple, CommandResult]" = None):
        self.calls: "list[list[str]]" = []
        self.responses = responses or {}

    def __call__(self, cmd: "list[str]") -> CommandResult:
        self.calls.append(cmd)
        # Match longest-prefix first — allows test to stub specific verbs
        for prefix_len in range(len(cmd), 0, -1):
            key = tuple(cmd[:prefix_len])
            if key in self.responses:
                return self.responses[key]
        return CommandResult(0, "", "")


# ── install_files / remove_files ──────────────────────────────────────


def test_install_files_copies_both_units(tmp_path):
    mgr = UnitManager(unit_dir=tmp_path, shell=FakeShell())
    written = mgr.install_files()
    assert len(written) == 2
    for name in SERVICE_NAMES:
        assert (tmp_path / name).exists()
        # Spot-check the files have actual unit content, not placeholder
        text = (tmp_path / name).read_text()
        assert "[Unit]" in text
        assert "[Service]" in text


def test_install_files_is_idempotent(tmp_path):
    mgr = UnitManager(unit_dir=tmp_path, shell=FakeShell())
    mgr.install_files()
    # Re-run overwrites cleanly
    mgr.install_files()
    assert (tmp_path / SERVICE_NAMES[0]).exists()


def test_remove_files_noop_on_missing(tmp_path):
    mgr = UnitManager(unit_dir=tmp_path, shell=FakeShell())
    removed = mgr.remove_files()
    assert removed == []


def test_remove_files_deletes(tmp_path):
    mgr = UnitManager(unit_dir=tmp_path, shell=FakeShell())
    mgr.install_files()
    removed = mgr.remove_files()
    assert len(removed) == 2
    for name in SERVICE_NAMES:
        assert not (tmp_path / name).exists()


# ── daemon_reload / enable / disable ──────────────────────────────────


def test_enable_invokes_systemctl_with_now(tmp_path):
    shell = FakeShell()
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    mgr.enable(now=True)
    assert shell.calls == [
        ["systemctl", "--user", "enable", "--now", *SERVICE_NAMES]
    ]


def test_enable_without_now(tmp_path):
    shell = FakeShell()
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    mgr.enable(now=False)
    assert shell.calls[0] == ["systemctl", "--user", "enable", *SERVICE_NAMES]


def test_disable_invokes_systemctl(tmp_path):
    shell = FakeShell()
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    mgr.disable(now=True)
    assert shell.calls[0] == [
        "systemctl", "--user", "disable", "--now", *SERVICE_NAMES
    ]


def test_is_active_parses_systemctl_output(tmp_path):
    shell = FakeShell(
        {
            ("systemctl", "--user", "is-active", "foo.service"): CommandResult(
                0, "active\n", ""
            ),
            ("systemctl", "--user", "is-active", "bar.service"): CommandResult(
                3, "inactive\n", ""
            ),
        }
    )
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    assert mgr.is_active("foo.service") is True
    assert mgr.is_active("bar.service") is False


def test_is_enabled_accepts_alias_and_static(tmp_path):
    shell = FakeShell(
        {
            ("systemctl", "--user", "is-enabled", "a"): CommandResult(0, "enabled\n", ""),
            ("systemctl", "--user", "is-enabled", "b"): CommandResult(0, "alias\n", ""),
            ("systemctl", "--user", "is-enabled", "c"): CommandResult(0, "static\n", ""),
            ("systemctl", "--user", "is-enabled", "d"): CommandResult(1, "disabled\n", ""),
        }
    )
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    assert mgr.is_enabled("a")
    assert mgr.is_enabled("b")
    assert mgr.is_enabled("c")
    assert not mgr.is_enabled("d")


# ── high-level install / uninstall ────────────────────────────────────


def test_install_full_sequence(tmp_path):
    shell = FakeShell()
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    log = mgr.install(enable=True, start=True)

    # Files on disk
    for name in SERVICE_NAMES:
        assert (tmp_path / name).exists()
    # systemctl called in the expected order
    assert shell.calls[0] == ["systemctl", "--user", "daemon-reload"]
    assert shell.calls[1] == ["systemctl", "--user", "enable", "--now", *SERVICE_NAMES]
    # Log mentions what happened
    joined = "\n".join(log)
    assert "wrote" in joined
    assert "enabled" in joined
    assert "started" in joined


def test_install_daemon_reload_failure_short_circuits(tmp_path):
    shell = FakeShell(
        {("systemctl", "--user", "daemon-reload"): CommandResult(1, "", "bad config")}
    )
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    log = mgr.install()
    # enable is never called after a daemon-reload failure
    enable_calls = [c for c in shell.calls if c[:3] == ["systemctl", "--user", "enable"]]
    assert enable_calls == []
    assert any("daemon-reload FAILED" in line for line in log)


def test_real_shell_handles_missing_binary(monkeypatch):
    """_real_shell must not raise FileNotFoundError when systemctl is
    absent (Docker, macOS, WSL1). Before 0.5.1 this crashed `init` with
    an unhandled traceback — check it returns a shell-like 127 instead."""
    import subprocess as sp

    def boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory", "systemctl")

    monkeypatch.setattr(sp, "run", boom)
    r = units._real_shell(["systemctl", "--user", "daemon-reload"])
    assert r.returncode == 127
    assert "command not found" in r.stderr
    assert "systemctl" in r.stderr


def test_install_skips_when_systemctl_absent(tmp_path, monkeypatch):
    """On a host without systemctl, `install()` must write unit files
    (harmless, enables later re-run) but skip daemon-reload/enable with
    a clear message — not stack FAILED errors on top of each other."""
    monkeypatch.setattr(units.shutil, "which", lambda name: None)
    shell = FakeShell()
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    log = mgr.install()

    # Files written (prereq for future systemd-enabled reinstall)
    for name in SERVICE_NAMES:
        assert (tmp_path / name).exists()
    # No systemctl calls attempted
    assert not any(c[:1] == ["systemctl"] for c in shell.calls)
    # User-visible message present
    assert any("systemctl not available" in line for line in log)


def test_install_enable_failure_reported(tmp_path):
    shell = FakeShell(
        {
            ("systemctl", "--user", "enable", "--now", *SERVICE_NAMES): CommandResult(
                1, "", "link failed"
            )
        }
    )
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    log = mgr.install()
    assert any("enable FAILED" in line for line in log)


def test_uninstall_sequence(tmp_path):
    shell = FakeShell()
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    mgr.install_files()
    log = mgr.uninstall()
    # Files gone
    for name in SERVICE_NAMES:
        assert not (tmp_path / name).exists()
    # Disable attempted, files removed, daemon-reload called once after
    assert shell.calls[0][:3] == ["systemctl", "--user", "disable"]
    assert ["systemctl", "--user", "daemon-reload"] in shell.calls
    assert any("removed" in line for line in log)


def test_uninstall_noop_when_nothing_installed(tmp_path):
    shell = FakeShell()
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    log = mgr.uninstall()
    # Disable is still attempted (safe) but no daemon-reload because no files removed
    reloads = [c for c in shell.calls if c == ["systemctl", "--user", "daemon-reload"]]
    assert reloads == []


def test_status(tmp_path):
    shell = FakeShell(
        {
            ("systemctl", "--user", "is-active", "getbased-rag.service"): CommandResult(
                0, "active\n", ""
            ),
            ("systemctl", "--user", "is-enabled", "getbased-rag.service"): CommandResult(
                0, "enabled\n", ""
            ),
            (
                "systemctl",
                "--user",
                "is-active",
                "getbased-dashboard.service",
            ): CommandResult(3, "inactive\n", ""),
            (
                "systemctl",
                "--user",
                "is-enabled",
                "getbased-dashboard.service",
            ): CommandResult(1, "disabled\n", ""),
        }
    )
    mgr = UnitManager(unit_dir=tmp_path, shell=shell)
    (tmp_path / "getbased-rag.service").write_text("dummy")

    out = mgr.status()
    rag = next(s for s in out if s["name"] == "getbased-rag.service")
    dash = next(s for s in out if s["name"] == "getbased-dashboard.service")
    assert rag == {
        "name": "getbased-rag.service",
        "installed": True,
        "enabled": True,
        "active": True,
    }
    assert dash == {
        "name": "getbased-dashboard.service",
        "installed": False,
        "enabled": False,
        "active": False,
    }


# ── linger + session helpers ──────────────────────────────────────────


def test_has_linger_yes(monkeypatch):
    shell = FakeShell(
        {
            ("loginctl", "show-user", "alice", "--property=Linger"): CommandResult(
                0, "Linger=yes\n", ""
            )
        }
    )
    monkeypatch.setenv("USER", "alice")
    assert units.has_linger(shell=shell) is True


def test_has_linger_no(monkeypatch):
    shell = FakeShell(
        {
            ("loginctl", "show-user", "alice", "--property=Linger"): CommandResult(
                0, "Linger=no\n", ""
            )
        }
    )
    monkeypatch.setenv("USER", "alice")
    assert units.has_linger(shell=shell) is False


def test_has_linger_command_failure(monkeypatch):
    shell = FakeShell(
        {
            ("loginctl", "show-user", "alice", "--property=Linger"): CommandResult(
                1, "", "error"
            )
        }
    )
    monkeypatch.setenv("USER", "alice")
    assert units.has_linger(shell=shell) is False


def test_is_gui_session_detects_display(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert units.is_gui_session() is True


def test_is_gui_session_headless(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    assert units.is_gui_session() is False
