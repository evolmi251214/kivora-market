"""FAZ DJ startup helper for Render Secret Files.

Keeps large YouTube cookies out of environment variables so Render builds do
not fail with `argument list too long`.

If /etc/secrets/youtube-cookies.txt exists, the worker transparently treats it
as YOUTUBE_COOKIES_B64 input. This means no large environment variable and no
manual sentinel value are required.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

_SENTINEL = "__FAZ_RENDER_SECRET_FILE__"
_SECRET_PATH = Path(os.getenv("YOUTUBE_COOKIES_FILE", "/etc/secrets/youtube-cookies.txt"))
_original_b64decode = base64.b64decode
_original_getenv = os.getenv


def _faz_getenv(key, default=None):
    if key == "YOUTUBE_COOKIES_B64" and _SECRET_PATH.is_file():
        return _SENTINEL
    return _original_getenv(key, default)


def _faz_b64decode(value, *args, **kwargs):
    text = value.decode("utf-8", errors="ignore") if isinstance(value, (bytes, bytearray)) else str(value)
    if text == _SENTINEL:
        return _SECRET_PATH.read_bytes()
    return _original_b64decode(value, *args, **kwargs)


os.getenv = _faz_getenv
base64.b64decode = _faz_b64decode

try:
    if _SECRET_PATH.is_file():
        print("FAZ DJ: Render YouTube secret file detected")
    else:
        print("FAZ DJ: Render YouTube secret file not found")
except Exception:
    pass
