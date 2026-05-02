"""MCP tab API — config generation, stdio tester, env viewer.

Unlike the knowledge proxy, this talks directly to the `getbased-mcp`
Python module (imported in-process for env introspection) and to the
`getbased-mcp` CLI (spawned as stdio subprocess for the test button).
The dashboard doesn't launch MCP as a service — it's always a stdio
child of whichever AI client consumes it. We just poke the subprocess
to verify the install works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import sys
import time
from typing import Literal

log = logging.getLogger(__name__)

from fastapi import APIRouter, FastAPI, HTTPException, Request

from .. import __version__ as _PKG_VERSION
from ..config import DashboardConfig
from ..server import _require_auth


def _cfg(request: Request) -> DashboardConfig:
    return request.app.state.config


def _resolved_mcp_env(cfg: DashboardConfig) -> dict:
    """Report the values an MCP subprocess would see when spawned by the
    dashboard. The MCP reads `LENS_URL` and `LENS_API_KEY_FILE` from env
    at import time — we spawn it with our values, so the dashboard's
    view IS the MCP's view. No secrets are returned; token presence is
    reported as a boolean so the UI can show "configured / not set".

    Previously this reloaded the `getbased_mcp` module with temporarily
    mutated `os.environ` to observe its resolution. That raced on
    `os.environ` across concurrent requests (two tabs corrupt each
    other's config) and leaked the reload to any other in-process user
    of the module. Now pure read — no global mutation, no reload."""
    # Import for the module path, then peek at its publicly-interesting
    # symbols to confirm we're in sync with what it would resolve given
    # the same env. We do NOT rely on the currently-cached module-level
    # values (they were resolved at whatever env the process started in).
    import getbased_mcp as _mcp  # noqa: PLC0415

    # Key file: use the dashboard's configured path directly — it's what
    # a spawned MCP inherits. Read via the mcp module's own helper so the
    # "present / absent" check matches exactly what the MCP would see.
    key_file = str(cfg.api_key_file)
    try:
        key_present = bool(cfg.api_key_file.read_text().strip())
    except OSError:
        key_present = False

    return {
        "lens_url": cfg.lens_url,
        "lens_api_key_file": key_file,
        "lens_api_key_present": key_present,
        # GATEWAY / TOKEN are read from the dashboard process's env since
        # spawned subprocesses inherit the dashboard's env by default.
        "getbased_gateway": os.environ.get(
            "GETBASED_GATEWAY", "https://sync.getbased.health"
        ),
        "getbased_token_present": bool(os.environ.get("GETBASED_TOKEN")),
        "mcp_module_path": _mcp.__file__,
    }


def _mcp_command_path() -> str:
    """Locate the `getbased-mcp` CLI. Preference order:
      1. Same venv as the running dashboard — `getbased-mcp` sits next
         to the Python that launched us. Handles `uv run`, activated
         venvs, and `pipx install` all at once.
      2. PATH lookup via `shutil.which`.
      3. Bare name — config generator still works; `run test` will 404.
    We want (1) first because dashboards run via `.venv/bin/getbased-
    dashboard` don't have their venv on PATH, so `shutil.which` misses
    the right binary even though it's sitting right next door."""
    venv_bin = os.path.dirname(sys.executable)
    candidate = os.path.join(venv_bin, "getbased-mcp")
    if os.path.exists(candidate) and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which("getbased-mcp")
    return found or "getbased-mcp"


def _claude_desktop_path() -> str:
    """OS-appropriate path to claude_desktop_config.json. Users otherwise
    have to Google where it lives on their platform; showing the absolute
    path inline removes that detour."""
    sys_name = platform.system().lower()
    if sys_name == "darwin":
        return "~/Library/Application Support/Claude/claude_desktop_config.json"
    if sys_name == "windows":
        return "%APPDATA%\\Claude\\claude_desktop_config.json"
    return "~/.config/Claude/claude_desktop_config.json"


def _config_for_client(
    client: str,
    cfg: DashboardConfig,
    env_info: dict,
) -> tuple[str, str]:
    """Return (filename, content) pair for the requested client's config.
    Filename is a hint for the user ('paste into this file') — the UI
    labels the download accordingly."""
    mcp_cmd = _mcp_command_path()

    # Env block every client gets. Omit GETBASED_TOKEN — we don't have it,
    # and emitting a placeholder string would mislead. The UI explains.
    env_block = {
        "GETBASED_TOKEN": "<paste from getbased → Settings → Data → Messenger Access>",
        "LENS_URL": env_info["lens_url"],
        "LENS_API_KEY_FILE": env_info["lens_api_key_file"],
    }

    enabled_tools = [
        "getbased_lab_context",
        "getbased_section",
        "getbased_list_profiles",
        "getbased_lens_config",
        "knowledge_search",
        "knowledge_list_libraries",
        "knowledge_activate_library",
        "knowledge_stats",
    ]

    if client in ("claude-desktop", "claude-code", "cursor", "cline"):
        # The MCP client config spec is the same across these four — one
        # `mcpServers.<name>` entry with command/args/env.
        body = {
            "mcpServers": {
                "getbased": {
                    "command": mcp_cmd,
                    "args": [],
                    "env": env_block,
                }
            }
        }
        filenames = {
            "claude-desktop": _claude_desktop_path(),
            "claude-code": ".mcp.json (in your project) or ~/.claude.json (user-wide)",
            "cursor": "~/.cursor/mcp.json (or .cursor/mcp.json in your project)",
            "cline": "VS Code Settings JSON → cline.mcpServers",
        }
        return filenames[client], json.dumps(body, indent=2)

    if client == "openclaw":
        # OpenClaw nests servers under `mcp.servers.<name>` (not the
        # `mcpServers` convention Anthropic's clients use). Same
        # command/args/env stdio shape inside. Users can either paste
        # into ~/.openclaw/openclaw.json or install via the CLI:
        #   openclaw mcp set getbased '<json-value-below>'
        body = {
            "mcp": {
                "servers": {
                    "getbased": {
                        "command": mcp_cmd,
                        "args": [],
                        "env": env_block,
                    }
                }
            }
        }
        return "~/.openclaw/openclaw.json", json.dumps(body, indent=2)

    if client == "hermes":
        # Hermes uses YAML and supports per-server tool allowlists via
        # `enabled_tools`. We inline the generation rather than pull
        # PyYAML in — the shape is fixed and tiny.
        lines = [
            "# Paste into ~/.hermes/config.yaml under `mcp_servers:`",
            "mcp_servers:",
            "  getbased:",
            f"    command: {mcp_cmd}",
            "    args: []",
            "    enabled_tools:",
        ]
        lines.extend(f"    - {t}" for t in enabled_tools)
        lines.append("    env:")
        for k, v in env_block.items():
            # YAML: quote values that contain characters with meaning —
            # safer to always double-quote.
            vs = json.dumps(v)  # JSON strings are valid YAML double-quoted
            lines.append(f"      {k}: {vs}")
        return "~/.hermes/config.yaml", "\n".join(lines)

    raise HTTPException(
        status_code=400,
        detail=(
            f"Unknown client '{client}'. Supported: "
            "claude-desktop, claude-code, cursor, cline, hermes, openclaw"
        ),
    )


async def _stdio_probe(cfg: DashboardConfig, timeout_s: float = 10.0) -> dict:
    """Spawn getbased-mcp as stdio, send init + tools/list, return the
    tool names. Every failure mode is captured as a returned dict with
    an `error` key so the UI has one code path. The subprocess is
    guaranteed to be reaped no matter how we exit — any unexpected
    exception triggers kill + wait via the outer finally."""
    mcp_cmd = _mcp_command_path()

    env = os.environ.copy()
    # Propagate the dashboard's view of where lens lives + the key file.
    # Ensures the spawned MCP resolves the same paths the env-viewer UI
    # reports, even if the dashboard process's own env differs.
    env["LENS_URL"] = cfg.lens_url
    env["LENS_API_KEY_FILE"] = str(cfg.api_key_file)

    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            mcp_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": f"MCP CLI '{mcp_cmd}' not found on PATH. Install getbased-mcp.",
        }

    # JSON-RPC request ids we'll match responses against. Using distinct
    # ints so a response can be classified even if MCP emits them in a
    # different order than we sent them (robust to any stream quirks).
    INIT_ID = 1
    LIST_ID = 2

    async def _read_response(expected_id: int) -> dict:
        """Read lines from stdout until we see a JSON-RPC response whose
        id matches the expected one. Notifications, log noise, or any
        out-of-band output is tolerated — we skip past it instead of
        desyncing like a fixed readline-count probe would."""
        while True:
            line = await proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                raise RuntimeError("MCP closed stdout before responding")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON line on stdout (unlikely from FastMCP but
                # harmless if it happens) — ignore and keep reading.
                continue
            if isinstance(msg, dict) and msg.get("id") == expected_id:
                return msg

    async def _do_probe():
        req_init = {
            "jsonrpc": "2.0",
            "id": INIT_ID,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "getbased-dashboard", "version": _PKG_VERSION},
            },
        }
        req_initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        req_list = {"jsonrpc": "2.0", "id": LIST_ID, "method": "tools/list"}

        assert proc.stdin is not None and proc.stdout is not None
        for msg in (req_init, req_initialized, req_list):
            proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

        init_resp = await _read_response(INIT_ID)
        list_resp = await _read_response(LIST_ID)
        return init_resp, list_resp

    async def _cleanup() -> None:
        """Best-effort subprocess reap. Called from the outer finally so
        every exit path (success, exception, cancellation) lands here."""
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    return
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Zombie — the OS will reap it eventually. Don't
                    # block the request thread on a hung child.
                    return

    try:
        try:
            init_resp, list_resp = await asyncio.wait_for(
                _do_probe(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"MCP didn't respond within {timeout_s}s"}
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"MCP returned invalid JSON: {e}"}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            # Log full detail server-side for triage; return only the
            # exception class to the dashboard. Stack-trace strings can leak
            # filesystem layout / env state that doesn't belong in the response.
            log.exception("MCP probe failed")
            return {"ok": False, "error": f"MCP probe failed: {type(e).__name__}"}

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        server_info = (init_resp.get("result") or {}).get("serverInfo") or {}
        tools = [t["name"] for t in (list_resp.get("result") or {}).get("tools", [])]
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "server_info": server_info,
            "tools": tools,
        }
    finally:
        await _cleanup()


def register(app: FastAPI) -> None:
    router = APIRouter(prefix="/api/mcp", tags=["mcp"])

    @router.get("/env")
    async def env_info(request: Request):
        cfg = _cfg(request)
        _require_auth(request, cfg)
        return _resolved_mcp_env(cfg)

    @router.get("/config")
    async def generate_config(
        request: Request,
        client: Literal[
            "claude-desktop", "claude-code", "cursor", "cline", "hermes", "openclaw"
        ] = "claude-desktop",
    ):
        cfg = _cfg(request)
        _require_auth(request, cfg)
        env_info = _resolved_mcp_env(cfg)
        filename, content = _config_for_client(client, cfg, env_info)
        return {"client": client, "filename": filename, "content": content}

    @router.post("/test")
    async def test_mcp(request: Request):
        cfg = _cfg(request)
        _require_auth(request, cfg)
        return await _stdio_probe(cfg)

    app.include_router(router)
