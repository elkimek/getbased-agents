#!/usr/bin/env python3
"""Integration smoke test for the getbased MCP server.

Exercises every tool by calling the underlying async functions directly
(bypassing the MCP stdio transport). Point at a running Lens server and an
active sync gateway session, then run. Prints PASS/SKIP/FAIL per tool.

Prereqs — any subset:
  - GETBASED_TOKEN=... and GETBASED_GATEWAY=...  (default: sync.getbased.health)
    → exercises getbased_lab_context, getbased_section, getbased_list_profiles
  - A running Lens RAG server + LENS_API_KEY_FILE readable
    → exercises knowledge_search, knowledge_list_libraries,
      knowledge_activate_library, knowledge_stats, getbased_lens_config

Tools whose prereqs aren't met are SKIPped with an explanation — partial
runs are fine. Exits 0 iff every attempted tool returned something that
looks like real data (not an error string).

Usage:
  uv run python scripts/smoke.py
  # or
  python scripts/smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Awaitable, Callable

# Make the sibling module importable without packaging.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import getbased_mcp as gm  # noqa: E402


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def _ok(name: str, note: str = "") -> None:
    tail = f" {DIM}— {note}{RESET}" if note else ""
    print(f"  {GREEN}✓{RESET} {name}{tail}")


def _skip(name: str, why: str) -> None:
    print(f"  {YELLOW}⊘{RESET} {name} {DIM}— skipped: {why}{RESET}")


def _fail(name: str, why: str) -> None:
    print(f"  {RED}✗{RESET} {name} {DIM}— {why}{RESET}")


def _looks_like_error(result: str) -> bool:
    return result.startswith("Error:") or result.lower().startswith(("knowledge search error", "knowledge libraries error", "knowledge stats error", "activate library error"))


async def _run(name: str, fn: Callable[[], Awaitable[str]], prereq_ok: bool, prereq_why: str, counters: dict) -> None:
    if not prereq_ok:
        _skip(name, prereq_why)
        counters["skip"] += 1
        return
    try:
        result = await fn()
    except Exception as e:  # noqa: BLE001
        _fail(name, f"raised {type(e).__name__}: {e}")
        counters["fail"] += 1
        return
    if not isinstance(result, str):
        _fail(name, f"expected str, got {type(result).__name__}")
        counters["fail"] += 1
        return
    if _looks_like_error(result):
        _fail(name, result.split("\n", 1)[0])
        counters["fail"] += 1
        return
    # Truncate preview for the log.
    preview = result.splitlines()[0][:80] if result else "(empty)"
    _ok(name, preview)
    counters["pass"] += 1


async def main() -> int:
    counters = {"pass": 0, "skip": 0, "fail": 0}

    gateway_ok = bool(gm.TOKEN)
    gateway_why = "GETBASED_TOKEN not set — skipping lab-context tools"

    lens_key = gm._read_lens_key()
    lens_ok = bool(lens_key)
    lens_why = f"Lens API key not found at {gm.LENS_API_KEY_FILE}"

    print(f"{DIM}Gateway:  {gm.GATEWAY}{RESET}")
    print(f"{DIM}Lens URL: {gm.LENS_URL}{RESET}")
    print()

    print("Blood-work tools")
    await _run("getbased_list_profiles", lambda: gm.getbased_list_profiles(), gateway_ok, gateway_why, counters)
    await _run("getbased_lab_context",   lambda: gm.getbased_lab_context(),   gateway_ok, gateway_why, counters)
    await _run("getbased_section (index)", lambda: gm.getbased_section(),     gateway_ok, gateway_why, counters)

    print()
    print("Knowledge-base tools")
    await _run("getbased_lens_config",       lambda: gm.getbased_lens_config(),    lens_ok, lens_why, counters)
    await _run("knowledge_list_libraries",   lambda: gm.knowledge_list_libraries(), lens_ok, lens_why, counters)
    await _run("knowledge_stats",            lambda: gm.knowledge_stats(),         lens_ok, lens_why, counters)
    await _run("knowledge_search (smoke)",   lambda: gm.knowledge_search(query="health", n_results=1), lens_ok, lens_why, counters)

    # knowledge_activate_library requires knowing a valid library ID. Pull
    # one from the list-libraries result instead of hardcoding — this tests
    # the round-trip (list → pick id → activate → verify).
    if lens_ok:
        libs_data = await gm._lens_call("GET", "/libraries")
        libs = (libs_data or {}).get("libraries") or []
        if libs:
            target = libs[0].get("id", "")
            if target:
                await _run(
                    f"knowledge_activate_library({target})",
                    lambda: gm.knowledge_activate_library(library_id=target),
                    True, "", counters,
                )
            else:
                _skip("knowledge_activate_library", "library list returned no id")
                counters["skip"] += 1
        else:
            _skip("knowledge_activate_library", "no libraries to activate")
            counters["skip"] += 1
    else:
        _skip("knowledge_activate_library", lens_why)
        counters["skip"] += 1

    print()
    summary = f"{counters['pass']} passed, {counters['skip']} skipped, {counters['fail']} failed"
    colour = GREEN if counters["fail"] == 0 else RED
    print(f"{colour}{summary}{RESET}")
    return 0 if counters["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
