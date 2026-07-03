"""Load `.cursor/cursor.local.env` into os.environ (without overriding existing vars)."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_PATH = Path(__file__).with_name("cursor.local.env")


def load_cursor_local_env() -> Path | None:
    if not _ENV_PATH.is_file():
        return None

    for raw_line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

    return _ENV_PATH


load_cursor_local_env()
