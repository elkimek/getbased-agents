"""API key generation + persistence for the Lens HTTP server.

Auto-generates a key on first start if one doesn't exist. The desktop wrapper
(Tauri) reads this file via `getbased_lens_config` MCP tool to display the
key for the user to paste into the getbased web app's Custom Knowledge Source.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def get_or_create_api_key(key_file: Path) -> str:
    """Read the API key from disk; generate + write one if missing.

    Creates the file with O_EXCL + mode 0o600 in one syscall, so the key
    is never briefly present with loose permissions (the race the old
    write_text → chmod sequence had).
    """
    if key_file.exists():
        try:
            key = key_file.read_text().strip()
            if key:
                return key
        except OSError:
            pass

    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(key_file), flags, 0o600)
    except FileExistsError:
        # Another process beat us to it — trust whatever they wrote rather
        # than clobbering it with a fresh key.
        existing = key_file.read_text().strip()
        if existing:
            return existing
        raise
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    return key


def load_api_key(key_file: Path) -> str | None:
    """Read existing API key without generating one."""
    try:
        if key_file.exists():
            key = key_file.read_text().strip()
            return key if key else None
    except OSError:
        pass
    return None
