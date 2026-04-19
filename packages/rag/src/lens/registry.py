"""Library registry — per-library qdrant collections for the desktop engine.

Mirrors the semantics of the browser-local lens's multi-library layout
(js/lens-local-worker.js): users keep separate collections (research
papers, clinical guides, personal notes) and switch between them. Chat
grounds its answers in the active library only.

On-disk layout
  <data_dir>/libraries.json   {"activeId": "...", "libraries": [...]}
  <data_dir>/qdrant/          qdrant storage dir (shared, one collection per library)

Collection naming: each library's qdrant collection is `lib_<uuid-no-dashes>`.
Qdrant accepts [A-Za-z0-9_-]; the uuid-derived name stays safe and is distinct
from the legacy "knowledge" collection, which migrates into a "Default" library
on first upgrade.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import LensConfig

log = logging.getLogger("lens.registry")

LEGACY_COLLECTION = "knowledge"


def _new_id() -> str:
    return uuid.uuid4().hex


def _collection_for(library_id: str) -> str:
    # library_id is already hex-only (no dashes) but guard anyway.
    safe = "".join(c for c in library_id if c.isalnum() or c in "_-")
    return f"lib_{safe}"


class Registry:
    """Thin file-backed registry of libraries.

    Not thread-safe at write time — FastAPI runs one async worker by default,
    so concurrent writes don't happen under normal use. If the ops become
    parallel in the future, wrap mutations in a file lock (fcntl/msvcrt).
    """

    def __init__(self, config: LensConfig):
        self._config = config
        self._path = config.data_dir / "libraries.json"

    # ── Persistence ────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"activeId": "", "libraries": []}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("libraries.json unreadable (%s) — starting fresh", e)
            return {"activeId": "", "libraries": []}
        if not isinstance(data, dict):
            return {"activeId": "", "libraries": []}
        libs = data.get("libraries") or []
        if not isinstance(libs, list):
            libs = []
        return {
            "activeId": str(data.get("activeId") or ""),
            "libraries": [dict(l) for l in libs if isinstance(l, dict)],
        }

    def _save(self, state: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp + rename. Prevents a half-written JSON from
        # wiping the registry if the process is killed mid-write.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".libraries.",
            suffix=".json.tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, self._path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ── Public surface ────────────────────────────────────────────────
    def list(self) -> dict:
        state = self._load()
        # Redact nothing — the registry is hash-ids + user-chosen names.
        return {
            "activeId": state["activeId"],
            "libraries": [
                {
                    "id": l.get("id", ""),
                    "name": l.get("name", ""),
                    "createdAt": int(l.get("createdAt", 0)),
                }
                for l in state["libraries"]
            ],
        }

    def create(self, name: str) -> dict:
        name = (name or "").strip() or "Untitled"
        state = self._load()
        # Reject duplicate names outright — prevents rapid double-submit
        # from creating N libraries with the same label. Client-side
        # guards help but can't bulletproof across DOM re-renders; the
        # authoritative check lives here. Case-insensitive so "Research"
        # and "research" collide.
        for existing in state["libraries"]:
            if existing.get("name", "").strip().lower() == name.lower():
                raise ValueError(f"A library named {name!r} already exists")
        lib = {"id": _new_id(), "name": name, "createdAt": int(time.time() * 1000)}
        state["libraries"].append(lib)
        if not state["activeId"]:
            state["activeId"] = lib["id"]
        self._save(state)
        return lib

    def activate(self, library_id: str) -> str:
        state = self._load()
        if not any(l.get("id") == library_id for l in state["libraries"]):
            raise ValueError(f"No such library: {library_id}")
        state["activeId"] = library_id
        self._save(state)
        return library_id

    def rename(self, library_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("Name cannot be empty")
        state = self._load()
        found = None
        for l in state["libraries"]:
            if l.get("id") == library_id:
                l["name"] = name
                found = l
                break
        if not found:
            raise ValueError(f"No such library: {library_id}")
        self._save(state)
        return {
            "id": found["id"],
            "name": found["name"],
            "createdAt": int(found.get("createdAt", 0)),
        }

    def delete(self, library_id: str) -> None:
        state = self._load()
        before = len(state["libraries"])
        state["libraries"] = [l for l in state["libraries"] if l.get("id") != library_id]
        if len(state["libraries"]) == before:
            raise ValueError(f"No such library: {library_id}")
        if state["activeId"] == library_id:
            state["activeId"] = state["libraries"][0]["id"] if state["libraries"] else ""
        self._save(state)

    def active_id(self) -> str:
        state = self._load()
        return state["activeId"]

    def active_collection(self) -> str:
        """Qdrant collection name for the active library.

        If no libraries exist yet, returns an empty string — callers must
        handle this by calling `ensure_default()` first.
        """
        aid = self.active_id()
        return _collection_for(aid) if aid else ""

    def collection_for(self, library_id: str) -> str:
        return _collection_for(library_id)

    # ── Bootstrap / migration ─────────────────────────────────────────
    def ensure_default(self) -> str:
        """Make sure at least one library exists and is active. Returns the
        active library id.

        If the registry is empty but the legacy "knowledge" qdrant collection
        exists, creates a "Default" library pointing at a fresh collection —
        the old one is left untouched for a one-shot migration pass that the
        caller (server bootstrap) handles separately.
        """
        state = self._load()
        if state["libraries"]:
            # Repair: activeId missing or stale → pick first.
            active_ids = [l.get("id") for l in state["libraries"]]
            if state["activeId"] not in active_ids:
                state["activeId"] = active_ids[0]
                self._save(state)
            return state["activeId"]
        lib = self.create("Default")
        return lib["id"]
