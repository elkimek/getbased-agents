# Security policy

This meta-package ships **no runtime code of its own** beyond a thin CLI wrapper (`src/getbased_agent_stack/cli.py`). The actual trust surface lives in the two siblings:

- [getbased-rag SECURITY.md](https://github.com/elkimek/getbased-rag/blob/main/SECURITY.md)
- [getbased-mcp SECURITY.md](https://github.com/elkimek/getbased-mcp/blob/main/SECURITY.md)

Report vulnerabilities affecting either sibling to that sibling's issue tracker (or email if the advisory is live).

## What this repo is responsible for

| Area | Mechanism |
|---|---|
| Version pinning | Siblings pinned at `>=x.y.0`. When a sibling ships a breaking change, bump the meta so users get the compatible set |
| systemd hardening | `systemd/getbased-rag.service` ships with `ProtectSystem=strict`, `NoNewPrivileges=true`, `RestrictAddressFamilies`, etc. Run `systemd-analyze security getbased-rag` to audit |
| Example config safety | `examples/*.{json,yaml}` never contains real tokens — only placeholders |
| Supply chain | Both siblings resolved from GitHub main until they land on PyPI. After PyPI publish, pinned by PEP 440 version spec, not by git rev |

## What this repo does NOT protect against

- Vulnerabilities in the siblings themselves (reported + fixed there)
- Compromised GitHub accounts that can push to either sibling repo
- Users running `lens serve` with `LENS_HOST=0.0.0.0` on an untrusted network without TLS — see the rag SECURITY.md

## Reporting vulnerabilities

Scope-affecting (the meta's own code, CI, systemd units, or example configs): email `claude.l8hw3@simplelogin.com` with subject `[getbased-agent-stack] security`.

For sibling vulnerabilities, report to the relevant sibling repo.

Do NOT open a public GitHub issue for a live vulnerability.
