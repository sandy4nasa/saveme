#!/usr/bin/env python3
"""
fetch_instagram_video.py

Best-effort video download for a public Instagram post/reel, used as a
LAST-RESORT fallback (see analyze_video_llm.py) for the rare case where
even the real caption (fetch_instagram_caption.py) doesn't name a specific
place -- e.g. the place is only shown on a signboard or spoken aloud in
the video itself.

Why this needs a third-party API instead of scraping directly: Instagram's
public post pages do NOT expose a direct video URL the way they expose the
caption via `og:description`. This was confirmed via 5 independent tests:
no `og:video` meta tag / embedded src in the page HTML, `yt-dlp` fails
without cookies ("Instagram sent an empty media response"), the `/embed/`
page confirms the media type but strips the actual video src, the legacy
`?__a=1` JSON endpoint returns HTTP 500, and the private mobile API
endpoint redirects to a login page. Actual video bytes are only reachable
with an authenticated Instagram session -- either our own (risky: ToS
violation / account ban exposure) or a third party's.

We use HikerAPI (https://hikerapi.com), a paid Instagram data API that
maintains its own authenticated scraping infrastructure so SaveMe never
stores a real Instagram session/cookies. Requires HIKER_API_KEY in the
environment/.env; if unset, fetch_video_info() returns None so the whole
video-analysis fallback is skipped without affecting caption-only
ingestion (see IMPLEMENTATION_PLAN.md Section 20).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

HIKER_BASE_URL = "https://api.hikerapi.com/v1/media/by/url"
REQUEST_TIMEOUT_SECONDS = 20
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_VIDEO_BYTES = 60 * 1024 * 1024  # 60MB safety cap -- Reels are almost always well under this

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def fetch_video_info(source_url, hiker_api_key=None, timeout=REQUEST_TIMEOUT_SECONDS):
    """Best-effort lookup of a public Instagram post's video URL (+ caption,
    as a bonus) via HikerAPI. Returns
    {"video_url": str, "caption_text": str|None, "video_duration": float|None,
    "owner_username": str|None} on success, or None on any failure (no API
    key configured, private post, image-only post/carousel, network error,
    rate limit, etc.) -- callers must handle None gracefully."""
    hiker_api_key = hiker_api_key or os.environ.get("HIKER_API_KEY")
    if not hiker_api_key or not source_url or "instagram.com" not in source_url:
        return None

    url = f"{HIKER_BASE_URL}?url={urllib.parse.quote(source_url, safe='')}"
    req = urllib.request.Request(
        url,
        headers={"x-access-key": hiker_api_key, "accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    except Exception:
        return None

    video_url = data.get("video_url")
    if not video_url:
        return None  # image-only post, carousel without video, or private/unavailable

    return {
        "video_url": video_url,
        "caption_text": data.get("caption_text"),
        "video_duration": data.get("video_duration"),
        "owner_username": (data.get("user") or {}).get("username"),
    }


def download_video(video_url, dest_path, timeout=DOWNLOAD_TIMEOUT_SECONDS, max_bytes=MAX_VIDEO_BYTES):
    """Downloads the CDN video URL to dest_path. Returns True on success,
    False on any failure (oversized, network error, etc.) -- best-effort,
    never raises."""
    try:
        req = urllib.request.Request(video_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                return False
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python3 scripts/fetch_instagram_video.py <instagram_post_url>")
        sys.exit(1)

    info = fetch_video_info(sys.argv[1])
    if info is None:
        print("No video found (no HIKER_API_KEY, private post, image-only post, or network error).")
    else:
        print(json.dumps(info, indent=2))
