#!/usr/bin/env python3
"""getbased MCP server — exposes blood work data and knowledge base search as tools.

Architecture:
  getbased (browser) → sync gateway → this MCP → your AI client
                                          ↕
                                    Lens RAG server (Qdrant + BGE-M3)

Blood work data is fetched from the getbased sync gateway.
Knowledge base queries go through the Lens RAG server (separate process).
No models are loaded in this process — everything is HTTP.
"""

import functools
import json
import logging
import os
import re
import time

import httpx
from mcp.server.fastmcp import FastMCP


def _maybe_load_user_env() -> None:
    """Opt-in: load $XDG_CONFIG_HOME/getbased/env into os.environ.

    Guarded by GETBASED_STACK_MANAGED=1 so existing deployments that wire env
    explicitly (Hermes via ~/.hermes/config.yaml, hand-rolled setups) are
    untouched. Uses setdefault — an explicit env var always wins over the file.
    Silent on a missing file; malformed lines skipped without crashing.
    Escape hatch: GETBASED_NO_ENV_FILE=1 disables even when managed.
    """
    if os.environ.get("GETBASED_STACK_MANAGED") != "1":
        return
    if os.environ.get("GETBASED_NO_ENV_FILE") == "1":
        return
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(xdg, "getbased", "env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


_maybe_load_user_env()

log = logging.getLogger("getbased_mcp")

mcp = FastMCP("getbased")

# ── Config ───────────────────────────────────────────────────────────
TOKEN = os.environ.get("GETBASED_TOKEN", "")
GATEWAY = os.environ.get("GETBASED_GATEWAY", "https://sync.getbased.health")

LENS_URL = os.environ.get("LENS_URL", f"http://localhost:{os.environ.get('LENS_PORT', '8322')}")


def _resolve_default_key_file() -> str:
    """Default Lens API key path. Prefer the XDG location used by getbased-rag;
    fall back to the legacy ~/.hermes/rag/lens_api_key so upgrades from the
    standalone getbased-mcp ≤ 0.1.0 don't silently break on boxes that still
    have the old key there (e.g. Hermes VMs)."""
    xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    new_default = os.path.join(xdg, "getbased", "lens", "api_key")
    legacy = os.path.expanduser("~/.hermes/rag/lens_api_key")
    if not os.path.exists(new_default) and os.path.exists(legacy):
        return legacy
    return new_default


LENS_API_KEY_FILE = os.environ.get("LENS_API_KEY_FILE", _resolve_default_key_file())

# Friendly message surfaced when a tool hits a route the lens server doesn't
# expose (old lens, pre-libraries). See _lens_call's 404 handling.
_UNSUPPORTED_LENS_HINT = (
    "this lens server doesn't expose library management. "
    "Upgrade to getbased-rag ≥ 0.2.0, or point LENS_URL at a library-capable lens."
)

# Cap on how much of an upstream error body we echo back to the AI client.
# Rag's exception_handler emits its own {error: ...} payload — safe in a
# self-hosted trust model, but that error often ends up in a cloud LLM's
# context window where the full response text (stack traces, file paths)
# would be sensitive. Truncate to a short hint.
_UPSTREAM_ERROR_PREVIEW = 200


# ── Activity logging ────────────────────────────────────────────────
# Every tool call writes one JSONL record: tool name, wall-clock ts,
# duration in ms, success flag, and error-class on failure. **Args are
# never logged** — queries can contain sensitive health info, so we
# record the shape of usage, not its content. The dashboard tails this
# file for the Activity tab; with no dashboard installed the file is
# just a rotating-by-hand log the user can inspect.
#
# Default path is $XDG_STATE_HOME/getbased/mcp/activity.jsonl
# (~/.local/state/getbased/mcp/activity.jsonl on Linux). Override with
# LENS_MCP_ACTIVITY_LOG. Set LENS_MCP_ACTIVITY_LOG=off to disable.

def _activity_log_path() -> str:
    """Resolve where activity records get appended. Env override wins;
    otherwise XDG_STATE_HOME. Returns "" when logging is disabled."""
    override = os.environ.get("LENS_MCP_ACTIVITY_LOG")
    if override is not None:
        return "" if override.lower() in ("off", "false", "0", "") else override
    state = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(state, "getbased", "mcp", "activity.jsonl")


def _append_activity(tool: str, duration_ms: int, ok: bool, error: str) -> None:
    """Best-effort JSONL append. Any I/O failure is swallowed — telemetry
    must never break a tool call. Directory is created on first write."""
    path = _activity_log_path()
    if not path:
        return
    record = {
        "ts": time.time(),
        "tool": tool,
        "duration_ms": duration_ms,
        "ok": ok,
    }
    if error:
        record["error"] = error
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        # Don't spam stderr in stdio MCP (it'd confuse the MCP client).
        # Debug-level is fine; the user can inspect via Python logging.
        log.debug("activity log append failed: %s", e)


def _instrumented(label: str):
    """Wrap an async tool implementation with success/failure + duration
    logging. Applied below each `@mcp.tool()` so the registered callable
    is the instrumented one. Preserves the function's signature and
    docstring via functools.wraps — FastMCP's tool registration reads
    those to build the tool schema."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            error_name = ""
            ok = True
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                ok = False
                error_name = type(e).__name__
                raise
            finally:
                _append_activity(
                    label,
                    int((time.monotonic() - t0) * 1000),
                    ok,
                    error_name,
                )

        return wrapper

    return deco


# ── Helpers ──────────────────────────────────────────────────────────

async def _fetch_context(profile: str = "") -> dict:
    """Fetch formatted lab context from the getbased sync gateway."""
    if not TOKEN:
        return {"error": "GETBASED_TOKEN not set"}
    try:
        params = {"profile": profile} if profile else {}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{GATEWAY}/api/context",
                headers={"Authorization": f"Bearer {TOKEN}"},
                params=params,
            )
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"getbased gateway returned {e.response.status_code}"}
    except httpx.RequestError as e:
        return {"error": f"Failed to reach getbased gateway: {e}"}


def _parse_sections(context: str) -> dict[str, str]:
    """Parse [section:name ...]...[/section:name] blocks → {full_name: content}."""
    sections = {}
    for m in re.finditer(
        r"\[section:(\S+)([^\]]*)\]([\s\S]*?)\[/section:\1\]", context
    ):
        base = m.group(1)
        meta = m.group(2).strip()
        full_name = f"{base} {meta}" if meta else base
        sections[full_name] = m.group(3).strip()
    return sections


def _read_lens_key() -> str:
    """Read the Lens API key from file (generated by lens_server.py)."""
    try:
        with open(LENS_API_KEY_FILE) as f:
            key = f.read().strip()
        return key if key else ""
    except OSError:
        return ""


async def _lens_request(query: str, top_k: int = 5) -> dict:
    """Send a query to the Lens RAG server. Returns parsed JSON or error dict."""
    key = _read_lens_key()
    if not key:
        return {"error": "Lens API key not found. Start lens_server.py first."}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{LENS_URL}/query",
                headers={"Authorization": f"Bearer {key}"},
                json={"version": 1, "query": query, "top_k": top_k},
            )
            r.raise_for_status()
            if len(r.content) > 32 * 1024:
                return {"error": "Lens response exceeds 32 KB — possible server issue"}
            return r.json()
    except httpx.ConnectError:
        return {"error": f"Lens server not reachable at {LENS_URL}. Is it running?"}
    except httpx.HTTPStatusError as e:
        # The error surfaces into an AI client (typically cloud-hosted),
        # so don't forward the raw response text — it may contain internal
        # paths or stack traces. Truncate to a short preview; if an
        # operator needs more detail they have the lens logs.
        preview = (e.response.text or "")[:_UPSTREAM_ERROR_PREVIEW]
        return {"error": f"Lens returned {e.response.status_code}: {preview}"}
    except httpx.RequestError as e:
        return {"error": f"Lens request failed: {e}"}
    except (json.JSONDecodeError, ValueError) as e:
        return {"error": f"Lens returned invalid JSON: {e}"}


async def _lens_call(method: str, path: str, json_body: dict | None = None) -> dict:
    """Generic authenticated call to the Lens server. Same error contract as
    _lens_request — every failure mode returns {"error": "..."} so tool
    callsites can uniformly forward errors to the MCP client without trying
    to catch exceptions themselves."""
    key = _read_lens_key()
    if not key:
        return {"error": "Lens API key not found. Start lens_server.py first."}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.request(
                method,
                f"{LENS_URL}{path}",
                headers={"Authorization": f"Bearer {key}"},
                json=json_body,
            )
            r.raise_for_status()
            return r.json() if r.content else {}
    except httpx.ConnectError:
        return {"error": f"Lens server not reachable at {LENS_URL}. Is it running?"}
    except httpx.HTTPStatusError as e:
        # Distinguish "this route doesn't exist on the server" (old lens, no
        # /libraries or /stats endpoint) from a genuine 404 like "library id
        # not found". FastAPI's default 404 body is `{"detail": "Not Found"}`;
        # the new lens returns a structured error for real misses.
        if e.response.status_code == 404:
            try:
                body = e.response.json()
                if body.get("detail") == "Not Found":
                    return {"error": "unsupported_endpoint"}
            except (json.JSONDecodeError, ValueError):
                pass
        # Same rationale as _lens_request: truncate the body preview so
        # internal details don't end up in a cloud AI client's context.
        preview = (e.response.text or "")[:_UPSTREAM_ERROR_PREVIEW]
        return {"error": f"Lens returned {e.response.status_code}: {preview}"}
    except httpx.RequestError as e:
        return {"error": f"Lens request failed: {e}"}
    except (json.JSONDecodeError, ValueError) as e:
        return {"error": f"Lens returned invalid JSON: {e}"}


# ═══════════════════════════════════════════════════════════════════════
# TOOLS — Blood work data
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
@_instrumented("getbased_lab_context")
async def getbased_lab_context(profile: str = "") -> str:
    """Get a full summary of the user's blood work data, health context,
    supplements, and goals from getbased. Use when the user asks broad
    questions about their labs, biomarkers, or health trends.
    Pass a profile ID to query a specific profile, or omit for the default."""
    data = await _fetch_context(profile)
    if "error" in data:
        return f"Error: {data['error']}"
    parts = []
    if data.get("profileId"):
        parts.append(f"Profile: {data['profileId']}")
    if data.get("updatedAt"):
        parts.append(f"Updated: {data['updatedAt']}")
    parts.append(data.get("context", "No context available"))
    return "\n\n".join(parts)


@mcp.tool()
@_instrumented("getbased_section")
async def getbased_section(section: str = "", profile: str = "") -> str:
    """Get a specific section of health data, or list all available sections.
    Call with no section name to get the index (section names + line counts).
    Call with a section name to get just that section's content.
    Sections include: biometrics, hormones, lipids, hematology, biochemistry,
    supplements, goals, genetics, context cards, etc.
    Section names are matched by prefix.
    Pass a profile ID to query a specific profile, or omit for the default."""
    data = await _fetch_context(profile)
    if "error" in data:
        return f"Error: {data['error']}"
    context = data.get("context", "")
    if not context:
        return "No context available"

    sections = _parse_sections(context)

    if not section:
        lines = []
        for name, content in sections.items():
            count = len([l for l in content.split("\n") if l.strip()])
            lines.append(f"  {name}  ({count} lines)")
        return "Available sections:\n\n" + "\n".join(lines)

    query = section.lower().strip()
    match_key = None
    for k in sections:
        if k.lower() == query:
            match_key = k
            break
    if not match_key:
        for k in sections:
            if k.lower().startswith(query):
                match_key = k
                break
    if not match_key:
        available = [k.split(" ")[0] for k in sections]
        return f'Section "{section}" not found\nAvailable: {", ".join(available)}'

    return f"[{match_key}]\n\n{sections[match_key]}"


@mcp.tool()
@_instrumented("getbased_wearables_series")
async def getbased_wearables_series(
    metric: str = "",
    days: int = 0,
    profile: str = "",
) -> str:
    """Read the wearable daily-values series the user opted into pushing.

    The user picks a window in Settings → Integrations → Agent Access:
    7, 30, or 90 days (or off). When set, the browser pushes a
    `[section:wearables-series-{N}d]` block to the gateway containing
    one line per metric, daily values separated by `→` (oldest to
    newest), `—` for no-reading days, and the primary source in parens.

    This tool extracts that series and optionally slices it.

    Args:
        metric: optional metric id to return only one line. Examples:
            'hrv_rmssd' (overnight HRV), 'rhr' (overnight resting HR),
            'hr_day' (daytime HR), 'sleep_score', 'readiness_score',
            'steps', 'weight'. Pass empty string for the whole matrix.
        days: optional preferred window. If 0, returns whichever
            window the user pushed. If 7/30/90, returns that section
            specifically (404 if not pushed). The browser only pushes
            ONE window at a time, so non-matching values fall back.
        profile: profile id (omit for default).

    Returns the section content, or a clear error if the user hasn't
    enabled the toggle yet.
    """
    data = await _fetch_context(profile)
    if "error" in data:
        return f"Error: {data['error']}"
    context = data.get("context", "")
    if not context:
        return "No context available"

    sections = _parse_sections(context)
    # Find the wearables-series-Nd section. Prefer requested `days`, else
    # whichever the user opted into.
    candidates = [k for k in sections if k.startswith("wearables-series-")]
    if not candidates:
        return (
            "No wearable series available. The user can enable this in "
            "getbased: Settings → Integrations → Agent Access → "
            "'Push wearable daily series'. Pick 7, 30, or 90 days."
        )

    chosen = None
    if days in (7, 30, 90):
        target = f"wearables-series-{days}d"
        chosen = next((k for k in candidates if k == target), None)
        if not chosen:
            available = [k.replace("wearables-series-", "").replace("d", "") for k in candidates]
            return (
                f"User hasn't pushed the {days}-day window. Currently "
                f"available: {', '.join(available)} day(s). They can "
                f"change the window in Settings → Integrations → Agent "
                f"Access."
            )
    else:
        chosen = candidates[0]

    content = sections[chosen]
    if not metric:
        return f"[{chosen}]\n\n{content}"

    # Parse one line. Lines look like:
    #   HRV (overnight) ms (oura): 33→35→32→…→39
    metric_lower = metric.lower().strip()
    matched = []
    for line in content.split("\n"):
        if not line or line.startswith("##"):
            continue
        # The metric id isn't directly in the line — labels are like
        # "HRV (overnight)" / "Resting HR" / "Steps". Match by checking
        # whether `metric_lower` appears in the line label OR the line
        # starts with a known label-form for that metric.
        head = line.split(":", 1)[0].lower()
        if metric_lower in head:
            matched.append(line)
            continue
        # Common id → label-fragment aliases. Browser emits labels via
        # `${label}${unit ? ' ' + unit : ''} (${primarySource})` where
        # `label` is `canon.label` followed by an optional `(${canon.sub})`.
        # For `hrv_rmssd` that produces `HRV (🌙) ms (oura)` — the literal
        # parens around the glyph mean substring matches like "hrv 🌙"
        # FAIL. List enough fragments per id to handle all the label forms
        # the canonical registry can emit.
        aliases = {
            # HRV overnight: label="HRV", sub="🌙" → "hrv (🌙)"
            "hrv_rmssd": ["hrv (🌙)", "hrv 🌙", "hrv (overnight)", "hrv overnight"],
            # HRV daytime: label="HRV", sub="☀️" → "hrv (☀️)"
            "hrv_day": ["hrv (☀", "hrv ☀", "hrv (daytime)", "hrv daytime"],
            # HRV SDNN (Apple Health): label="HRV", sub="SDNN" → "hrv (sdnn)"
            "hrv_sdnn": ["hrv (sdnn)", "hrv sdnn"],
            # Resting HR: label="Resting HR", sub="" → "resting hr"
            "rhr": ["resting hr", "resting heart"],
            # Heart rate daytime: label="Heart rate", sub="☀️" → "heart rate (☀️)"
            "hr_day": ["heart rate (☀", "heart rate ☀", "heart rate (daytime)", "heart rate daytime"],
            # Sleep score: label="Sleep", sub="score" → "sleep (score)"
            "sleep_score": ["sleep (score)", "sleep score"],
            "readiness_score": ["readiness (score)", "readiness score"],
            "activity_score": ["activity (score)", "activity score"],
            "stress_high_min": ["stress"],
            "resilience_level": ["resilience"],
            "cardio_age": ["cardio age"],
            "strain": ["strain (day)", "strain"],
            "steps": ["steps"],
            "weight": ["weight"],
            "bp_systolic": ["bp (syst)", "bp syst", "blood pressure systolic"],
            "bp_diastolic": ["bp (dia)", "bp dia", "blood pressure diastolic"],
            "spo2_avg": ["spo₂", "spo2"],
            "body_temp_delta": ["body temp", "body_temp"],
            "glucose_avg": ["glucose"],
            # Withings full coverage (getbased PR #140 / #143). Labels are
            # unsubbed for body comp, but sleep architecture carries subs
            # like "Sleep total", "Sleep HR (avg) bpm", etc.
            "pwv": ["pwv"],
            "vascular_age": ["vascular age"],
            "cardio_fitness": ["cardio fit"],
            "body_fat_pct": ["body fat"],
            "fat_mass_kg": ["fat mass"],
            "muscle_mass_kg": ["muscle"],
            "lean_mass_kg": ["lean mass"],
            "bone_mass_kg": ["bone"],
            "water_mass_kg": ["water"],
            "visceral_fat": ["visceral fat"],
            "nerve_health_score": ["nerve health"],
            "body_temp": ["body temp"],
            "skin_temp": ["skin temp"],
            "sleep_total_min": ["sleep total"],
            "sleep_deep_min": ["deep sleep"],
            "sleep_light_min": ["light sleep"],
            "sleep_rem_min": ["rem sleep"],
            "sleep_awake_min": ["awake (in bed)", "awake in bed"],
            "sleep_hr_avg": ["sleep hr (avg)", "sleep hr"],
            "sleep_breathing_rate": ["breathing (sleep)", "breathing"],
            "sleep_snoring_min": ["snoring"],
            "sleep_breath_disturb": ["apnea (level)", "apnea"],
        }
        for alias_id, label_forms in aliases.items():
            if alias_id == metric_lower and any(lf in head for lf in label_forms):
                matched.append(line)
                break

    if not matched:
        # Surface the available metric labels so the agent can retry.
        labels = []
        for line in content.split("\n"):
            if line and not line.startswith("##") and ":" in line:
                labels.append(line.split(":", 1)[0].strip())
        return (
            f"Metric '{metric}' not found in [{chosen}]. "
            f"Available labels: {' · '.join(labels)}"
        )

    return f"[{chosen}: {metric}]\n\n" + "\n".join(matched)


@mcp.tool()
@_instrumented("getbased_list_profiles")
async def getbased_list_profiles() -> str:
    """List all available profiles in getbased."""
    data = await _fetch_context()
    if "error" in data:
        return f"Error: {data['error']}"
    profiles = data.get("profiles") or []
    if not profiles:
        return "No profiles found"
    return "\n".join(
        f"{p.get('id', '?')}  {p.get('name', 'unnamed')}" for p in profiles
    )


# ═══════════════════════════════════════════════════════════════════════
# TOOLS — Knowledge base (RAG via Lens server)
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
@_instrumented("knowledge_search")
async def knowledge_search(
    query: str,
    n_results: int = 5,
) -> str:
    """Search the knowledge base for relevant passages using semantic similarity.

    Searches the **currently active library** on the Lens server. If the user
    has multiple libraries (research papers, clinical guides, personal notes),
    list them with `knowledge_list_libraries` and switch with
    `knowledge_activate_library` before searching.

    Returns the top-K passages ranked by relevance, with source
    attribution. Use this when the user asks about mechanisms, causal
    relationships, or prescriptive guidance related to health topics.

    Args:
        query: Natural language search query (e.g. "folic acid MTHFR methylation")
        n_results: Number of results to return (default 5, max 10)
    """
    n_results = max(1, min(10, n_results))
    data = await _lens_request(query, top_k=n_results)

    if "error" in data:
        return f"Knowledge search error: {data['error']}"

    chunks = data.get("chunks", [])[:10]  # mirror web-app MAX_CHUNKS
    if not chunks:
        return "No results found for that query."

    output_lines = []
    for i, chunk in enumerate(chunks):
        text = (chunk.get("text") or "")[:4000]
        source = (chunk.get("source") or "")[:200]
        output_lines.append(f"[{i + 1}] {source}")
        output_lines.append(text)
        output_lines.append("")

    return "\n".join(output_lines)


@mcp.tool()
@_instrumented("getbased_lens_config")
async def getbased_lens_config() -> str:
    """Get the Lens RAG endpoint configuration for getbased's Knowledge Base.
    Returns the URL, API key, and recommended top_k to paste into
    Settings → AI → Knowledge Base → External server in getbased.

    Note: Treat the response as sensitive — it contains the API key in plaintext."""
    key = _read_lens_key()
    if not key:
        return (
            "Lens API key not found. Start lens_server.py first to generate one.\n"
            f"Expected key file: {LENS_API_KEY_FILE}"
        )
    return (
        f"Endpoint URL: {LENS_URL}/query\n"
        f"API key (Bearer token): {key}\n"
        f"Recommended top_k: 5\n\n"
        "Paste these into getbased: Settings → AI → Knowledge Base → External server.\n"
        "For production (non-localhost), use HTTPS via a reverse proxy."
    )


@mcp.tool()
@_instrumented("knowledge_list_libraries")
async def knowledge_list_libraries() -> str:
    """List all knowledge base libraries on the Lens server, showing which is
    active. Use this to discover what collections the user has (research
    papers, clinical guides, personal notes, etc.) before searching or
    switching between them."""
    data = await _lens_call("GET", "/libraries")
    if data.get("error") == "unsupported_endpoint":
        return f"Knowledge libraries: {_UNSUPPORTED_LENS_HINT}"
    if "error" in data:
        return f"Knowledge libraries error: {data['error']}"
    libs = data.get("libraries") or []
    active = data.get("activeId", "")
    if not libs:
        return "No libraries found. Ingest at least one document to create the default library."
    lines = ["Libraries:"]
    for lib in libs:
        lib_id = lib.get("id", "")
        name = lib.get("name", "unnamed")
        marker = "  (active)" if lib_id == active else ""
        lines.append(f"  {lib_id}  {name}{marker}")
    return "\n".join(lines)


@mcp.tool()
@_instrumented("knowledge_activate_library")
async def knowledge_activate_library(library_id: str) -> str:
    """Switch the Lens server's active library. All subsequent
    `knowledge_search` and `knowledge_stats` calls will target this library
    until switched again. Use `knowledge_list_libraries` first to find the ID.

    Args:
        library_id: The library's ID (not its display name). Obtained from
            knowledge_list_libraries.
    """
    if not library_id:
        return "Error: library_id is required. Call knowledge_list_libraries to find one."
    data = await _lens_call("POST", f"/libraries/{library_id}/activate")
    if data.get("error") == "unsupported_endpoint":
        return f"Activate library: {_UNSUPPORTED_LENS_HINT}"
    if "error" in data:
        return f"Activate library error: {data['error']}"
    libs = data.get("libraries") or []
    active = data.get("activeId", "")
    for lib in libs:
        if lib.get("id") == active:
            return f"Active library is now: {lib.get('name', active)} ({active})"
    return f"Active library is now: {active}"


@mcp.tool()
@_instrumented("knowledge_stats")
async def knowledge_stats() -> str:
    """Get per-source chunk counts for the active knowledge base library.
    Tells you which documents are indexed and how many excerpts each
    contributes. Useful when diagnosing "I can't find X" — either the source
    isn't indexed, or the relevant passages didn't score high enough."""
    data = await _lens_call("GET", "/stats")
    if data.get("error") == "unsupported_endpoint":
        return f"Knowledge stats: {_UNSUPPORTED_LENS_HINT}"
    if "error" in data:
        return f"Knowledge stats error: {data['error']}"
    total = data.get("total_chunks", 0)
    docs = data.get("documents") or []
    if not docs:
        return f"Active library is empty (total chunks: {total})."
    lines = [f"Total chunks: {total}", "", "Sources:"]
    for doc in docs:
        src = doc.get("source", "unknown")
        chunks = doc.get("chunks", 0)
        lines.append(f"  {chunks:>6}  {src}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
