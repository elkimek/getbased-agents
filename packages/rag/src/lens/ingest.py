"""Document ingestion — read files from disk, chunk, embed, store."""

from __future__ import annotations

import logging
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .config import LensConfig
from .embedder import create_embedder
from .registry import Registry
from .store import QdrantBackend, Store, chunk_text

log = logging.getLogger("lens.ingest")

# File loaders — heavy deps are imported lazily so the lens core doesn't pull them
SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".rst", ".json", ".pdf", ".docx"}


@contextmanager
def _expand_zip_if_needed(source: Path):
    """Yield the real path to walk. If `source` is a .zip, extract to a
    temp dir and yield that; cleanup happens when the context closes.
    Otherwise yield `source` unchanged. Rejects absolute paths and ".."
    components inside the archive to prevent zip-slip writes outside tmp.
    """
    if not (source.is_file() and source.suffix.lower() == ".zip"):
        yield source
        return

    with tempfile.TemporaryDirectory(prefix="lens-zip-") as tmp:
        tmp_path = Path(tmp).resolve()
        with zipfile.ZipFile(source) as zf:
            for member in zf.namelist():
                # zip-slip guard. Path.is_relative_to catches both absolute
                # and parent-walking entries without the prefix-match off-by-one
                # that str.startswith has (e.g. /tmp/lens-zip-abc would match
                # /tmp/lens-zip-abc-evil/x under naive prefix matching).
                target = (tmp_path / member).resolve()
                try:
                    target.relative_to(tmp_path)
                except ValueError:
                    raise RuntimeError(f"Unsafe zip entry: {member}")
            zf.extractall(tmp_path)
        log.info("Extracted zip %s into %s", source.name, tmp_path)
        yield tmp_path


MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB — a research PDF fits; zip bombs don't


def _read_text(path: Path) -> str:
    # Cap per-file size before any parser runs. Caller's try/except treats
    # RuntimeError as "skip this file, continue with the rest", so an
    # oversized outlier can't take down the whole batch.
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise RuntimeError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB cap ({size} bytes)")

    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".markdown", ".rst", ".json"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            raise RuntimeError(
                "PDF ingest requires PyPDF2. Install lens with: pip install 'getbased-lens[pdf]'"
            )
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        try:
            import docx  # type: ignore
        except ImportError:
            raise RuntimeError(
                "DOCX ingest requires python-docx. Install lens with: pip install 'getbased-lens[docx]'"
            )
        doc = docx.Document(str(path))
        return "\n\n".join(p.text for p in doc.paragraphs)
    raise RuntimeError(f"Unsupported file type: {suffix}")


def _expand_zips_in_dir(root: Path) -> None:
    """Find every .zip inside `root`, extract it to a sibling directory
    (`_zip_<stem>`), and delete the original. Idempotent — already-
    extracted zips are skipped by re-checking existence. Called before
    _walk so uploads that contain a mix of zips + loose files behave
    the same as `lens ingest file.zip` from the CLI.

    Why this exists: the HTTP /ingest endpoint saves all uploads into
    one temp dir and passes that dir to ingest_path. Without expansion,
    `.zip` files fail the SUPPORTED_EXTS filter in _walk() and their
    contents never reach the parser. The single-file CLI path hits
    _expand_zip_if_needed directly, but the multi-file HTTP path does
    not — so we normalise here.
    """
    if not root.is_dir():
        return
    zips = list(root.rglob("*.zip"))
    for zip_path in zips:
        if not zip_path.is_file():
            continue
        # Skip if we already extracted it this run (some corner cases
        # where a zip sits inside another zip).
        target_dir = zip_path.parent / f"_zip_{zip_path.stem}"
        if target_dir.exists():
            continue
        root_abs = root.resolve()
        try:
            target_dir.mkdir(parents=True, exist_ok=False)
            target_abs = target_dir.resolve()
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    dest = (target_abs / member).resolve()
                    # zip-slip guard — entries must land inside the
                    # per-zip target, not leak out via "../".
                    try:
                        dest.relative_to(target_abs)
                    except ValueError:
                        raise RuntimeError(f"Unsafe zip entry: {member}")
                    # And must stay inside the overall ingest root too
                    # (defence in depth; redundant given the per-zip
                    # check but cheap).
                    try:
                        dest.relative_to(root_abs)
                    except ValueError:
                        raise RuntimeError(f"Zip entry escapes root: {member}")
                zf.extractall(target_abs)
            log.info(
                "Expanded %s → %s (%d entries)",
                zip_path.name,
                target_dir.name,
                len(zipfile.ZipFile(zip_path).namelist()),
            )
            zip_path.unlink()
        except (zipfile.BadZipFile, RuntimeError, OSError) as e:
            log.warning("Failed to expand %s: %s — leaving zip in place", zip_path, e)
            # Don't crash the whole ingest — just move on and the zip
            # will be silently skipped by _walk's extension filter.
            # Clean up partial extraction.
            if target_dir.exists():
                import shutil as _shutil

                _shutil.rmtree(target_dir, ignore_errors=True)


def _walk(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    # Auto-extract any zips the caller dropped in (matches CLI single-
    # file behaviour for ingest_path(some.zip)).
    _expand_zips_in_dir(root)
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            yield p


def ingest_path(
    config: LensConfig,
    source: Path,
    emit_progress: bool = False,
    store: Store | None = None,
    embedder=None,
    backend: QdrantBackend | None = None,
    on_event=None,
    should_cancel=None,
) -> dict:
    """Ingest a file or directory into the lens store. Returns summary stats.
    A .zip input is auto-extracted into a temp directory and ingested as if
    the user had passed that directory; the temp dir is removed on exit.

    Progress reporting has two transports:
      - `emit_progress=True` prints JSONL events to stdout (CLI/subprocess
        consumer path — Rust / Tauri read this).
      - `on_event=<callable>` gets each event dict directly (HTTP streaming
        path — the server plumbs events into an NDJSON response).

    Events: {"event":"start","total":N}, {"event":"file","index":i,
    "total":N,"source":"...","chunks":n,"skipped"?:bool}. The return value
    is the final summary dict (no "event" key).

    `store`, `embedder`, and `backend` are for in-process reuse — when the
    HTTP server calls ingest it passes its own singletons, otherwise a
    second QdrantBackend would race the server for the file lock on local
    Qdrant. CLI callers leave them None and get fresh instances.
    """
    if not source.exists():
        raise FileNotFoundError(f"No such path: {source}")

    with _expand_zip_if_needed(source) as walk_root:
        return _ingest_walk(
            config,
            walk_root,
            emit_progress=emit_progress,
            store=store,
            embedder=embedder,
            backend=backend,
            on_event=on_event,
            should_cancel=should_cancel,
        )


def _ingest_walk(
    config: LensConfig,
    source: Path,
    emit_progress: bool = False,
    store: Store | None = None,
    embedder=None,
    backend: QdrantBackend | None = None,
    on_event=None,
    should_cancel=None,
) -> dict:
    import json as _json
    import sys as _sys

    def _emit(**event):
        # Direct-callback path wins when both are set — HTTP streaming
        # and stdout-to-subprocess are orthogonal use cases.
        if on_event is not None:
            try:
                on_event(event)
            except Exception as e:
                log.debug("progress callback failed: %s", e)
        if not emit_progress:
            return
        print(_json.dumps(event), flush=True)
        # Also echo to stderr so a human watching the CLI can see activity
        # if they ever run `lens ingest --json` manually.
        print(_json.dumps(event), file=_sys.stderr, flush=True)

    if embedder is None:
        embedder = create_embedder(config)
    # Ingest always targets the ACTIVE library. Bootstrap a default library
    # if none exists — mirrors the browser-local lens semantics.
    if store is None:
        registry = Registry(config)
        registry.ensure_default()
        if backend is None:
            backend = QdrantBackend(config)
        store = Store(config, collection=registry.active_collection(), backend=backend)
    store.ensure_collection(embedder.dimension())

    def _cancelled() -> bool:
        if should_cancel is None:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    # ── Pass 1: plan ────────────────────────────────────────────────
    # Read + chunk every file up front, collect (text, source) tuples.
    # Produces an accurate total-chunks count so the UI can show a
    # real progress bar ("142/850 excerpts · 3.2/s") instead of
    # guessing. Cost is O(disk reads + chunker), no embedder cost —
    # embedding dominates wall-clock by 99%, so planning is effectively
    # free. Matches the browser-local lens's two-pass flow.
    all_files = list(_walk(source))
    skipped: list[str] = []
    planned: list[dict] = []
    for file_path in all_files:
        if _cancelled():
            break
        try:
            text = _read_text(file_path)
        except Exception as e:
            log.warning("Skipping %s: %s", file_path, e)
            skipped.append(str(file_path))
            continue
        if not text.strip():
            continue
        rel_source = str(
            file_path.relative_to(source.parent if source.is_file() else source)
        )
        for chunk in chunk_text(
            text,
            max_size=config.chunk_max_size,
            overlap=config.chunk_overlap,
            min_size=config.chunk_min_size,
        ):
            planned.append({"text": chunk, "source": rel_source})

    chunks_planned = len(planned)
    sources_planned = {p["source"] for p in planned}
    _emit(event="start", total=chunks_planned)

    # Source-level dedup: re-ingesting the same file would otherwise
    # create a parallel set of chunks (each gets a fresh uuid4 below).
    for src in sources_planned:
        try:
            store.delete_by_source(src)
        except Exception as e:
            log.debug(
                "Pre-ingest dedup delete failed for %s (likely first ingest): %s",
                src, e,
            )

    # ── Pass 2: embed ───────────────────────────────────────────────
    # Emit cadence matches the browser-local lens: a progress event
    # every ~5 chunks (or at end of run). The embed batch size = emit
    # cadence so each encode() call maps to one progress update —
    # trades a bit of embedder throughput for noticeably smoother
    # progress bar + live chunks/sec rate. On MiniLM the difference is
    # single-digit percent on the total embed time, well worth the UX.
    BATCH = 5
    chunks_indexed = 0
    cancelled = False
    last_source = ""

    for i in range(0, chunks_planned, BATCH):
        if _cancelled():
            cancelled = True
            break
        batch = planned[i : i + BATCH]
        texts = [b["text"] for b in batch]
        try:
            vectors = embedder.encode(texts)
        except Exception:
            log.exception("Embedding batch %d-%d failed", i, i + len(batch))
            # Non-fatal — skip the batch and keep going. Partial ingest
            # is better than losing the whole run on one bad batch.
            continue
        points = [
            {
                "id": str(uuid4()),
                "text": b["text"],
                "source": b["source"],
                "vector": v,
            }
            for b, v in zip(batch, vectors)
        ]
        store.upsert(points)
        chunks_indexed += len(points)
        last_source = batch[-1]["source"]
        _emit(
            event="embed",
            index=chunks_indexed,
            total=chunks_planned,
            source=last_source,
        )

    # If cancellation fired during pass 1 (before we ever reached the
    # embed loop), the pass-2 loop never ran so `cancelled` is still
    # False. Correct that so the UI sees the intent.
    if not cancelled and _cancelled():
        cancelled = True

    files_seen = len(sources_planned)
    return {
        "files_seen": files_seen,
        "chunks_indexed": chunks_indexed,
        "chunks_planned": chunks_planned,
        "cancelled": cancelled,
        "skipped": skipped,
    }
