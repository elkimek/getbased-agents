"""Dashboard HTTP API.

Each submodule registers its routes onto a FastAPI app via a `register()`
function. Keeps the server.py entry point readable and lets each concern
(knowledge, mcp, activity) own its own dependencies.
"""
