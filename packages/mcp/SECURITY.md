# Security policy

## Threat model

getbased-mcp is a **thin HTTP client** spawned on-demand by MCP-capable agents (Claude Code, Hermes, OpenClaw) via stdio. It holds no persistent state; every tool call reads the relevant config from environment variables and makes one or two HTTP requests to backends that enforce their own auth.

The MCP process is only as trusted as the agent that spawns it. If an untrusted agent has permission to execute `getbased-mcp`, they have:

- Read access to `GETBASED_TOKEN` (sync gateway bearer) → a read-only summary of your lab data
- Read access to `LENS_API_KEY_FILE` contents → full query/management access to your local RAG library

Run the MCP only under agents you trust. Revoke the gateway token in **Settings → Data → Agent Access** if exposure is suspected.

## What the MCP protects

| Asset | Mechanism |
|---|---|
| Secrets in tool output | `getbased_lens_config` flags its own output as sensitive in the docstring; other tools never echo keys |
| Against response bloat (OOM on pathological server) | `/query` response hard-capped at 32 KB before JSON parsing |
| Against unreachable backends | Every tool has try/except that returns a user-visible error string — nothing raises into the MCP transport |
| Against unauthenticated Lens calls | Read-on-every-call from `LENS_API_KEY_FILE`, no caching |

## What the MCP does NOT protect against

- **Malicious MCP agents.** If the agent process is compromised, it has your Lens key and gateway token by virtue of spawning this MCP.
- **Malicious responses.** The MCP forwards backend response bodies (error messages, chunk text) into the agent's tool-call output. Adversarial content in an ingested RAG library reaches the LLM via `knowledge_search`.
- **Network interception** between the MCP and its backends. The default `LENS_URL` is `http://localhost:8322` (plaintext, fine on loopback). If you point MCP at a remote Lens, use HTTPS.

## Known dependency vulnerabilities

Run `uv run --with pip-audit pip-audit` from the repo root. At time of writing, clean.

## Reporting vulnerabilities

Email the maintainer at `claude.l8hw3@simplelogin.com` with subject `[getbased-mcp] security`. Do NOT open a public GitHub issue for a live vulnerability.

## Related

- [getbased-rag SECURITY.md](https://github.com/elkimek/getbased-rag/blob/main/SECURITY.md) — the RAG backend's threat model
- [getbased Settings → Data → Agent Access](https://app.getbased.health) — where the read-only token lives
