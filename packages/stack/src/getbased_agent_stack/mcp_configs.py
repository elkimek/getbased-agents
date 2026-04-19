"""MCP client config snippets.

Each client has its own config shape (JSON vs YAML, where to place the
mcpServers key). All generated snippets point at the absolute path to
`getbased-mcp` resolved via shutil.which, and carry only one env var:
GETBASED_STACK_MANAGED=1. Everything else lives in the shared env file,
read by the MCP process at startup.

The stock Hermes snippet is the one exception — it supports richer
configuration natively (enabled_tools, tool aliases), so we emit those.
"""
from __future__ import annotations

import json
import shutil
from typing import Callable


SUPPORTED_CLIENTS = ("claude-desktop", "claude-code", "cursor", "cline", "hermes")

# Single-token list for the Hermes snippet. Matches the tool set the MCP
# server exposes as of getbased-mcp 0.2.2. Keep alphabetical for diff-friendliness.
HERMES_ENABLED_TOOLS = [
    "getbased_lens_config",
    "getbased_list_profiles",
    "getbased_read_profile",
    "knowledge_activate_library",
    "knowledge_list_libraries",
    "knowledge_search",
    "knowledge_stats",
]


def _resolve_mcp_binary(resolver: Callable[[str], "str | None"] = shutil.which) -> str:
    """Absolute path to the `getbased-mcp` binary, or a bare-name fallback.
    GUI-launched MCP clients (Claude Desktop, Cursor) don't inherit the
    shell PATH, so a bare binary name often fails to resolve. We emit the
    absolute path when we can; otherwise bare name — the caller tells the
    user which case they got via `_resolver_warning`.
    """
    resolved = resolver("getbased-mcp")
    return resolved or "getbased-mcp"


def _resolver_warning(command: str) -> "str | None":
    """Return a warning string if the command isn't an absolute path,
    else None. Used by emitters to tell the user when the snippet needs
    hand-editing to work from a GUI-launched client."""
    if command.startswith("/") or (len(command) > 1 and command[1] == ":"):
        return None
    return (
        "WARNING: `getbased-mcp` wasn't found on PATH when this snippet was\n"
        "generated. GUI-launched MCP clients don't inherit your shell PATH;\n"
        "replace the `command` below with an absolute path (run\n"
        "`which getbased-mcp` in a shell where it works, paste that result)."
    )


def _json_block(command: str) -> "dict":
    return {
        "mcpServers": {
            "getbased": {
                "command": command,
                "env": {"GETBASED_STACK_MANAGED": "1"},
            }
        }
    }


def _prepend_warning(banner: str, warning: "str | None") -> str:
    if not warning:
        return banner
    return "// " + warning.replace("\n", "\n// ") + "\n" + banner


def emit_claude_desktop(resolver: Callable[[str], "str | None"] = shutil.which) -> str:
    cmd = _resolve_mcp_binary(resolver)
    payload = _json_block(cmd)
    banner = (
        "// Paste into ~/Library/Application Support/Claude/claude_desktop_config.json\n"
        "// (macOS) or %APPDATA%\\Claude\\claude_desktop_config.json (Windows).\n"
        "// Merge with existing mcpServers if present.\n"
    )
    banner = _prepend_warning(banner, _resolver_warning(cmd))
    return banner + json.dumps(payload, indent=2) + "\n"


def emit_claude_code(resolver: Callable[[str], "str | None"] = shutil.which) -> str:
    cmd = _resolve_mcp_binary(resolver)
    payload = _json_block(cmd)
    banner = (
        "// Paste into ~/.claude/settings.json (user scope) or <project>/.mcp.json\n"
        "// (project scope). Merge with existing mcpServers if present.\n"
    )
    banner = _prepend_warning(banner, _resolver_warning(cmd))
    return banner + json.dumps(payload, indent=2) + "\n"


def emit_cursor(resolver: Callable[[str], "str | None"] = shutil.which) -> str:
    cmd = _resolve_mcp_binary(resolver)
    payload = _json_block(cmd)
    banner = "// Paste into ~/.cursor/mcp.json (merge with existing mcpServers).\n"
    banner = _prepend_warning(banner, _resolver_warning(cmd))
    return banner + json.dumps(payload, indent=2) + "\n"


def emit_cline(resolver: Callable[[str], "str | None"] = shutil.which) -> str:
    cmd = _resolve_mcp_binary(resolver)
    payload = _json_block(cmd)
    banner = (
        "// Cline MCP settings. Paste into the Cline extension settings panel\n"
        "// under 'MCP Servers' (Cursor/VSCode).\n"
    )
    banner = _prepend_warning(banner, _resolver_warning(cmd))
    return banner + json.dumps(payload, indent=2) + "\n"


def emit_hermes(resolver: Callable[[str], "str | None"] = shutil.which) -> str:
    """Hermes uses YAML — we emit by hand (no yaml dep) since the shape
    is trivial. Includes enabled_tools for parity with the existing
    examples/hermes-mcp.yaml snippet.

    The emitted snippet carries GETBASED_STACK_MANAGED=1 so MCP reads the
    stack's shared env file. If you'd rather keep Hermes's config.yaml as
    the single source of env (e.g. an existing Hermes VM that already has
    GETBASED_TOKEN + LENS_API_KEY_FILE set explicitly), drop the env block
    entirely. Both modes work — setdefault semantics mean explicit env
    always wins over the shared file — but committing to one keeps
    future debugging simpler.
    """
    cmd = _resolve_mcp_binary(resolver)
    warning = _resolver_warning(cmd)
    lines = [
        "# Hermes Agent MCP configuration snippet for ~/.hermes/config.yaml",
        "# See https://github.com/hermes-agent/hermes-agent for the full config schema.",
        "# The getbased stack's shared env file carries GETBASED_TOKEN + rag URL +",
        "# api key path; only the opt-in flag belongs in Hermes's config.",
        "# (If your Hermes config already sets GETBASED_TOKEN / LENS_* explicitly,",
        "# drop the env block entirely — the Python loader honors existing env.)",
    ]
    if warning:
        lines.append("#")
        for wline in warning.splitlines():
            lines.append(f"# {wline}")
    lines.extend(
        [
            "",
            "mcp_servers:",
            "  getbased:",
            f"    command: {cmd}",
            "    env:",
            '      GETBASED_STACK_MANAGED: "1"',
            "    enabled_tools:",
        ]
    )
    for tool in HERMES_ENABLED_TOOLS:
        lines.append(f"      - {tool}")
    return "\n".join(lines) + "\n"


def emit(client: str, resolver: Callable[[str], "str | None"] = shutil.which) -> str:
    """Dispatch to the right emitter. Raises ValueError on unknown client."""
    match client:
        case "claude-desktop":
            return emit_claude_desktop(resolver)
        case "claude-code":
            return emit_claude_code(resolver)
        case "cursor":
            return emit_cursor(resolver)
        case "cline":
            return emit_cline(resolver)
        case "hermes":
            return emit_hermes(resolver)
        case _:
            raise ValueError(
                f"unknown client {client!r}. Supported: {', '.join(SUPPORTED_CLIENTS)}"
            )
