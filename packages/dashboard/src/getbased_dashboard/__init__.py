"""getbased-dashboard — web UI for getbased-agents.

Orchestration layer that sits between the browser and the rag + mcp
packages. Holds no data of its own: proxies knowledge-base operations
to rag, spawns the mcp stdio process on demand for tool discovery and
config generation, reads the mcp's activity log for the dashboard feed.
"""

__version__ = "0.1.0"
