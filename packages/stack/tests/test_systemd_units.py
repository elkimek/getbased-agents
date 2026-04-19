"""Regression tests for the bundled systemd unit files.

Found during the Hermes 0.4.0 clean-install smoke test: several hardening
directives (ProtectKernelTunables, RestrictNamespaces, etc.) require
CAP_SYS_ADMIN and fail with 218/CAPABILITIES under `systemctl --user`.
Inline `# comment` on a directive line also breaks parsing (systemd
appends the comment to the value).

These tests lock in both fixes by asserting on unit-file content."""
from __future__ import annotations

import pytest

from getbased_agent_stack import units


# Capability-requiring directives that fail under `systemctl --user`.
# Any of these in a bundled unit would bring back the CAPABILITIES 218
# error on a user install.
FORBIDDEN_USER_MODE_DIRECTIVES = (
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectControlGroups",
    "RestrictNamespaces",
    "RestrictAddressFamilies",
    "RestrictRealtime",
    "MemoryDenyWriteExecute",
    "SystemCallFilter",
    "SystemCallArchitectures",
    "CapabilityBoundingSet",
    "AmbientCapabilities",
)


def _directive_values(text: str) -> "list[tuple[str, str]]":
    """Return (key, value) pairs for every `KEY=VALUE` line that is not a
    comment, preserving order. Section headers excluded."""
    out: "list[tuple[str, str]]" = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")):
            continue
        if "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        out.append((key.strip(), val))
    return out


@pytest.mark.parametrize("name,text", units.bundled_units())
def test_no_user_mode_forbidden_directives(name, text):
    """Capability-requiring directives must not appear in bundled units —
    they're incompatible with `systemctl --user` and prevent first start."""
    for key, _ in _directive_values(text):
        assert key not in FORBIDDEN_USER_MODE_DIRECTIVES, (
            f"{name} has {key}= which fails under systemctl --user "
            f"(needs CAP_SYS_ADMIN, causes 218/CAPABILITIES)"
        )


@pytest.mark.parametrize("name,text", units.bundled_units())
def test_no_inline_comments_on_directives(name, text):
    """systemd does not strip inline `# comments` from directive values.
    A line like `MemoryDenyWriteExecute=false  # needed for ONNX` parses
    the full string (including '# needed for ONNX') as the value, which
    breaks boolean directives and was a real Hermes install bug."""
    for key, val in _directive_values(text):
        # An '#' AFTER meaningful content on a directive line is an inline
        # comment. Leading-# lines were filtered already by _directive_values.
        assert "#" not in val, (
            f"{name} directive {key}= has an inline comment; systemd won't "
            f"strip it. Move the comment to its own line."
        )


@pytest.mark.parametrize("name,text", units.bundled_units())
def test_restart_always_not_on_failure(name, text):
    """Restart=always (not on-failure) so a clean SIGTERM triggers restart.
    `on-failure` was the exact bug that kept lens-rag.service dead on Hermes
    for 5 hours — don't regress that."""
    restart_values = [val for key, val in _directive_values(text) if key == "Restart"]
    assert restart_values, f"{name} has no Restart= directive"
    assert restart_values[-1] == "always", (
        f"{name} has Restart={restart_values[-1]}; must be `always` so clean "
        f"SIGTERM still brings the service back."
    )


@pytest.mark.parametrize("name,text", units.bundled_units())
def test_reads_shared_env_file(name, text):
    """Every unit must source the shared env file via EnvironmentFile=
    so the GETBASED_STACK_MANAGED flag + token + paths are available."""
    values = [val for key, val in _directive_values(text) if key == "EnvironmentFile"]
    assert any(
        "getbased/env" in v for v in values
    ), f"{name} does not source %h/.config/getbased/env via EnvironmentFile="


@pytest.mark.parametrize("name,text", units.bundled_units())
def test_sets_stack_managed_flag(name, text):
    """The opt-in flag must be set via Environment= so the Python loader
    inside the binary picks up the shared file. Without this, services
    run without the shared config and every user has to duplicate env
    into the unit file manually."""
    envs = [val for key, val in _directive_values(text) if key == "Environment"]
    flag_set = any(e.startswith("GETBASED_STACK_MANAGED=1") for e in envs)
    assert flag_set, (
        f"{name} does not set GETBASED_STACK_MANAGED=1 via Environment="
    )
