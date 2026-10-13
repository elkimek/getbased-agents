# Coordinated security release — 2026-10-13

| Package | Version |
|---|---|
| getbased-mcp | 0.3.0 |
| getbased-rag | 0.8.0 |
| getbased-dashboard | 0.7.0 |
| getbased-agent-stack | 0.6.0 |

All four packages now require Python 3.11 or newer. Existing Python 3.10
tool environments must be rebuilt with a supported interpreter; follow the
[migration instructions](https://github.com/elkimek/getbased-agents/blob/main/README.md#migrating-an-existing-python-310-installation).
Configuration and knowledge-library data remain outside the tool environment.
Stop manually launched servers before rebuilding, then relaunch them and
reconnect MCP clients. For a systemd-managed stack, run `getbased-stack install`
after rebuilding to restart its services.

This release includes the dependency security updates from #60 and #71 and the
Python/ONNX migration from #72. The stack and dashboard require the corresponding
new sibling releases so installation cannot silently retain older project code.
MCP remains on SDK 1.x because SDK 2 removed the adapter's FastMCP API.

The full RAG installation uses ONNX Runtime 1.31 or newer. ONNX 1.31 wheels
support CPython 3.11–3.14 on Linux with glibc 2.28+ (x86-64 or ARM64), Windows
(x86-64 or ARM64), and macOS 14+ on Apple Silicon. See the
[RAG installation requirements](https://github.com/elkimek/getbased-agents/blob/main/packages/rag/README.md#install)
before upgrading. Standalone MCP and dashboard installations do not need the
native RAG dependencies.

Environment files are now written atomically with private permissions, and new
API key files are created privately before their contents are written. The
real-model integration suite covers ONNX batch consistency alongside the
PyTorch fallback, PDF ingestion, and MCP/RAG round trips. CI covers all four
packages and dependency audits on Python 3.11, 3.12, 3.13, and 3.14.
