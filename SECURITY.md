# Security policy

## Threat model

getbased-rag is designed to run as a **trusted local service** behind a single API key. The default bind (`127.0.0.1`) and the exclusive local-mode Qdrant lock both reinforce that: the server is one-user, one-process.

If you expose the server to a LAN or the public internet, you're responsible for:

- Front-ending it with a reverse proxy that terminates TLS
- Rate-limiting (the server has none — an attacker can hammer `/query` to DoS the embedder)
- Using `LENS_HOST=0.0.0.0` only deliberately, and never on a network with untrusted clients
- Treating the API key file (default `$XDG_DATA_HOME/getbased/lens/api_key`) as a secret at rest (it's written `0600` but is still filesystem-readable by root / container hosts)

## What the server protects

| Asset | Mechanism |
|---|---|
| Ingested document content | Bearer auth on every data endpoint (`/query`, `/stats`, `/sources`, `/libraries`) |
| API key at rest | File mode `0600`, regenerated via `lens key` when deleted |
| Against timing attacks on the key | `secrets.compare_digest` on the bearer comparison |
| Against library-ID path traversal | `_collection_for` sanitises to `[a-zA-Z0-9_-]`, membership check against registry before any op |
| Against oversized responses | Bounded `top_k` (clamped `[1, max_chunks]`), bounded chunk/source string lengths |

## What the server does NOT protect against

- **Local OS users with filesystem access.** Anyone who can read `$XDG_DATA_HOME/getbased/lens/api_key` owns the server. Containerise or use an encrypted filesystem if you share a box.
- **Untrusted ingested documents.** The server embeds whatever text it's given — if your corpus contains adversarial prompt-injection payloads, they'll end up in `chunks[n].text` and downstream clients (PWA, MCP, Hermes) will dutifully pass them to an LLM. Sanitising ingested content is the client's responsibility.
- **Malicious Qdrant client libraries.** Everything depends on `qdrant-client` and the HF model stack. Dep audits flag known CVEs; see below.
- **Denial of service.** No rate limiting. A malicious local caller can drive CPU/GPU usage to 100% with pathological queries.

## Known dependency vulnerabilities

Run `uv run --with pip-audit pip-audit` from the repo root.

- **`transformers` CVE-2026-1839** — affects versions `<5.0.0rc3`. Fix-available is a release candidate; we're tracking for the first stable release. In the meantime, ingest only documents you trust (the vuln is in tokenizer parsing paths triggered during model load, not query serving).

## Reporting vulnerabilities

Email the maintainer at `claude.l8hw3@simplelogin.com` with subject `[getbased-rag] security`. Please include reproduction steps and your PGP key if you want encrypted replies.

Do NOT open a public GitHub issue for a live vulnerability.

## Auth architecture diagram

```
client (PWA or MCP)
  │
  │ HTTPS (if behind reverse proxy) / HTTP (localhost)
  │ Authorization: Bearer <key>
  ▼
FastAPI ── require_auth() ── constant-time compare vs on-disk key
  │
  │ if OK →
  ▼
active_store() ── Store(collection=registry.active_collection)
  │
  │ path-sanitised library_id
  ▼
Qdrant local (exclusive file lock)
```

Every non-public endpoint routes through `require_auth()` before touching any state. `/` and `/health` are explicitly public — they return no sensitive data.
