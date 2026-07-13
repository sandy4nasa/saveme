#!/usr/bin/env python3
"""
fetch_youtube_metadata.py

Fetches the real title + description for a shared YouTube video/Shorts URL,
mirroring what fetch_instagram_caption.py does for Instagram -- so the same
downstream extraction pipeline (extract_places_llm.process_item()) can run
on YouTube saves without any changes.

Unlike Instagram (which requires unofficial og:description scraping because
there's no way to get an arbitrary user's public post via the official API),
YouTube offers this directly: the official YouTube Data API v3 `videos.list`
endpoint returns title/description/channel for any public video with just an
API key -- no OAuth, no per-user consent, no scraping fragility. Uses the
same Google Cloud API key as GOOGLE_PLACES_API_KEY (just needs "YouTube Data
API v3" enabled on the same project).

Video description text becomes the "caption" fed into process_item() --
title is prepended since travel/food channels often put the place name in
the title but not the body of the description.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3/videos"
REQUEST_TIMEOUT_SECONDS = 10

# Matches youtube.com/watch?v=ID, youtu.be/ID, youtube.com/shorts/ID,
# youtube.com/embed/ID, m.youtube.com/watch?v=ID -- the handful of URL shapes
# Android's share sheet / browser address bar can produce.
VIDEO_ID_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?[^#]*\bv=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)([\w-]{11})"),
]


def extract_video_id(source_url: str):
    """Returns the 11-character YouTube video ID from any common URL shape,
    or None if source_url isn't a recognizable YouTube URL."""
    if not source_url or ("youtube.com" not in source_url and "youtu.be" not in source_url):
        return None
    for pattern in VIDEO_ID_PATTERNS:
        match = pattern.search(source_url)
        if match:
            return match.group(1)
    return None


def fetch_metadata(source_url: str, api_key: str, timeout: float = REQUEST_TIMEOUT_SECONDS):
    """Best-effort fetch of title/description/channel for a public YouTube
    video URL. Returns {"caption": str, "owner_username": str|None,
    "title": str, "video_id": str} on success, or None if unavailable
    (private/deleted video, invalid URL, API error, quota exceeded, etc.) --
    callers must handle None gracefully, same contract as
    fetch_instagram_caption.fetch_caption()."""
    video_id = extract_video_id(source_url)
    if not video_id:
        return None

    params = urllib.parse.urlencode({"part": "snippet", "id": video_id, "key": api_key})
    url = f"{YOUTUBE_API_BASE}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    except Exception:
        return None

    items = data.get("items") or []
    if not items:
        return None  # private, deleted, or age-restricted video

    snippet = items[0].get("snippet", {})
    title = (snippet.get("title") or "").strip()
    description = (snippet.get("description") or "").strip()
    channel_title = snippet.get("channelTitle")

    caption = f"{title}\n\n{description}".strip() if description else title
    if not caption:
        return None

    return {"caption": caption, "owner_username": channel_title, "title": title, "video_id": video_id}


if __name__ == "__main__":
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from enrich_places import load_dotenv  # noqa: E402

    if len(sys.argv) != 2:
        print("Usage: python3 scripts/fetch_youtube_metadata.py <youtube_video_url>")
        sys.exit(1)

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        print("Error: GOOGLE_PLACES_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    result = fetch_metadata(sys.argv[1], key)
    if result is None:
        print("No metadata found (private/deleted video, invalid URL, or API error).")
    else:
        print(f"video_id: {result['video_id']}")
        print(f"owner_username (channel): {result['owner_username']}")
        print(f"caption:\n{result['caption']}")
