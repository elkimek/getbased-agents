"""Vector store wrapper around Qdrant client (local + cloud modes)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .config import LensConfig

log = logging.getLogger("lens.store")


class QdrantBackend:
    """Shared Qdrant client — one per process.

    Local path-mode qdrant locks the storage directory, so multiple clients
    fighting over the same path will deadlock. Centralizing the client lets
    every per-library Store instance share the one lock.
    """

    def __init__(self, config: LensConfig):
        self._config = config
        self._client = None

    def client(self):
        if self._client is not None:
            return self._client
        from qdrant_client import QdrantClient

        if self._config.is_cloud:
            if not self._config.qdrant_cloud_url:
                raise ValueError("LENS_QDRANT_CLOUD_URL required for cloud mode")
            self._client = QdrantClient(
                url=self._config.qdrant_cloud_url,
                api_key=self._config.qdrant_cloud_key or None,
            )
            log.info("Connected to Qdrant Cloud: %s", self._config.qdrant_cloud_url)
        else:
            path = self._config.qdrant_path
            path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(path))
            log.info("Local Qdrant store at %s", path)
        return self._client

    def list_collection_names(self) -> list[str]:
        try:
            resp = self.client().get_collections()
            return [c.name for c in (resp.collections or [])]
        except Exception as e:
            log.warning("list_collections failed: %s", e)
            return []


class Store:
    """Thin wrapper over qdrant-client for one collection."""

    def __init__(
        self,
        config: LensConfig,
        collection: Optional[str] = None,
        backend: Optional[QdrantBackend] = None,
    ):
        self._config = config
        self._collection = collection or config.collection
        self._backend = backend or QdrantBackend(config)

    @property
    def collection(self) -> str:
        return self._collection

    def _client(self):
        return self._backend.client()

    def ensure_collection(self, dim: int) -> None:
        """Create the collection if it doesn't exist."""
        from qdrant_client.models import Distance, VectorParams

        try:
            existing = self._client().get_collection(self._collection)
            existing_dim = existing.config.params.vectors.size
            if existing_dim != dim:
                raise RuntimeError(
                    f"Collection {self._collection} has dim {existing_dim}, "
                    f"but embedder produces {dim}. Delete and re-ingest."
                )
            return
        except Exception:
            pass  # collection doesn't exist; create below

        self._client().create_collection(
            collection_name=self._collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        log.info("Created collection %s (dim=%d)", self._collection, dim)

    def search(self, vector: list[float], top_k: int, score_threshold: float) -> list[dict]:
        """Return list of {text, source, score} dicts.

        Uses query_points (qdrant-client 1.10+); falls back to deprecated search()
        for older clients so we work across version bumps.

        An empty library that was never ingested into has no Qdrant collection
        yet — we treat that as "no results" rather than 500'ing. Matches what
        a user would expect: query a fresh library, get an empty chunks list,
        ingest something, query again, get results.
        """
        try:
            # New API
            resp = self._client().query_points(
                collection_name=self._collection,
                query=vector,
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
            )
            points = resp.points
        except AttributeError:
            # Older qdrant-client (<1.10) — use legacy search()
            points = self._client().search(
                collection_name=self._collection,
                query_vector=vector,
                limit=top_k,
                score_threshold=score_threshold,
            )
        except ValueError as e:
            if "not found" in str(e).lower():
                return []
            raise
        out = []
        for r in points:
            payload = r.payload or {}
            out.append({
                "text": payload.get("text", ""),
                "source": payload.get("source", ""),
                "score": float(r.score),
            })
        return out

    def upsert(self, points: list[dict]) -> None:
        """Insert points: list of {id, vector, text, source}."""
        from qdrant_client.models import PointStruct

        structs = [
            PointStruct(
                id=p["id"],
                vector=p["vector"],
                payload={"text": p["text"], "source": p.get("source", "")},
            )
            for p in points
        ]
        self._client().upsert(collection_name=self._collection, points=structs)

    def count(self) -> int:
        """Number of vectors in the collection."""
        try:
            info = self._client().get_collection(self._collection)
            return int(info.points_count or 0)
        except Exception:
            return 0

    def clear(self) -> int:
        """Drop every chunk from the collection. Returns the pre-clear count.

        Recreates the collection with the same vector config immediately after
        deletion so the next query doesn't 404 on a missing collection. Without
        this, a "Remove all" followed by a chat query would throw 500 until the
        user re-ingested something to trigger ensure_collection().
        """
        from qdrant_client.models import Distance, VectorParams

        count = self.count()
        dim = None
        try:
            info = self._client().get_collection(self._collection)
            dim = int(info.config.params.vectors.size)
        except Exception as e:
            log.debug("get_collection before clear failed (likely empty): %s", e)
        try:
            self._client().delete_collection(self._collection)
        except Exception as e:  # noqa: BLE001
            log.warning("delete_collection failed, collection may not exist: %s", e)
        if dim is not None:
            try:
                self._client().create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("recreate empty collection failed: %s", e)
        return count

    def drop(self) -> None:
        """Delete the collection entirely, without recreating it. Used when a
        library is being deleted — there's no reason to leave an empty
        collection behind."""
        try:
            self._client().delete_collection(self._collection)
        except Exception as e:  # noqa: BLE001
            log.warning("drop collection %s failed: %s", self._collection, e)

    def list_sources(self) -> list[dict]:
        """Aggregate by source: returns [{source, chunks}, ...] sorted by source.

        Scrolls all points and groups client-side. For typical personal-knowledge-base
        size (<10k chunks) this is plenty fast. For larger deployments add a payload
        index + use Qdrant's facet API.
        """
        from collections import Counter
        counts: Counter = Counter()
        offset = None
        try:
            while True:
                points, offset = self._client().scroll(
                    collection_name=self._collection,
                    with_payload=["source"],
                    with_vectors=False,
                    limit=512,
                    offset=offset,
                )
                for p in points:
                    src = (p.payload or {}).get("source", "")
                    if src:
                        counts[src] += 1
                if offset is None:
                    break
        except Exception as e:
            log.warning("scroll failed: %s", e)
            return []
        return sorted(
            [{"source": s, "chunks": n} for s, n in counts.items()],
            key=lambda d: d["source"],
        )

    def delete_by_source(self, source: str) -> int:
        """Delete all chunks where payload.source == source. Returns count deleted."""
        from qdrant_client.models import (
            Filter,
            FieldCondition,
            MatchValue,
            FilterSelector,
        )

        before = self.count()
        try:
            self._client().delete(
                collection_name=self._collection,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="source", match=MatchValue(value=source))]
                    )
                ),
            )
        except Exception as e:
            log.warning("delete failed: %s", e)
            return 0
        after = self.count()
        return max(0, before - after)


def chunk_text(text: str, max_size: int = 800, overlap: int = 50, min_size: int = 50) -> list[str]:
    """Split text into chunks of roughly max_size chars with overlap.
    Tries to break on sentence boundaries when possible.
    """
    if len(text) <= max_size:
        return [text] if len(text) >= min_size else []

    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_size, len(text))
        if end < len(text):
            # Try to break at a sentence boundary
            for sep in (". ", "? ", "! ", "\n\n", "\n", " "):
                idx = text.rfind(sep, pos + min_size, end)
                if idx > 0:
                    end = idx + len(sep)
                    break
        chunk = text[pos:end].strip()
        if len(chunk) >= min_size:
            chunks.append(chunk)
        pos = end - overlap if end < len(text) else end
    return chunks
