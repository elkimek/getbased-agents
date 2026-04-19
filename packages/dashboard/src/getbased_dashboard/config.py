"""Dashboard configuration, loaded from environment variables at startup.

One source of truth — tests override by monkeypatching env + constructing
a fresh DashboardConfig. The auth key is resolved using the same
"new-default with legacy fallback" logic as getbased-mcp so upgraders
don't have to reconfigure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _maybe_load_user_env() -> None:
    """Opt-in: load $XDG_CONFIG_HOME/getbased/env into os.environ.

    Guarded by GETBASED_STACK_MANAGED=1. setdefault semantics — explicit env
    wins. Silent on missing file; malformed lines skipped.
    Escape hatch: GETBASED_NO_ENV_FILE=1 disables even when managed.
    """
    if os.environ.get("GETBASED_STACK_MANAGED") != "1":
        return
    if os.environ.get("GETBASED_NO_ENV_FILE") == "1":
        return
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(xdg, "getbased", "env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


def _default_activity_log() -> Path:
    state = os.environ.get(
        "XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")
    )
    return Path(state) / "getbased" / "mcp" / "activity.jsonl"


def _resolve_key_file() -> Path:
    """Prefer getbased-rag's XDG location; fall back to the legacy Hermes
    path so upgrading from a standalone getbased-mcp ≤ 0.1.0 install keeps
    working without re-configuring LENS_API_KEY_FILE."""
    xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    new_default = Path(xdg) / "getbased" / "lens" / "api_key"
    legacy = Path(os.path.expanduser("~/.hermes/rag/lens_api_key"))
    if not new_default.exists() and legacy.exists():
        return legacy
    return new_default


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8323
    lens_url: str = "http://127.0.0.1:8322"
    api_key_file: Path = field(default_factory=_resolve_key_file)
    activity_log: Path = field(default_factory=_default_activity_log)

    @classmethod
    def from_env(cls) -> "DashboardConfig":
        _maybe_load_user_env()
        return cls(
            host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
            port=int(os.environ.get("DASHBOARD_PORT", "8323")),
            lens_url=os.environ.get("LENS_URL", "http://127.0.0.1:8322"),
            api_key_file=Path(
                os.environ.get("LENS_API_KEY_FILE", str(_resolve_key_file()))
            ),
            activity_log=Path(
                os.environ.get("DASHBOARD_ACTIVITY_LOG", str(_default_activity_log()))
            ),
        )

    def read_api_key(self) -> str:
        """Returns the shared bearer key, or empty string if the file
        doesn't exist. Dashboard validates incoming browser requests
        against this same key — single token across rag + mcp + dashboard."""
        try:
            return self.api_key_file.read_text().strip()
        except OSError:
            return ""
