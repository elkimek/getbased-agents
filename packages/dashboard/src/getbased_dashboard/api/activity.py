"""Activity API — tail the MCP's JSONL activity log and surface the
recent records + simple aggregations.

The log is written by getbased-mcp at `$XDG_STATE_HOME/getbased/mcp/
activity.jsonl` (configurable via LENS_MCP_ACTIVITY_LOG). One record
per tool call: tool name, timestamp, duration, ok flag, error class on
failure. Args are never logged upstream so we don't have to strip them.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request

from ..config import DashboardConfig
from ..server import _require_auth


def _cfg(request: Request) -> DashboardConfig:
    return request.app.state.config


# Cap on how much of the log we read per request. Users with heavy agent
# usage can accumulate megabytes quickly — loading the entire file on
# every poll is wasteful. Tailing from the end keeps the endpoint O(cap)
# regardless of how long the log has been running.
_TAIL_BYTES = 512 * 1024


def _read_records(path: Path, limit: int) -> list[dict]:
    """Return up to `limit` most-recent records. If the file is under
    _TAIL_BYTES read the whole thing; otherwise seek from the end. Malformed
    lines (partial last write, corrupt records) are silently skipped so
    one bad line can't hide the rest."""
    if not path.exists():
        return []
    size = path.stat().st_size
    try:
        if size <= _TAIL_BYTES:
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            with path.open("rb") as f:
                f.seek(size - _TAIL_BYTES)
                # Drop the first (likely partial) line so we don't parse
                # garbage. There will always be a complete line after the
                # first newline we find, assuming writers use line-atomic
                # append — which Python's text-mode write does.
                f.readline()
                text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                records.append(rec)
        except json.JSONDecodeError:
            continue

    return records[-limit:]


def _aggregate(records: list[dict]) -> dict:
    """Per-tool counts, success rate, and P50/P95 latency. O(N log N) —
    fine up to the ~thousand records our tail window holds."""
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        t = r.get("tool")
        if isinstance(t, str):
            by_tool[t].append(r)

    def _percentile(sorted_vals: list[int], p: float) -> int | None:
        if not sorted_vals:
            return None
        idx = int(p * (len(sorted_vals) - 1))
        return sorted_vals[idx]

    tools: list[dict] = []
    for name, group in sorted(by_tool.items()):
        durations = sorted(
            int(r.get("duration_ms", 0)) for r in group if isinstance(r.get("duration_ms"), (int, float))
        )
        errors = sum(1 for r in group if not r.get("ok", True))
        tools.append(
            {
                "tool": name,
                "calls": len(group),
                "errors": errors,
                "error_rate": (errors / len(group)) if group else 0.0,
                "p50_ms": _percentile(durations, 0.5),
                "p95_ms": _percentile(durations, 0.95),
            }
        )

    total_errors = sum(1 for r in records if not r.get("ok", True))
    return {
        "total_calls": len(records),
        "total_errors": total_errors,
        "overall_error_rate": (total_errors / len(records)) if records else 0.0,
        "tools": tools,
    }


def register(app: FastAPI) -> None:
    router = APIRouter(prefix="/api/activity", tags=["activity"])

    @router.get("")
    async def activity_feed(request: Request, limit: int = 200):
        cfg = _cfg(request)
        _require_auth(request, cfg)
        # Bound limit so a client can't ask us to return 10 million records
        # in one payload. 1000 is plenty for a dashboard tick.
        limit = max(1, min(1000, int(limit)))
        records = _read_records(cfg.activity_log, limit)
        stats = _aggregate(records)
        return {
            "log_path": str(cfg.activity_log),
            "log_exists": cfg.activity_log.exists(),
            "records": records,
            "stats": stats,
        }

    @router.delete("")
    async def clear_activity(request: Request):
        """Wipe the log. Useful for resetting the dashboard's view after
        a period of testing. Returns the new (empty) state so the UI can
        refresh in one round-trip."""
        cfg = _cfg(request)
        _require_auth(request, cfg)
        try:
            if cfg.activity_log.exists():
                os.unlink(cfg.activity_log)
        except OSError:
            # File may have been created by another process or removed in
            # a race; either way we want to return "nothing here" state.
            pass
        return {
            "log_path": str(cfg.activity_log),
            "log_exists": False,
            "records": [],
            "stats": _aggregate([]),
        }

    app.include_router(router)
