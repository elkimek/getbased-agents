"""Unit tests for env_file.py — the KEY=VALUE parser + writer.

Kept strict: the loaders in mcp/rag/dashboard consume the exact format
written here, so any round-trip mismatch silently breaks downstream.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from getbased_agent_stack import env_file


@pytest.fixture
def tmp_env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "getbased" / "env"


def test_env_file_path_honors_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert env_file.env_file_path() == tmp_path / "getbased" / "env"


def test_parse_basic():
    out = env_file.parse_env_text("A=1\nB=2\n")
    assert out == {"A": "1", "B": "2"}


def test_parse_strips_quotes():
    out = env_file.parse_env_text('A="x"\nB=\'y\'\nC=z\n')
    assert out == {"A": "x", "B": "y", "C": "z"}


def test_parse_skips_comments_and_blanks():
    out = env_file.parse_env_text("# top\n\nA=1\n  # indented comment\nB=2\n")
    assert out == {"A": "1", "B": "2"}


def test_parse_skips_malformed():
    out = env_file.parse_env_text("NOEQUALS\n=missing_key\nA=1\n")
    assert out == {"A": "1"}


def test_parse_last_wins():
    out = env_file.parse_env_text("A=1\nA=2\nA=3\n")
    assert out == {"A": "3"}


def test_write_creates_parent_dir(tmp_env_path):
    env_file.write_env_file({"FOO": "bar"}, path=tmp_env_path)
    assert tmp_env_path.exists()
    assert tmp_env_path.parent.is_dir()


def test_write_sets_mode_0600(tmp_env_path):
    env_file.write_env_file({"FOO": "bar"}, path=tmp_env_path)
    mode = stat.S_IMODE(os.stat(tmp_env_path).st_mode)
    assert mode == 0o600


def test_write_rejects_invalid_keys(tmp_env_path):
    with pytest.raises(ValueError, match="invalid env key"):
        env_file.write_env_file({"has space": "x"}, path=tmp_env_path)
    with pytest.raises(ValueError, match="invalid env key"):
        env_file.write_env_file({"": "x"}, path=tmp_env_path)


def test_roundtrip(tmp_env_path):
    data = {"A": "1", "B": "has spaces", "C": "path/with/slashes"}
    env_file.write_env_file(data, path=tmp_env_path)
    assert env_file.read_env_file(tmp_env_path) == data


def test_read_missing_file_returns_empty(tmp_path):
    assert env_file.read_env_file(tmp_path / "nonexistent") == {}


def test_set_env_var_preserves_others(tmp_env_path):
    env_file.write_env_file({"A": "1", "B": "2"}, path=tmp_env_path)
    env_file.set_env_var("B", "new", path=tmp_env_path)
    assert env_file.read_env_file(tmp_env_path) == {"A": "1", "B": "new"}


def test_set_env_var_adds_new(tmp_env_path):
    env_file.write_env_file({"A": "1"}, path=tmp_env_path)
    env_file.set_env_var("B", "2", path=tmp_env_path)
    assert env_file.read_env_file(tmp_env_path) == {"A": "1", "B": "2"}


def test_unset_env_var_idempotent(tmp_env_path):
    env_file.write_env_file({"A": "1"}, path=tmp_env_path)
    env_file.unset_env_var("DOES_NOT_EXIST", path=tmp_env_path)  # no-op
    env_file.unset_env_var("A", path=tmp_env_path)
    assert env_file.read_env_file(tmp_env_path) == {}


def test_values_with_equals_signs(tmp_env_path):
    """URLs and bearer values often contain `=`. partition(1) must not split
    them."""
    env_file.write_env_file({"URL": "https://example.com?key=v&x=y"}, path=tmp_env_path)
    out = env_file.read_env_file(tmp_env_path)
    assert out["URL"] == "https://example.com?key=v&x=y"
