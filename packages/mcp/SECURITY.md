# Security policy

## Threat model

getbased-mcp is a **thin HTTP client** spawned on-demand by MCP-capable agents (Claude Code, Hermes, OpenClaw) via stdio. It holds no persistent state; every tool call reads the relevant config from environment variables and makes one or two HTTP requests to backends that enforce their own auth.

The MCP process is only as trusted as the agent that spawns it. If an untrusted agent has permission to execute `getbased-mcp`, they have:

- Read access to `GETBASED_TOKEN` (sync gateway bearer) → can fetch the encrypted Agent Access payload
- Read access to `GETBASED_AGENT_CONTEXT_KEY` (Agent Context key) → can decrypt that payload locally
- Read access to both values → the decrypted lab summary and the ability to submit encrypted, typed action proposals
- Read access to `LENS_API_KEY_FILE` contents → full query/management access to your local RAG library

Run the MCP only under agents you trust. Rotate the bearer token and/or context key in **Settings → Data → Agent Access** if exposure is suspected.

The write boundary remains in the browser. The MCP currently exposes only `sun.session.log`, and only as a short-lived proposal tied to an explicit profile and capability. The relay stores ciphertext; the browser revalidates every field and requires a human **Apply** click before running the app-owned action. Token possession does not provide a direct persistence or arbitrary-patch API.

## What the MCP protects

| Asset | Mechanism |
|---|---|
| Secrets in tool output | `getbased_lens_config` flags its own output as sensitive in the docstring; other tools never echo keys |
| Against response bloat (OOM on pathological server) | `/query` response hard-capped at 32 KB before JSON parsing |
| Against unreachable backends | Every tool has try/except that returns a user-visible error string — nothing raises into the MCP transport |
| Against unauthenticated Lens calls | Read-on-every-call from `LENS_API_KEY_FILE`, no caching |
| Against autonomous health-record writes | Proposal-only MCP tool, exact MCP-visible action schema, encrypted profile/capability/expiry envelope, deterministic browser revalidation at ingest and Apply, explicit user approval, durable proposal-ID idempotency |
| Against plaintext proposal metadata | Proposal ID is derived from the random AES-GCM IV; relay accepts only the owner-registered Agent Context key ID |

## What the MCP does NOT protect against

- **Malicious MCP agents.** If the agent process is compromised, it has your Lens key and gateway token by virtue of spawning this MCP.
- **Malicious responses.** The MCP forwards backend response bodies (error messages, chunk text) into the agent's tool-call output. Adversarial content in an ingested RAG library reaches the LLM via `knowledge_search`.
- **Network interception** between the MCP and its backends. The default `LENS_URL` is `http://localhost:8322` (plaintext, fine on loopback). If you point MCP at a remote Lens, use HTTPS.

## Known dependency vulnerabilities

Run `uv run --with pip-audit pip-audit` from the repo root. At time of writing, clean.

## Reporting vulnerabilities

Email the maintainer at `claude.l8hw3@simplelogin.com` with subject `[getbased-mcp] security`. Do NOT open a public GitHub issue for a live vulnerability.

## Related

- [getbased-rag SECURITY.md](https://github.com/elkimek/getbased-agents/blob/main/packages/rag/SECURITY.md) — the RAG backend's threat model
- [getbased Settings → Data → Agent Access](https://app.getbased.health) — where the Agent Access token and context key are managed
