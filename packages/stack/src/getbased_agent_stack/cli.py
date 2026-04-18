"""Thin CLI wrapper that proxies to getbased-rag's `lens` and getbased-mcp's
runner. Kept minimal so users can still call `lens` / `getbased-mcp`
directly — this exists only for discoverability ("what commands do I have
after `pipx install getbased-agent-stack`?")."""
from __future__ import annotations

import sys


HELP = """\
getbased-stack — thin wrapper over the two binaries installed by this package.

Real commands (use directly, they're also on your PATH):
  lens                      — getbased-rag CLI (serve, ingest, stats, ...)
  getbased-mcp              — getbased-mcp stdio server (spawned by agents)

This wrapper only exists for discoverability:
  getbased-stack serve      → lens serve
  getbased-stack info       → lens info
  getbased-stack version    → print the installed package versions

Quick start:
  1. `getbased-stack serve &`           start the RAG server
  2. `lens ingest /path/to/papers`      index your docs
  3. configure your MCP agent (Claude Code, Hermes) — see the README
"""


def main() -> int:
    import getbased_agent_stack

    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd == "version":
        try:
            import getbased_mcp  # noqa: F401
            import lens  # noqa: F401
            import importlib.metadata as md
            print(f"getbased-agent-stack {getbased_agent_stack.__version__}")
            try:
                print(f"  getbased-mcp {md.version('getbased-mcp')}")
            except md.PackageNotFoundError:
                print("  getbased-mcp (not installed)")
            try:
                print(f"  getbased-rag {md.version('getbased-rag')}")
            except md.PackageNotFoundError:
                print("  getbased-rag (not installed)")
            return 0
        except ImportError as e:
            print(f"Missing dependency: {e}", file=sys.stderr)
            return 1

    # Delegate to the lens CLI for everything else.
    try:
        from lens.cli import app as lens_app

        sys.argv = ["lens"] + argv
        lens_app()
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except ImportError:
        print("getbased-rag not installed — install with `pipx install getbased-agent-stack[full]`", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
