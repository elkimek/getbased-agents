"""Shell-style env file I/O for ~/.config/getbased/env.

The same file format the opt-in loaders in mcp/rag/dashboard read. Kept
intentionally tolerant — duplicate keys take last-wins, blanks and `#`
comments survive round-trips, unknown junk is preserved (we only rewrite
keys we explicitly care about). This lets a user hand-edit the file
without `getbased-stack init` clobbering their comments.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


ENV_PATH_COMMENT = """\
# getbased shared env — read by mcp, rag, and dashboard when
# GETBASED_STACK_MANAGED=1. Written by `getbased-stack init` and
# `getbased-stack set`. Mode 0600.
#
# Lines are KEY=VALUE. Comments start with `#`. Quoted values are unwrapped.
# Explicit env vars always win — this file only provides defaults.
"""


def env_file_path() -> Path:
    """Return the XDG-correct shared env file path. Honors $XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "getbased" / "env"


def parse_env_text(text: str) -> "dict[str, str]":
    """Parse shell-style KEY=VALUE text. Last occurrence of a key wins.
    Intentionally permissive — malformed lines are silently skipped."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def read_env_file(path: Path | None = None) -> "dict[str, str]":
    """Read the shared env file, returning {} on missing file."""
    p = path or env_file_path()
    try:
        return parse_env_text(p.read_text(encoding="utf-8"))
    except OSError:
        return {}


def write_env_file(
    values: "dict[str, str]",
    path: Path | None = None,
    header: str = ENV_PATH_COMMENT,
) -> Path:
    """Write values to the shared env file at mode 0600.

    Creates parent dir if missing. Overwrites — call read_env_file first +
    merge if you want to preserve existing keys. Values are never quoted;
    reader handles quoting-or-not symmetrically.

    Caveat: systemd's `EnvironmentFile=` parser has stricter quoting rules
    than our Python loader — values containing whitespace or shell
    metacharacters should be avoided. Our default values (tokens,
    URL-safe paths) are already safe; users passing arbitrary values via
    `getbased-stack set FOO="has spaces"` may see systemd-loaded services
    and directly-invoked CLIs parse the value differently.
    """
    p = path or env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [header.rstrip() + "\n\n"] if header else []
    for k, v in values.items():
        # Only alphanumeric + underscore are safe as env keys; anything else
        # is a programming bug upstream, fail loud.
        if not k or not all(c.isalnum() or c == "_" for c in k):
            raise ValueError(f"invalid env key: {k!r}")
        lines.append(f"{k}={v}\n")
    p.write_text("".join(lines), encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def set_env_var(key: str, value: str, path: Path | None = None) -> Path:
    """Idempotent single-key upsert. Preserves other keys."""
    current = read_env_file(path)
    current[key] = value
    return write_env_file(current, path)


def unset_env_var(key: str, path: Path | None = None) -> Path:
    """Idempotent single-key remove. No-op if absent."""
    current = read_env_file(path)
    current.pop(key, None)
    return write_env_file(current, path)
