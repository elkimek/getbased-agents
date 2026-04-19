"""Embedding backends — ONNX Runtime, sentence-transformers, or Qdrant Cloud Inference.

ABC design with lazy-loading, caching, and a factory function.
The ONNX backend is preferred when available (set via LENS_ONNX_PROVIDER env var)
because it's lighter than PyTorch and supports GPU acceleration directly.
"""

from __future__ import annotations

import logging
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from .config import LensConfig

log = logging.getLogger("lens.embedder")


def _platform_getbased_data_dirs() -> list[Path]:
    """Default getbased data directories per platform — matches what Tauri uses.
    Returns multiple candidates to handle dev (XDG) and bundled (platform-default) layouts.
    """
    home = Path.home()
    paths = []
    if sys.platform == "darwin":
        # Tauri's dirs::data_dir() on macOS = ~/Library/Application Support
        paths.append(home / "Library" / "Application Support" / "getbased" / "lens" / "models")
    elif sys.platform.startswith("win"):
        # Tauri's dirs::data_dir() on Windows = %APPDATA% (Roaming)
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "getbased" / "lens" / "models")
    else:
        # Linux: dirs::data_dir() = $XDG_DATA_HOME or ~/.local/share
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            paths.append(Path(xdg) / "getbased" / "lens" / "models")
        paths.append(home / ".local" / "share" / "getbased" / "lens" / "models")
    # Always include the legacy ~/.getbased/lens/models for back-compat
    paths.append(home / ".getbased" / "lens" / "models")
    return paths

# ── ABC ────────────────────────────────────────────────────────────

class Embedder(ABC):
    """Abstract embedding interface."""

    @abstractmethod
    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch of texts into normalized vectors."""
        ...

    @abstractmethod
    def dimension(self) -> int:
        """Return the embedding dimensionality."""
        ...

    def info(self) -> dict:
        """Return a small dict describing this backend — which engine,
        which model, active provider, etc. Surfaces to the dashboard so
        users can see at a glance what's doing the embedding work.
        Subclasses override; default reports engine name only."""
        return {"engine": self.__class__.__name__, "dimension": self.dimension()}


# ── Known model dimensions ────────────────────────────────────────

_MODEL_DIMS: dict[str, int] = {
    "all-MiniLM-L6-v2": 384,
    "all-MiniLM-L12-v2": 384,
    "BAAI/bge-m3": 1024,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
}


# ── ONNX model → pre-exported HF repo map ─────────────────────────
# Every entry maps a "canonical" model name (the one we expose in our
# /models API and config) to a HuggingFace repo that already ships
# pre-exported ONNX weights. Using pre-exported models lets us skip
# the PyTorch→ONNX conversion step entirely — no optimum dep, no
# transformers dep, no /tmp space requirement.
_ONNX_REPO_MAP: dict[str, str] = {
    "sentence-transformers/all-MiniLM-L6-v2": "Xenova/all-MiniLM-L6-v2",
    "all-MiniLM-L6-v2": "Xenova/all-MiniLM-L6-v2",
    "sentence-transformers/all-MiniLM-L12-v2": "Xenova/all-MiniLM-L12-v2",
    "all-MiniLM-L12-v2": "Xenova/all-MiniLM-L12-v2",
    "BAAI/bge-small-en-v1.5": "Xenova/bge-small-en-v1.5",
    "BAAI/bge-base-en-v1.5": "Xenova/bge-base-en-v1.5",
    "BAAI/bge-large-en-v1.5": "Xenova/bge-large-en-v1.5",
    "BAAI/bge-m3": "Xenova/bge-m3",
}


# ── ONNX Runtime (preferred) ─────────────────────────────────────

class OnnxEmbedder(Embedder):
    """Embedding via ONNX Runtime — light, fast, GPU-accelerated.

    Loads pre-exported ONNX weights directly from HuggingFace via
    huggingface_hub — no optimum, no transformers, no PyTorch→ONNX
    conversion step. The tokenizer is loaded from `tokenizer.json`
    via the `tokenizers` Rust-backed library, which reads the
    fast-tokenizer config without pulling the transformers package.

    Provider is set via LENS_ONNX_PROVIDER env var. Falls back to CPU
    if the requested provider isn't available at runtime.

    Canonical model names (all-MiniLM-L6-v2, BAAI/bge-m3, etc.) are
    mapped to community ONNX re-exports at import time — see
    _ONNX_REPO_MAP. Users can also pass a HuggingFace repo id directly
    and we'll try to load from it as-is (useful for custom finetunes).
    """

    _PROVIDER_MAP: dict[str, list[str]] = {
        "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "rocm": ["ROCmExecutionProvider", "CPUExecutionProvider"],
        "openvino": ["OpenVINOExecutionProvider", "CPUExecutionProvider"],
        "coreml": ["CoreMLExecutionProvider", "CPUExecutionProvider"],
        "cpu": ["CPUExecutionProvider"],
    }

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2", provider: str = ""):
        self._model_name = model_name
        self._onnx_repo = _ONNX_REPO_MAP.get(model_name, model_name)
        self._provider_name = provider
        self._session = None
        self._tokenizer = None
        self._dim: int | None = None
        self._needs_token_type_ids = False
        self._max_seq_len = 512  # overridden per-model on load

    # lazy init --------------------------------------------------------

    def _load(self) -> None:
        if self._session is not None:
            return

        import onnxruntime as ort

        providers = self._resolve_providers(ort)
        log.info(
            "Loading ONNX model: %s (repo=%s, providers=%s)",
            self._model_name, self._onnx_repo, providers,
        )

        model_dir = self._resolve_model_dir()
        onnx_file = self._find_onnx_file(model_dir)

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(onnx_file),
            sess_options=sess_options,
            providers=providers,
        )

        active = self._session.get_providers()
        log.info("ONNX session active providers: %s", active)

        # Does this model want token_type_ids? BERT-family yes, many
        # modern embeddings no. Inspect the session's declared inputs.
        input_names = {inp.name for inp in self._session.get_inputs()}
        self._needs_token_type_ids = "token_type_ids" in input_names

        # Tokenizer — tokenizers lib reads tokenizer.json natively.
        from tokenizers import Tokenizer

        tok_file = model_dir / "tokenizer.json"
        if not tok_file.exists():
            raise FileNotFoundError(
                f"tokenizer.json not found in {model_dir}. The repo "
                f"{self._onnx_repo!r} may not ship a fast tokenizer."
            )
        self._tokenizer = Tokenizer.from_file(str(tok_file))

        # BGE-M3 has 8192 context; most others 512. Read model_max_length
        # from the tokenizer config if present, else use a name-based
        # heuristic.
        self._max_seq_len = self._detect_max_len(model_dir)

        # Dimension: prefer known; else probe with a short input
        self._dim = self._detect_dimension()
        log.info(
            "ONNX model ready (dim=%d, provider=%s, max_len=%d)",
            self._dim, active[0], self._max_seq_len,
        )

    def _resolve_providers(self, ort) -> list[str]:
        available = ort.get_available_providers()
        log.debug("Available ONNX providers: %s", available)
        if self._provider_name and self._provider_name in self._PROVIDER_MAP:
            requested = self._PROVIDER_MAP[self._provider_name]
            resolved = [p for p in requested if p in available]
            if resolved:
                return resolved
            log.warning(
                "Requested provider '%s' not available (have: %s), falling back",
                self._provider_name, available,
            )
        for provider_key in ("cuda", "rocm", "openvino", "coreml"):
            chain = self._PROVIDER_MAP[provider_key]
            if any(p in available for p in chain):
                return [p for p in chain if p in available]
        return ["CPUExecutionProvider"]

    def _resolve_model_dir(self) -> Path:
        """Download (or reuse cached) ONNX model files from HuggingFace.

        Uses huggingface_hub.snapshot_download with a tight allow_patterns
        list — we only pull what we need (model.onnx, tokenizer.json,
        config.json). Skips the conversion step entirely, so there's no
        `/tmp` space requirement for `.onnx.data` files.

        Cache honours the standard HF_HOME / HUGGINGFACE_HUB_CACHE env
        vars so models co-locate with any other HF-using tool on the
        same machine.
        """
        # If already present in a known local location, use it — avoids
        # a network touch every first-call.
        for local in self._local_candidates():
            if local and (local / "tokenizer.json").exists():
                if self._find_onnx_file_silent(local) is not None:
                    log.info("Using cached ONNX model at %s", local)
                    return local

        # Download from HF
        try:
            from huggingface_hub import snapshot_download
        except ImportError as e:
            raise ImportError(
                "ONNX backend requires `huggingface_hub` and `tokenizers`. "
                "Install with: pip install 'getbased-rag[full]'"
            ) from e

        log.info("Downloading ONNX weights from HF: %s", self._onnx_repo)
        local = snapshot_download(
            repo_id=self._onnx_repo,
            allow_patterns=[
                "onnx/model.onnx",
                "onnx/model.onnx_data",
                "onnx/model_quantized.onnx",
                "model.onnx",
                "model.onnx_data",
                "tokenizer.json",
                "tokenizer_config.json",
                "config.json",
                "special_tokens_map.json",
            ],
        )
        return Path(local)

    def _local_candidates(self) -> list[Path]:
        """Candidate local paths to check before hitting the network."""
        out: list[Path] = []
        # LENS_DATA_DIR / models / models--{slug}/snapshots/{rev}/ —
        # where huggingface_hub drops files when HF_HOME is set there.
        env_dir = os.environ.get("LENS_DATA_DIR")
        if env_dir:
            models = Path(env_dir) / "models"
            if models.exists():
                out.extend(self._snapshot_dirs(models))
        # Platform default getbased data dir
        for d in _platform_getbased_data_dirs():
            if d.exists():
                out.extend(self._snapshot_dirs(d))
        # Standard HF hub cache
        hf_cache = Path(
            os.environ.get("HF_HOME", "") or Path.home() / ".cache" / "huggingface"
        ) / "hub"
        if hf_cache.exists():
            out.extend(self._snapshot_dirs(hf_cache))
        return out

    def _snapshot_dirs(self, root: Path) -> list[Path]:
        """All snapshot dirs under `root` that match our ONNX repo id."""
        slug = self._onnx_repo.replace("/", "--")
        repo_dir = root / f"models--{slug}"
        if not repo_dir.exists():
            return []
        snap = repo_dir / "snapshots"
        if not snap.exists():
            return []
        return sorted(snap.iterdir(), reverse=True)

    def _find_onnx_file(self, model_dir: Path) -> Path:
        """Pick the best ONNX weights file, falling back through options."""
        found = self._find_onnx_file_silent(model_dir)
        if found is None:
            raise FileNotFoundError(
                f"No .onnx files found in {model_dir}. The repo "
                f"{self._onnx_repo!r} may not include ONNX weights."
            )
        return found

    def _find_onnx_file_silent(self, model_dir: Path) -> Path | None:
        for candidate in (
            model_dir / "onnx" / "model.onnx",
            model_dir / "model.onnx",
            model_dir / "onnx" / "model_quantized.onnx",
        ):
            if candidate.exists():
                return candidate
        # Last-ditch glob
        for onnx_file in model_dir.rglob("*.onnx"):
            return onnx_file
        return None

    def _detect_max_len(self, model_dir: Path) -> int:
        """Read model_max_length from tokenizer_config.json if present.
        Falls back to 8192 for BGE-M3 (long-context), 512 otherwise."""
        import json as _json

        cfg = model_dir / "tokenizer_config.json"
        if cfg.exists():
            try:
                data = _json.loads(cfg.read_text())
                ml = int(data.get("model_max_length", 0))
                # Some HF tokenizers ship a sentinel ≈1e30 meaning "no
                # cap"; treat anything absurd as 512 fallback.
                if 32 <= ml <= 16384:
                    return ml
            except (ValueError, _json.JSONDecodeError):
                pass
        return 8192 if "bge-m3" in self._model_name.lower() else 512

    def _detect_dimension(self) -> int:
        if self._model_name in _MODEL_DIMS:
            return _MODEL_DIMS[self._model_name]
        # Probe with the live session + tokenizer
        import numpy as np

        enc = self._tokenizer.encode("probe")
        ids = np.array([enc.ids], dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        inputs = {"input_ids": ids, "attention_mask": mask}
        if self._needs_token_type_ids:
            inputs["token_type_ids"] = np.zeros_like(ids)
        outputs = self._session.run(None, inputs)
        return int(outputs[0].shape[-1])

    # public API -------------------------------------------------------

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._load()
        import numpy as np

        # Enable fixed-length padding + truncation inside the tokenizer
        # so `encode_batch` returns arrays we can stack. The `tokenizers`
        # lib handles padding server-side (no numpy padding dance).
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._tokenizer.enable_truncation(max_length=self._max_seq_len)

        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array(
            [e.attention_mask for e in encodings], dtype=np.int64
        )

        inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
        if self._needs_token_type_ids:
            inputs["token_type_ids"] = np.zeros_like(input_ids)

        outputs = self._session.run(None, inputs)
        embeddings = outputs[0]  # (batch, seq, hidden) — last hidden state

        # Mean pool with attention mask, then L2 normalise.
        # Matches sentence-transformers' default for MiniLM, BGE, etc.
        if embeddings.ndim == 3:
            mask_f = attention_mask[..., np.newaxis].astype(embeddings.dtype)
            summed = (embeddings * mask_f).sum(axis=1)
            counts = np.clip(mask_f.sum(axis=1), a_min=1e-9, a_max=None)
            embeddings = summed / counts

        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        embeddings = embeddings / norms
        return embeddings.tolist()

    def dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        if self._model_name in _MODEL_DIMS:
            return _MODEL_DIMS[self._model_name]
        self._load()
        return self._dim  # type: ignore[return-value]

    def info(self) -> dict:
        active = None
        if self._session is not None:
            try:
                providers = self._session.get_providers()
                if providers:
                    active = providers[0]
            except Exception:
                pass
        return {
            "engine": "onnx",
            "model": self._model_name,
            "onnx_repo": self._onnx_repo,
            "provider": active or (self._provider_name or "auto"),
            "dimension": self.dimension(),
            "loaded": self._session is not None,
        }


# ── Local (sentence-transformers, fallback) ──────────────────────

class LocalEmbedder(Embedder):
    """Local embedding via sentence-transformers.

    Model is lazy-loaded on first ``encode()`` / ``dimension()`` call
    and cached for the lifetime of the instance.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model = None
        self._dim: int | None = None

    # lazy init --------------------------------------------------------

    def _load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        log.info("Loading embedding model: %s …", self._model_name)
        self._model = SentenceTransformer(self._model_name)
        self._model.eval()
        self._dim = self._model.get_sentence_embedding_dimension()
        log.info("Model ready (dim=%d)", self._dim)

    # public API -------------------------------------------------------

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._load()
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        if self._model_name in _MODEL_DIMS:
            return _MODEL_DIMS[self._model_name]
        self._load()
        return self._dim  # type: ignore[return-value]

    def info(self) -> dict:
        # Detect whether torch thinks it has a GPU — sentence-transformers
        # picks it up automatically, so reporting this matches what's
        # actually running. Best-effort: the torch import is cheap once
        # the model has loaded.
        device = "cpu"
        try:
            import torch  # noqa: PLC0415

            if torch.cuda.is_available():
                device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"
        except Exception:
            pass
        return {
            "engine": "pytorch",
            "model": self._model_name,
            "device": device,
            "dimension": self.dimension(),
            "loaded": self._model is not None,
        }


# ── Cloud Inference (Qdrant Cloud) ────────────────────────────────

class CloudInferenceEmbedder(Embedder):
    """Delegates embedding to Qdrant Cloud's built-in inference API.

    No local model is loaded — vectors come from the cloud endpoint.
    """

    def __init__(self, url: str, api_key: str, model_name: str = "all-MiniLM-L6-v2"):
        self._url = url
        self._api_key = api_key
        self._model_name = model_name
        self._client = None
        self._dim: int = _MODEL_DIMS.get(model_name, 384)

    def _ensure_client(self):
        if self._client is not None:
            return
        from qdrant_client import QdrantClient

        self._client = QdrantClient(url=self._url, api_key=self._api_key)
        log.info("Cloud inference client ready via %s", self._url)

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._ensure_client()
        from qdrant_client.models import Document

        vectors: list[list[float]] = []
        batch_size = 32
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            docs = [
                Document(text=t, model=self._model_name) for t in batch
            ]
            result = self._client.infer("", docs)
            vectors.extend([list(v) for v in result])
        return vectors

    def dimension(self) -> int:
        return self._dim

    def info(self) -> dict:
        # URL might be sensitive if it encodes the workspace — strip the
        # host-tail so the UI gets a hint without leaking the full
        # endpoint. The api_key is never surfaced.
        from urllib.parse import urlparse

        host = ""
        try:
            host = urlparse(self._url).hostname or ""
        except Exception:
            pass
        return {
            "engine": "qdrant-cloud",
            "model": self._model_name,
            "host": host,
            "dimension": self.dimension(),
            "loaded": self._client is not None,
        }


# ── Factory ────────────────────────────────────────────────────────

def create_embedder(config: LensConfig) -> Embedder:
    """Create the appropriate embedder from a LensConfig.

    Priority:
    1. Cloud inference (if enabled) — no local model needed
    2. ONNX Runtime (if onnx_provider set or optimum available) — GPU-accelerated
    3. sentence-transformers (fallback) — always works
    """
    if config.cloud_inference:
        if not config.qdrant_cloud_url:
            raise ValueError(
                "LENS_QDRANT_CLOUD_URL required when cloud_inference=True"
            )
        return CloudInferenceEmbedder(
            url=config.qdrant_cloud_url,
            api_key=config.qdrant_cloud_key,
            model_name=config.embedding_model,
        )

    # Try ONNX backend if provider is set or optimum is available
    if config.onnx_provider or _onnx_available():
        log.info(
            "Using ONNX backend (provider=%s)",
            config.onnx_provider or "auto",
        )
        return OnnxEmbedder(
            model_name=config.embedding_model,
            provider=config.onnx_provider,
        )

    # Fallback to sentence-transformers
    log.info("ONNX not available, falling back to sentence-transformers")
    return LocalEmbedder(model_name=config.embedding_model)


def _onnx_available() -> bool:
    """ONNX path needs three pure-Python / native deps to be installed:
    onnxruntime (inference), huggingface_hub (download), tokenizers
    (fast tokenizer). If any is missing we transparently fall back to
    sentence-transformers. No more optimum / transformers involvement —
    removed in v0.7 to avoid the PyTorch→ONNX conversion chain
    entirely (see `OnnxEmbedder` docstring)."""
    for mod in ("onnxruntime", "huggingface_hub", "tokenizers"):
        try:
            __import__(mod)
        except ImportError:
            return False
    return True
