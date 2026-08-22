"""FAZ DJ startup helper for Render Secret Files.

Keeps large YouTube cookies out of environment variables so Render builds do
not fail with `argument list too long`.

The main worker already reads YOUTUBE_COOKIES_B64.  We use a tiny sentinel
value and resolve it from /etc/secrets/youtube-cookies.txt only inside the
Python process.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

_SENTINEL = "__FAZ_RENDER_SECRET_FILE__"
_SECRET_PATH = Path(os.getenv("YOUTUBE_COOKIES_FILE", "/etc/secrets/youtube-cookies.txt"))
_original_b64decode = base64.b64decode


def _faz_b64decode(value, *args, **kwargs):
    text = value.decode("utf-8", errors="ignore") if isinstance(value, (bytes, bytearray)) else str(value)
    if text == _SENTINEL:
        return _SECRET_PATH.read_bytes()
    return _original_b64decode(value, *args, **kwargs)


base64.b64decode = _faz_b64decode
