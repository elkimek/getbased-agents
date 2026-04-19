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
import os
import shutil
import time
from importlib import reload
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request

from ..config import DashboardConfig
from ..server import _require_auth


def _cfg(request: Request) -> DashboardConfig:
    return request.app.state.config


def _resolved_mcp_env(cfg: DashboardConfig) -> dict:
    """Import getbased_mcp fresh (env vars may have changed since last
    import) and return the publicly-interesting module globals. We never
    return secrets — GETBASED_TOKEN is reported as present/absent only."""
    # The MCP reads env at import time. Temporarily prime the env with the
    # dashboard's view so the module resolves its defaults the same way a
    # real spawn from this dashboard config would.
    saved_env: dict[str, str | None] = {}
    overrides = {
        "LENS_API_KEY_FILE": str(cfg.api_key_file),
        "LENS_URL": cfg.lens_url,
    }
    for k, v in overrides.items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        import getbased_mcp as _mcp  # noqa: PLC0415

        reload(_mcp)
        return {
            "lens_url": _mcp.LENS_URL,
            "lens_api_key_file": _mcp.LENS_API_KEY_FILE,
            "lens_api_key_present": bool(_mcp._read_lens_key()),
            "getbased_gateway": _mcp.GATEWAY,
            "getbased_token_present": bool(_mcp.TOKEN),
            "mcp_module_path": _mcp.__file__,
        }
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mcp_command_path() -> str:
    """Locate the `getbased-mcp` CLI. Prefer the install that ships with
    this dashboard (same venv — picked up via `shutil.which`); fall back
    to the bare name relying on PATH. Users can always edit the generated
    config by hand if we guess wrong."""
    found = shutil.which("getbased-mcp")
    return found or "getbased-mcp"


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
            "claude-desktop": "claude_desktop_config.json",
            "claude-code": ".mcp.json (project) or ~/.claude.json (user)",
            "cursor": ".cursor/mcp.json",
            "cline": ".vscode/settings.json → cline.mcpServers",
        }
        return filenames[client], json.dumps(body, indent=2)

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
            "claude-desktop, claude-code, cursor, cline, hermes"
        ),
    )


async def _stdio_probe(cfg: DashboardConfig, timeout_s: float = 10.0) -> dict:
    """Spawn getbased-mcp as stdio, send init + tools/list, return the
    tool names. Every failure mode is captured as a returned dict with
    an `error` key so the UI has one code path."""
    mcp_cmd = _mcp_command_path()

    env = os.environ.copy()
    # Propagate the dashboard's view of where lens lives + the key file.
    # The subprocess will re-resolve these but at least they're consistent
    # with what the user sees in the env viewer.
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

    async def _do_probe():
        req_init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "getbased-dashboard", "version": "0.1.0"},
            },
        }
        req_initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        req_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

        assert proc.stdin is not None and proc.stdout is not None
        for msg in (req_init, req_initialized, req_list):
            proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

        # Two responses (init + list); `initialized` is a notification.
        init_line = await proc.stdout.readline()
        list_line = await proc.stdout.readline()
        init_resp = json.loads(init_line)
        list_resp = json.loads(list_line)
        return init_resp, list_resp

    try:
        init_resp, list_resp = await asyncio.wait_for(_do_probe(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": f"MCP didn't respond within {timeout_s}s"}
    except json.JSONDecodeError as e:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": f"MCP returned invalid JSON: {e}"}
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass

    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    server_info = (init_resp.get("result") or {}).get("serverInfo") or {}
    tools = [t["name"] for t in (list_resp.get("result") or {}).get("tools", [])]
    return {
        "ok": True,
        "elapsed_ms": elapsed_ms,
        "server_info": server_info,
        "tools": tools,
    }


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
            "claude-desktop", "claude-code", "cursor", "cline", "hermes"
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
