"""Shared pytest fixtures for the Lens server test suite.

The real MiniLM embedder is ~90 MB and takes ~3 s to load — way too much
for unit tests. We patch `create_embedder` in `lens.server` to return a
deterministic hash-based fake, so tests run in milliseconds and CI
doesn't need to download model weights.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from lens import server as server_mod
from lens import store as store_mod
from lens.config import LensConfig
from lens.embedder import Embedder

DIM = 32  # tiny; keeps vectors small enough to be fast in Qdrant local


class FakeEmbedder(Embedder):
    """Deterministic text → vector mapping. Same text maps to the same
    vector across calls, different text to different vectors, and output
    is unit-normalised so cosine == dot product (matches the real
    embedder's contract). Keeps tests meaningful for similarity ranking
    without needing real weights."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            # Expand to DIM floats in [-1, 1] from the hash.
            raw = [((h[i % len(h)] / 128.0) - 1.0) for i in range(DIM)]
            norm = sum(x * x for x in raw) ** 0.5 or 1.0
            out.append([x / norm for x in raw])
        return out

    def dimension(self) -> int:
        return DIM


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LensConfig:
    """Isolated LensConfig pointed at a temp dir — no collision with the
    user's real data dir, no leftover state between tests."""
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LENS_SIMILARITY_FLOOR", "0.0")  # accept any score for tests
    return LensConfig.from_env()


@pytest.fixture
def patched_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap out the real embedder factory with the fake one so tests don't
    pay the MiniLM download cost."""
    monkeypatch.setattr(server_mod, "create_embedder", lambda cfg: FakeEmbedder())


@pytest.fixture
def shared_qdrant(config: LensConfig, monkeypatch: pytest.MonkeyPatch):
    """Local Qdrant takes an exclusive file lock — if the seeder and the
    server each instantiate their own QdrantBackend pointing at the same
    path, the second one throws "already accessed by another instance".

    We pin all QdrantBackend.client() calls during a test to one shared
    QdrantClient instance so seeder and server play nice. Production code
    always uses separate backends for cache-isolation; this fixture only
    bends that for the test process.
    """
    from qdrant_client import QdrantClient

    path = config.qdrant_path
    path.mkdir(parents=True, exist_ok=True)
    shared = QdrantClient(path=str(path))

    def _shared_client(self):
        return shared

    monkeypatch.setattr(store_mod.QdrantBackend, "client", _shared_client)
    yield shared
    shared.close()


@pytest.fixture
def client(config: LensConfig, patched_embedder: None, shared_qdrant) -> Iterator[TestClient]:
    """TestClient for the server bound to a temp config + fake embedder +
    shared Qdrant client (so seeder + server don't contend on the file lock)."""
    app = server_mod.create_app(config)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api_key(config: LensConfig) -> str:
    """Read the generated API key so auth'd tests can pass Bearer headers."""
    return config.api_key_file.read_text().strip()


@pytest.fixture
def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}
