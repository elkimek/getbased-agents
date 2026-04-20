"""Systemd user-unit install/uninstall/status helpers.

Tests drive this through a `UnitManager` class whose shell-out boundary
(`run_systemctl`, `run_loginctl`) can be monkeypatched. The default
`shell` implementation uses subprocess; tests swap it for a fake that
records calls without touching the real systemd.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Callable, Iterable

# Units this package ships. Order matters for start: rag first (dashboard
# Wants=+After= rag), so systemctl picks the right order naturally anyway.
SERVICE_NAMES = ("getbased-rag.service", "getbased-dashboard.service")


def _default_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def bundled_units() -> "list[tuple[str, str]]":
    """Return the (name, text) pairs for every service this package ships.
    Read via importlib.resources — unit files live inside the package tree
    so it works the same from wheel, sdist, and editable installs."""
    results = []
    for name in SERVICE_NAMES:
        ref = resources.files("getbased_agent_stack") / "systemd" / name
        results.append((name, ref.read_text(encoding="utf-8")))
    return results


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _real_shell(cmd: "list[str]") -> CommandResult:
    # FileNotFoundError when the binary isn't on PATH — happens on systems
    # without systemd (Docker containers, macOS, WSL1). Return a shell-like
    # 127 instead of propagating so callers can handle "not available" the
    # same way they handle "failed" without an unhandled traceback.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {cmd[0]}")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class UnitManager:
    """Orchestrates install/uninstall/status of the bundled service units.

    The shell boundary is injected so tests can assert on calls without
    touching real systemctl. Production code constructs with defaults."""

    def __init__(
        self,
        unit_dir: Path | None = None,
        shell: Callable[["list[str]"], CommandResult] | None = None,
    ) -> None:
        self.unit_dir = unit_dir or _default_unit_dir()
        # Resolve at call time (not class definition) so tests can
        # monkeypatch `units._real_shell` and have new instances pick
        # it up without needing to wire the shell through every call site.
        self._shell = shell if shell is not None else _real_shell

    # ── file operations ────────────────────────────────────────────

    def install_files(self) -> "list[Path]":
        """Copy bundled unit texts into the user unit dir. Overwrites to
        keep install idempotent across upgrades."""
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        written: "list[Path]" = []
        for name, text in bundled_units():
            dest = self.unit_dir / name
            dest.write_text(text, encoding="utf-8")
            written.append(dest)
        return written

    def remove_files(self) -> "list[Path]":
        """Delete unit files from the user unit dir. No-op per file if
        absent — idempotent."""
        removed: "list[Path]" = []
        for name in SERVICE_NAMES:
            p = self.unit_dir / name
            if p.exists():
                p.unlink()
                removed.append(p)
        return removed

    # ── systemctl orchestration ────────────────────────────────────

    def daemon_reload(self) -> CommandResult:
        return self._shell(["systemctl", "--user", "daemon-reload"])

    def enable(self, now: bool = True) -> CommandResult:
        args = ["systemctl", "--user", "enable"]
        if now:
            args.append("--now")
        args.extend(SERVICE_NAMES)
        return self._shell(args)

    def disable(self, now: bool = True) -> CommandResult:
        args = ["systemctl", "--user", "disable"]
        if now:
            args.append("--now")
        args.extend(SERVICE_NAMES)
        return self._shell(args)

    def is_active(self, service: str) -> bool:
        r = self._shell(["systemctl", "--user", "is-active", service])
        return r.returncode == 0 and r.stdout.strip() == "active"

    def is_enabled(self, service: str) -> bool:
        r = self._shell(["systemctl", "--user", "is-enabled", service])
        return r.returncode == 0 and r.stdout.strip() in ("enabled", "alias", "static")

    # ── high-level ops ─────────────────────────────────────────────

    def install(self, enable: bool = True, start: bool = True) -> "list[str]":
        """Run the full install sequence. Returns a log of human-readable
        steps for the caller to print."""
        log: "list[str]" = []
        written = self.install_files()
        for p in written:
            log.append(f"wrote {p}")
        # Unit files are written above regardless — they're a prerequisite
        # for any system that CAN run systemd. But if systemctl is absent
        # (Docker container, macOS, WSL1), skip the daemon-reload/enable
        # phase with a clear message rather than stacking cryptic "command
        # not found" errors on top of each other.
        if shutil.which("systemctl") is None:
            log.append(
                "systemctl not available — unit files written but not activated. "
                "On a systemd-enabled host, re-run `getbased-stack install` to enable + start."
            )
            return log
        r = self.daemon_reload()
        if r.returncode != 0:
            log.append(f"daemon-reload FAILED: {r.stderr.strip()}")
            return log
        if enable:
            r = self.enable(now=start)
            if r.returncode != 0:
                log.append(f"enable FAILED: {r.stderr.strip()}")
            else:
                log.append("enabled " + ", ".join(SERVICE_NAMES))
                if start:
                    log.append("started " + ", ".join(SERVICE_NAMES))
        return log

    def uninstall(self) -> "list[str]":
        log: "list[str]" = []
        r = self.disable(now=True)
        if r.returncode != 0:
            # Already-disabled units return non-zero on `disable --now`.
            # Treat as advisory, keep going.
            log.append(f"disable: {r.stderr.strip() or r.stdout.strip()}")
        removed = self.remove_files()
        for p in removed:
            log.append(f"removed {p}")
        if removed:
            r = self.daemon_reload()
            if r.returncode != 0:
                log.append(f"daemon-reload FAILED: {r.stderr.strip()}")
        return log

    def status(self) -> "list[dict]":
        """Return one dict per service with {name, installed, enabled, active}.
        Cheap: 2 systemctl calls per service."""
        out: "list[dict]" = []
        for name in SERVICE_NAMES:
            out.append(
                {
                    "name": name,
                    "installed": (self.unit_dir / name).exists(),
                    "enabled": self.is_enabled(name),
                    "active": self.is_active(name),
                }
            )
        return out


# ── Linger detection (separate from UnitManager — independent concern) ──


def has_linger(user: str | None = None, shell: Callable = _real_shell) -> bool:
    """True when `loginctl show-user --property=Linger` reports `yes`.

    Linger keeps user services running when the user isn't logged in —
    required for rag/dashboard to come back after a headless reboot.
    """
    user = user or os.environ.get("USER", "")
    if not user:
        return False
    r = shell(["loginctl", "show-user", user, "--property=Linger"])
    return r.returncode == 0 and r.stdout.strip().lower() == "linger=yes"


def is_gui_session() -> bool:
    """Heuristic: does the current session have a display?
    If not, we treat this as a headless host and require linger.
    """
    return bool(
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("XDG_SESSION_TYPE") in ("x11", "wayland")
    )
