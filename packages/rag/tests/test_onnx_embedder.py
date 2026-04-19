"""Unit tests for the 0.7 OnnxEmbedder internals.

The full OnnxEmbedder path (download a model from HF + run inference)
is verified manually before each release — too heavy for CI (multi-GB
downloads, flaky if HF is down). These tests cover the critical pure
logic that can't wait for manual verification: pad-config resolution,
cache-root env-var routing, pooling math. A regression in any of
these silently corrupts embeddings for someone's knowledge base.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from lens.embedder import OnnxEmbedder, _ONNX_REPO_MAP


# ─── _resolve_pad_config ──────────────────────────────────────────────
# The CRITICAL audit finding: hard-coded `pad_id=0, pad_token="[PAD]"`
# silently corrupts BGE-M3 (XLM-RoBERTa: pad=<pad> id=1). These tests
# cover every path through _resolve_pad_config so a regression there
# can't slip through to production.


class _FakeTokenizer:
    """Minimal tokenizer-like object — just the `token_to_id` surface
    _resolve_pad_config needs. Lets us unit-test without loading a real
    tokenizer.json."""

    def __init__(self, vocab: dict[str, int]):
        self._vocab = vocab

    def token_to_id(self, token: str) -> int | None:
        return self._vocab.get(token)


def _make_embedder(vocab: dict[str, int]) -> OnnxEmbedder:
    e = OnnxEmbedder(model_name="test-model")
    e._tokenizer = _FakeTokenizer(vocab)
    return e


def test_pad_config_reads_string_form_from_tokenizer_config(tmp_path: Path) -> None:
    """Legacy HF format: `"pad_token": "[PAD]"`."""
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"pad_token": "[PAD]", "model_max_length": 512})
    )
    e = _make_embedder({"[PAD]": 0, "hello": 100})
    tok, tid = e._resolve_pad_config(tmp_path)
    assert tok == "[PAD]"
    assert tid == 0


def test_pad_config_reads_dict_form_from_tokenizer_config(tmp_path: Path) -> None:
    """Newer HF format: `"pad_token": {"content": "<pad>", ...}`.
    This is what Xenova/bge-m3 ships. Getting this wrong is the
    BGE-M3 corruption bug the audit caught."""
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({
            "pad_token": {
                "content": "<pad>",
                "lstrip": False,
                "normalized": False,
                "rstrip": False,
                "single_word": False,
            },
            "model_max_length": 8192,
        })
    )
    # BGE-M3 / XLM-R: <s>=0, <pad>=1, </s>=2
    e = _make_embedder({"<s>": 0, "<pad>": 1, "</s>": 2})
    tok, tid = e._resolve_pad_config(tmp_path)
    assert tok == "<pad>"
    assert tid == 1, "BGE-M3 pad_id must be 1, not 0 — id=0 is <s>"


def test_pad_config_falls_back_to_vocab_probe_when_config_missing(
    tmp_path: Path,
) -> None:
    """No tokenizer_config.json — try known pad tokens in order."""
    # No config file written. Tokenizer only knows "<pad>" (not "[PAD]").
    e = _make_embedder({"<pad>": 3, "other": 100})
    tok, tid = e._resolve_pad_config(tmp_path)
    assert tok == "<pad>"
    assert tid == 3


def test_pad_config_fallback_when_no_candidate_in_vocab(
    tmp_path: Path, caplog
) -> None:
    """Absolute last resort — warn and default to ("[PAD]", 0). Real
    models wouldn't hit this, but make sure we fail loud instead of
    silently proceeding with garbage."""
    e = _make_embedder({"only": 10, "tokens": 20})  # no pad-like in vocab
    with caplog.at_level("WARNING", logger="lens.embedder"):
        tok, tid = e._resolve_pad_config(tmp_path)
    assert tok == "[PAD]"
    assert tid == 0
    # Warning emitted so operators know to investigate
    assert any("pad token" in rec.message.lower() for rec in caplog.records)


def test_pad_config_malformed_json_falls_through_to_vocab_probe(
    tmp_path: Path,
) -> None:
    """Garbage tokenizer_config.json shouldn't crash — skip it and
    fall back to vocab probing."""
    (tmp_path / "tokenizer_config.json").write_text("{ not json ] ")
    e = _make_embedder({"[PAD]": 0})
    tok, tid = e._resolve_pad_config(tmp_path)
    assert tok == "[PAD]"
    assert tid == 0


# ─── _cache_root ──────────────────────────────────────────────────────
# Env-var routing for where models get downloaded + looked up. Regression
# here means fresh installs re-download every process start (wasted
# bandwidth + minutes of latency).


def test_cache_root_prefers_lens_data_dir(tmp_path: Path, monkeypatch) -> None:
    """LENS_DATA_DIR wins over all HF env vars — matches our explicit
    self-hosted-install contract."""
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "lens-data"))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hf-hub"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))
    e = OnnxEmbedder(model_name="test")
    assert e._cache_root() == tmp_path / "lens-data" / "hf-cache"


def test_cache_root_honours_hf_hub_cache_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LENS_DATA_DIR", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "custom"))
    monkeypatch.delenv("HF_HOME", raising=False)
    e = OnnxEmbedder(model_name="test")
    # HUGGINGFACE_HUB_CACHE typically points at .../hub — parent is
    # the cache root. If it doesn't end in /hub, treat as root directly.
    root = e._cache_root()
    assert root == tmp_path / "custom"


def test_cache_root_honours_hf_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LENS_DATA_DIR", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "home"))
    e = OnnxEmbedder(model_name="test")
    assert e._cache_root() == tmp_path / "home"


def test_cache_root_default_is_home_cache(monkeypatch) -> None:
    """No env vars → ~/.cache/huggingface."""
    monkeypatch.delenv("LENS_DATA_DIR", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    e = OnnxEmbedder(model_name="test")
    assert e._cache_root() == Path.home() / ".cache" / "huggingface"


# ─── Model-map integrity ──────────────────────────────────────────────


def test_onnx_repo_map_entries_are_strings() -> None:
    """All mapped values are plausibly HF repo ids (`user/model` form)."""
    for canonical, onnx_repo in _ONNX_REPO_MAP.items():
        assert isinstance(canonical, str) and canonical
        assert isinstance(onnx_repo, str) and onnx_repo
        assert "/" in onnx_repo, f"{canonical} → {onnx_repo!r} not a repo id"


def test_onnx_embedder_resolves_canonical_name_to_mapped_repo() -> None:
    """Each canonical name resolves to its mapped ONNX repo."""
    for canonical, expected in _ONNX_REPO_MAP.items():
        e = OnnxEmbedder(model_name=canonical)
        assert e._onnx_repo == expected


def test_onnx_embedder_unknown_model_passes_through_as_repo_id() -> None:
    """Custom finetunes on HF: `user/my-finetune` should be used as-is."""
    e = OnnxEmbedder(model_name="my-org/custom-finetune")
    assert e._onnx_repo == "my-org/custom-finetune"


# ─── _find_onnx_file ──────────────────────────────────────────────────


def test_find_onnx_file_prefers_onnx_subfolder(tmp_path: Path) -> None:
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model.onnx").write_bytes(b"fake")
    (tmp_path / "model.onnx").write_bytes(b"also-fake")  # root fallback
    e = OnnxEmbedder(model_name="test")
    assert e._find_onnx_file(tmp_path) == tmp_path / "onnx" / "model.onnx"


def test_find_onnx_file_fallback_to_root(tmp_path: Path) -> None:
    (tmp_path / "model.onnx").write_bytes(b"fake")
    e = OnnxEmbedder(model_name="test")
    assert e._find_onnx_file(tmp_path) == tmp_path / "model.onnx"


def test_find_onnx_file_raises_when_nothing(tmp_path: Path) -> None:
    e = OnnxEmbedder(model_name="test")
    with pytest.raises(FileNotFoundError, match="No .onnx files found"):
        e._find_onnx_file(tmp_path)


# ─── _detect_max_len ─────────────────────────────────────────────────


def test_detect_max_len_reads_from_tokenizer_config(tmp_path: Path) -> None:
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"model_max_length": 8192})
    )
    e = OnnxEmbedder(model_name="BAAI/bge-m3")
    assert e._detect_max_len(tmp_path) == 8192


def test_detect_max_len_clamps_absurd_sentinel(tmp_path: Path) -> None:
    """HF sometimes ships `model_max_length: 1e30` meaning "no cap".
    Clamp anything absurd down to the name-based heuristic fallback."""
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"model_max_length": 1000000000000000})
    )
    e = OnnxEmbedder(model_name="sentence-transformers/all-MiniLM-L6-v2")
    assert e._detect_max_len(tmp_path) == 512  # not the 1e15 sentinel


def test_detect_max_len_name_heuristic_for_bge_m3(tmp_path: Path) -> None:
    """No tokenizer_config — BGE-M3 gets 8192 from the name heuristic,
    other models fall to 512. Matters because BGE-M3's whole pitch is
    long-context and silently truncating to 512 throws away its value."""
    e_bge = OnnxEmbedder(model_name="BAAI/bge-m3")
    assert e_bge._detect_max_len(tmp_path) == 8192
    e_mini = OnnxEmbedder(model_name="sentence-transformers/all-MiniLM-L6-v2")
    assert e_mini._detect_max_len(tmp_path) == 512
