"""Tests for MCP client config snippet emitters.

Every snippet must:
  1. Carry an absolute path to `getbased-mcp` (GUI-launched clients don't
     inherit the user's shell PATH)
  2. Emit only GETBASED_STACK_MANAGED=1 in env (no secrets)
  3. Parse as valid JSON / plausibly-YAML for its target client
"""
from __future__ import annotations

import json

import pytest

from getbased_agent_stack import mcp_configs


def _fake_resolver(found: str | None = "/usr/local/bin/getbased-mcp"):
    """Returns a shutil.which-compatible stub that resolves to a fixed path,
    or None to simulate 'binary not on PATH'."""

    def resolve(name: str) -> str | None:
        return found

    return resolve


def test_supported_clients_stable_list():
    """New clients must be added deliberately — don't want to drift."""
    assert mcp_configs.SUPPORTED_CLIENTS == (
        "claude-desktop",
        "claude-code",
        "cursor",
        "cline",
        "hermes",
    )


@pytest.mark.parametrize(
    "client",
    ["claude-desktop", "claude-code", "cursor", "cline"],
)
def test_json_clients_emit_valid_json(client):
    out = mcp_configs.emit(client, resolver=_fake_resolver())
    # Strip leading `//` comment lines so we can json.loads the payload
    json_lines = [line for line in out.splitlines() if not line.startswith("//")]
    payload = json.loads("\n".join(json_lines))
    assert "mcpServers" in payload
    assert "getbased" in payload["mcpServers"]
    server = payload["mcpServers"]["getbased"]
    assert server["command"] == "/usr/local/bin/getbased-mcp"
    assert server["env"] == {"GETBASED_STACK_MANAGED": "1"}


def test_hermes_emits_yaml_shape():
    out = mcp_configs.emit("hermes", resolver=_fake_resolver())
    # Can't parse YAML without pyyaml dep; check structural markers instead.
    assert "mcp_servers:" in out
    assert "  getbased:" in out
    assert "    command: /usr/local/bin/getbased-mcp" in out
    assert 'GETBASED_STACK_MANAGED: "1"' in out
    assert "enabled_tools:" in out
    assert "      - knowledge_search" in out


def test_no_secret_values_in_snippets():
    """A snippet must never carry a concrete token or key value. Prose
    comments may mention these variable names for context, but the emitted
    env block itself must contain only GETBASED_STACK_MANAGED."""
    for client in mcp_configs.SUPPORTED_CLIENTS:
        out = mcp_configs.emit(client, resolver=_fake_resolver())
        # The only env assignment in any snippet must be the managed flag
        for line in out.splitlines():
            stripped = line.strip()
            if "GETBASED_TOKEN" in stripped and "=" in stripped and not stripped.startswith(("#", "//")):
                pytest.fail(f"snippet for {client!r} carries a token assignment: {line}")
            if "LENS_API_KEY" in stripped and "=" in stripped and not stripped.startswith(("#", "//")):
                pytest.fail(f"snippet for {client!r} carries an API key assignment: {line}")


def test_fallback_when_binary_not_on_path():
    """shutil.which returning None should fall back to bare name. Snippet
    stays usable; the user sees a config that at least identifies what
    they need to fix."""
    out = mcp_configs.emit("claude-desktop", resolver=_fake_resolver(found=None))
    json_lines = [line for line in out.splitlines() if not line.startswith("//")]
    payload = json.loads("\n".join(json_lines))
    assert payload["mcpServers"]["getbased"]["command"] == "getbased-mcp"


def test_unknown_client_raises():
    with pytest.raises(ValueError, match="unknown client"):
        mcp_configs.emit("vim", resolver=_fake_resolver())
