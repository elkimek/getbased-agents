"""Activity API tests — verify tailing, aggregation, and boundary cases
(empty file, malformed lines, large file trimming)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-dashboard-key"}


def _write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ─── Auth ────────────────────────────────────────────────────────────

def test_activity_requires_auth(client: TestClient) -> None:
    assert client.get("/api/activity").status_code == 401


def test_clear_requires_auth(client: TestClient) -> None:
    assert client.delete("/api/activity").status_code == 401


# ─── Empty / missing log ─────────────────────────────────────────────

def test_missing_log_returns_empty_state(client: TestClient, cfg) -> None:
    r = client.get("/api/activity", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["records"] == []
    assert body["log_exists"] is False
    assert body["stats"]["total_calls"] == 0
    assert body["stats"]["tools"] == []


# ─── Tailing + aggregation ──────────────────────────────────────────

def test_records_returned_in_file_order(client: TestClient, cfg) -> None:
    _write_records(cfg.activity_log, [
        {"ts": 1.0, "tool": "a", "duration_ms": 10, "ok": True},
        {"ts": 2.0, "tool": "b", "duration_ms": 20, "ok": True},
        {"ts": 3.0, "tool": "a", "duration_ms": 30, "ok": False, "error": "X"},
    ])
    body = client.get("/api/activity", headers=AUTH).json()
    assert [r["tool"] for r in body["records"]] == ["a", "b", "a"]
    assert body["stats"]["total_calls"] == 3
    assert body["stats"]["total_errors"] == 1
    # Per-tool stats
    by_name = {t["tool"]: t for t in body["stats"]["tools"]}
    assert by_name["a"]["calls"] == 2
    assert by_name["a"]["errors"] == 1
    assert by_name["a"]["p50_ms"] in (10, 30)  # simple percentile impl
    assert by_name["b"]["errors"] == 0


def test_limit_applied_to_trailing_records(client: TestClient, cfg) -> None:
    _write_records(cfg.activity_log, [
        {"ts": float(i), "tool": f"t{i}", "duration_ms": i, "ok": True}
        for i in range(50)
    ])
    r = client.get("/api/activity?limit=5", headers=AUTH).json()
    # Last 5 records only
    assert [rec["tool"] for rec in r["records"]] == [f"t{i}" for i in range(45, 50)]


def test_limit_clamped_to_valid_range(client: TestClient, cfg) -> None:
    _write_records(cfg.activity_log, [
        {"ts": 1.0, "tool": "x", "duration_ms": 1, "ok": True}
    ])
    # limit=0 → clamp to 1
    assert client.get("/api/activity?limit=0", headers=AUTH).status_code == 200
    # limit=999999 → clamp to 1000, but we only have 1 record, so 1
    r = client.get("/api/activity?limit=999999", headers=AUTH).json()
    assert len(r["records"]) == 1


def test_malformed_lines_are_skipped(client: TestClient, cfg) -> None:
    """A partial write (power loss, kill -9) can leave a truncated last
    line. Parser must ignore it, not 500 the whole endpoint."""
    cfg.activity_log.parent.mkdir(parents=True, exist_ok=True)
    with cfg.activity_log.open("w") as f:
        f.write('{"ts": 1, "tool": "a", "duration_ms": 5, "ok": true}\n')
        f.write('{this is not valid json\n')
        f.write('\n')
        f.write('{"ts": 2, "tool": "b", "duration_ms": 8, "ok": true}\n')
    r = client.get("/api/activity", headers=AUTH).json()
    assert len(r["records"]) == 2
    assert [rec["tool"] for rec in r["records"]] == ["a", "b"]


def test_large_log_is_tailed_not_loaded_whole(client: TestClient, cfg) -> None:
    """Write more than the tail window (~512 KB). Endpoint must not
    return the whole file — we should see records from the tail only."""
    # Each line ≈ 100 bytes. 20 000 lines ≈ 2 MB — well past the 512 KB cap.
    cfg.activity_log.parent.mkdir(parents=True, exist_ok=True)
    with cfg.activity_log.open("w") as f:
        for i in range(20_000):
            f.write(json.dumps({"ts": float(i), "tool": "bulk", "duration_ms": 1, "ok": True}) + "\n")
    r = client.get("/api/activity?limit=10000", headers=AUTH).json()
    # We should have fewer than all 20 000 (tailed), but more than 0
    # (the tail window holds plenty)
    assert 100 < len(r["records"]) < 20_000


# ─── Clear ───────────────────────────────────────────────────────────

def test_clear_removes_log(client: TestClient, cfg) -> None:
    _write_records(cfg.activity_log, [
        {"ts": 1.0, "tool": "a", "duration_ms": 10, "ok": True}
    ])
    r = client.delete("/api/activity", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["log_exists"] is False
    assert body["records"] == []
    # File actually removed from disk
    assert not cfg.activity_log.exists()


def test_clear_when_log_already_missing(client: TestClient, cfg) -> None:
    """Clearing a non-existent log must not 500 — idempotent delete."""
    assert not cfg.activity_log.exists()
    r = client.delete("/api/activity", headers=AUTH)
    assert r.status_code == 200
