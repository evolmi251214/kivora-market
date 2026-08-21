"""FAZ DJ runtime patches.

This module is imported automatically by Python at startup.  It adds a
best-effort yt-dlp fallback chain for public YouTube URLs when the normal
web client is rejected with the common datacenter/bot-check response.

No cookies, passwords or account secrets are stored here.
"""

from __future__ import annotations

import copy
from typing import Any

try:
    import yt_dlp
    from yt_dlp.utils import DownloadError
except Exception:  # pragma: no cover - do not stop the worker booting
    yt_dlp = None
    DownloadError = Exception


if yt_dlp is not None:
    _original_extract_info = yt_dlp.YoutubeDL.extract_info

    def _looks_like_youtube(value: Any) -> bool:
        text = str(value or "").lower()
        return "youtube.com/" in text or "youtu.be/" in text

    def _is_bot_or_client_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        markers = (
            "sign in to confirm you're not a bot",
            "sign in to confirm you’re not a bot",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "cookies-from-browser",
            "use --cookies",
            "login required",
            "this helps protect our community",
            "player response",
            "no supported javascript runtime",
        )
        return any(marker in text for marker in markers)

    def _merged_params(base: dict, client: str) -> dict:
        params = copy.deepcopy(base)
        extractor_args = copy.deepcopy(params.get("extractor_args") or {})
        youtube_args = copy.deepcopy(extractor_args.get("youtube") or {})
        youtube_args["player_client"] = [client]
        extractor_args["youtube"] = youtube_args
        params["extractor_args"] = extractor_args
        # Keep retries short because the outer FAZ worker already has its own job timeout.
        params["retries"] = min(int(params.get("retries") or 2), 2)
        params["fragment_retries"] = min(int(params.get("fragment_retries") or 2), 2)
        return params

    def _faz_extract_info(self, url, download=True, *args, **kwargs):
        try:
            return _original_extract_info(self, url, download, *args, **kwargs)
        except DownloadError as first_error:
            if not _looks_like_youtube(url) or not _is_bot_or_client_error(first_error):
                raise

            # Ordered from least invasive public/embed client to alternate app clients.
            # These are best-effort fallbacks only; if YouTube requires account cookies
            # or a PO token for a particular video/IP, the final error is returned.
            fallbacks = ("web_embedded", "android_vr", "ios")
            last_error = first_error

            for client in fallbacks:
                try:
                    retry_params = _merged_params(self.params, client)
                    with yt_dlp.YoutubeDL(retry_params) as retry_ydl:
                        # Call the original method directly so this fallback does not recurse.
                        return _original_extract_info(
                            retry_ydl, url, download, *args, **kwargs
                        )
                except DownloadError as retry_error:
                    last_error = retry_error
                    continue

            raise last_error

    yt_dlp.YoutubeDL.extract_info = _faz_extract_info
