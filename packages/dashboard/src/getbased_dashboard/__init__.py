"""getbased-dashboard — web UI for getbased-agents.

Orchestration layer that sits between the browser and the rag + mcp
packages. Holds no data of its own: proxies knowledge-base operations
to rag, spawns the mcp stdio process on demand for tool discovery and
config generation, reads the mcp's activity log for the dashboard feed.
"""

from __future__ import annotations

# Read the version from installed-package metadata so bumping
# pyproject.toml alone is enough — no hunt-and-replace across the code
# base. Falls back to "0+unknown" in editable-from-source runs where
# the package hasn't been installed (e.g. running tests against an
# uninstalled checkout).
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        __version__ = _pkg_version("getbased-dashboard")
    except PackageNotFoundError:
        __version__ = "0+unknown"
except ImportError:  # pragma: no cover — only hit on Python < 3.8
    __version__ = "0+unknown"
