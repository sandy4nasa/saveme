#!/usr/bin/env python3
"""
analyze_video_llm.py

Last-resort place extraction fallback: analyzes the actual video (visuals,
on-screen text, and audio) via Gemini's multimodal video understanding, for
items where even the real caption/description doesn't name a specific place
(enrichment_status == "no_place_in_caption" after
extract_places_llm.process_item(), and -- for YouTube -- after the free
transcript-text fallback in fetch_youtube_transcript.py has also failed to
resolve it).

Pipeline (platform-dependent video-fetch step, everything downstream shared):
  Instagram: fetch_instagram_video.fetch_video_info()  [HikerAPI: get CDN video URL]
             -> download_video()                       [download the MP4]
  YouTube:   fetch_youtube_video.download_video()       [yt-dlp, no auth needed]
  both then:
    -> Gemini File API upload + poll ACTIVE  [make it referenceable]
    -> generateContent (video understanding) [ask for place name/city]
    -> extract_places_llm.enrich_with_places()  [same Google Places lookup]

This is opt-in and fully best-effort: for Instagram, if HIKER_API_KEY is not
configured, or the video can't be fetched/downloaded/uploaded/analyzed for
any reason, analyze_video() returns None and the caller should keep whatever
caption-based result it already had rather than fail the whole ingestion.
YouTube needs no paid API key at all -- yt-dlp downloads public videos
directly -- but is equally best-effort (private/age-restricted videos,
network errors, or a future YouTube anti-bot change all degrade to None the
same way).

Cost note: each Instagram attempt costs 1 HikerAPI request
(~$0.0006-$0.02/request depending on plan tier) plus a Gemini
video-understanding call; each YouTube attempt costs only the Gemini call
(no paid video-fetch API). Only ever triggered for items where caption-based
extraction already came up empty -- never run on every share (see
ingest_pipeline._run_enrichment).

See IMPLEMENTATION_PLAN.md Section 20 (Instagram) and Section 23 (YouTube)
for the full design/tradeoffs.
"""

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_instagram_video import fetch_video_info, download_video  # noqa: E402
from fetch_youtube_video import download_video as download_youtube_video  # noqa: E402
from extract_places_llm import enrich_with_places  # noqa: E402

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com"
UPLOAD_POLL_ATTEMPTS = 20
UPLOAD_POLL_DELAY_SECONDS = 3
CONFIDENCE_THRESHOLD = 0.5

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_place_mentioned": {
            "type": "BOOLEAN",
            "description": "True if the video shows/mentions a specific real-world venue or property "
                           "(restaurant, shop, park, hotel, landmark, house/land listing, etc.), false otherwise.",
        },
        "place_name": {
            "type": "STRING",
            "description": "The specific venue/property name or description, as best identified from the video. Empty string if none.",
        },
        "city_or_area": {
            "type": "STRING",
            "description": "City, neighborhood, or area shown/mentioned that helps disambiguate the place. Empty string if not identifiable.",
        },
        "confidence": {
            "type": "NUMBER",
            "description": "0.0-1.0 confidence that place_name is a real, findable location.",
        },
    },
    "required": ["is_place_mentioned", "place_name", "city_or_area", "confidence"],
}

PROMPT = """You are extracting a real-world venue/property name and location from a saved social media Reel video for a "saved places" app.

Watch the video's visuals (signboards, on-screen text, location tags) and listen to the audio/narration. Identify the specific place/venue/property this video is about (restaurant, cafe, shop, hotel, park, landmark, house or land listing, etc.), and the city or area it's in if shown or mentioned.

If nothing in the video identifies a specific real-world place, set is_place_mentioned to false."""


def _upload_to_gemini(video_path, gemini_key):
    """Uploads a local video file to Gemini's resumable File API and polls
    until it's ACTIVE (ready to reference in generateContent). Raises on
    any failure -- caller (analyze_video) wraps this in a try/except."""
    size = os.path.getsize(video_path)
    start_req = urllib.request.Request(
        f"{GEMINI_API_BASE}/upload/v1beta/files?key={gemini_key}",
        data=json.dumps({"file": {"display_name": "saveme_reel"}}).encode("utf-8"),
        method="POST",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": "video/mp4",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(start_req, timeout=30) as resp:
        upload_url = resp.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise RuntimeError("Gemini upload session did not return an upload URL")

    with open(video_path, "rb") as f:
        data = f.read()
    upload_req = urllib.request.Request(
        upload_url,
        data=data,
        method="POST",
        headers={
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
    )
    with urllib.request.urlopen(upload_req, timeout=120) as resp:
        file_info = json.loads(resp.read().decode("utf-8"))["file"]

    file_name = file_info["name"]
    status_url = f"{GEMINI_API_BASE}/v1beta/{file_name}?key={gemini_key}"
    for _ in range(UPLOAD_POLL_ATTEMPTS):
        with urllib.request.urlopen(status_url, timeout=30) as resp:
            file_info = json.loads(resp.read().decode("utf-8"))
        state = file_info.get("state")
        if state == "ACTIVE":
            return file_info["uri"], file_info["mimeType"]
        if state == "FAILED":
            raise RuntimeError("Gemini file processing failed")
        time.sleep(UPLOAD_POLL_DELAY_SECONDS)
    raise RuntimeError("Gemini file did not become ACTIVE in time")


def _call_gemini_video(file_uri, mime_type, gemini_key):
    url = f"{GEMINI_API_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent?key={gemini_key}"
    body = {
        "contents": [{"parts": [
            {"file_data": {"mime_type": mime_type, "file_uri": file_uri}},
            {"text": PROMPT},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.1,
        },
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    text = raw["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def analyze_video(item, gemini_key, places_key, hiker_api_key=None):
    """Best-effort last-resort fallback. Returns an enriched item dict
    (same shape as extract_places_llm.process_item()) on success, or None
    if the video couldn't be fetched/analyzed for any reason -- the caller
    should keep whatever caption-based result it already had in that case.

    Dispatches by platform: Instagram videos require the paid HikerAPI
    (fetch_instagram_video.py -- yt-dlp fails outright without an
    authenticated Instagram session). YouTube videos download directly via
    yt-dlp (fetch_youtube_video.py) with no paid API and no auth wall.
    """
    source_url = item.get("source_url") or ""
    platform = item.get("platform") or ("instagram" if "instagram.com" in source_url else "youtube" if ("youtube.com" in source_url or "youtu.be" in source_url) else None)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        if platform == "instagram":
            hiker_api_key = hiker_api_key or os.environ.get("HIKER_API_KEY")
            if not hiker_api_key:
                return None
            video_info = fetch_video_info(source_url, hiker_api_key)
            if not video_info or not download_video(video_info["video_url"], tmp_path):
                return None
        elif platform == "youtube":
            if not download_youtube_video(source_url, tmp_path):
                return None
        else:
            return None

        file_uri, mime_type = _upload_to_gemini(tmp_path, gemini_key)
        extraction = _call_gemini_video(file_uri, mime_type, gemini_key)
    except Exception as e:
        print(f"[analyze_video_llm] video analysis failed for {source_url}: {e}", file=sys.stderr)
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    item = {
        **item,
        "video_llm_is_place": extraction.get("is_place_mentioned"),
        "video_llm_place_name": extraction.get("place_name"),
        "video_llm_city_or_area": extraction.get("city_or_area"),
        "video_llm_confidence": extraction.get("confidence"),
    }

    if not extraction.get("is_place_mentioned") or not extraction.get("place_name"):
        return {**item, "enrichment_status": "no_place_in_caption"}

    if extraction.get("confidence", 0) < CONFIDENCE_THRESHOLD:
        return {**item, "enrichment_status": "needs_review_low_confidence"}

    query = f"{extraction['place_name']}, {extraction['city_or_area']}".strip(", ")
    enriched = enrich_with_places(item, query, places_key)
    enriched["enrichment_query_source"] = "video_llm"
    return enriched


if __name__ == "__main__":
    from enrich_places import load_dotenv

    if len(sys.argv) != 2:
        print("Usage: python3 scripts/analyze_video_llm.py <instagram_post_url>")
        sys.exit(1)

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    gemini_key = os.environ.get("GOOGLE_AI_API_KEY")
    places_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not gemini_key or not places_key:
        print("Error: GOOGLE_AI_API_KEY / GOOGLE_PLACES_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    result = analyze_video({"source_url": sys.argv[1], "raw_caption": "", "hashtags": []}, gemini_key, places_key)
    print(json.dumps(result, indent=2, ensure_ascii=False) if result else "No result (see stderr for details).")
