# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in any of the `getbased-agents` packages (`getbased-mcp`, `getbased-rag`, `getbased-dashboard`, `getbased-agent-stack`), please report it privately via [GitHub Security Advisories](https://github.com/elkimek/getbased-agents/security/advisories/new).

Do **not** open a public issue for security vulnerabilities.

I'll acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Scope

- MCP server (tool dispatch, knowledge_search, profile listing)
- RAG server (HTTP API, embedding worker, library CRUD, query handlers)
- Dashboard server (HTTP UI, login flow, MCP-spawn supervisor, activity log)
- Stack CLI (init flow, systemd unit generation, install/upgrade paths)
- Inter-process trust boundaries (bearer-token auth between dashboard ↔ rag and dashboard ↔ mcp)

## Out of Scope

- Issues in `@evolu/*`, `transformers`, or other vendored libraries (report upstream)
- Self-hosted relay infrastructure (report to the relay operator)
- Operating-system-level vulnerabilities

## Architecture

The stack runs entirely on the user's hardware. There is no shared backend. Each package binds to localhost or a user-specified bind address; sensitive endpoints require a bearer token. All persistent state lives under `$XDG_DATA_HOME/getbased/` (or platform equivalent).
