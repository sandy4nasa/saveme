#!/usr/bin/env python3
"""
fetch_youtube_transcript.py

Fetches the free transcript/auto-caption text for a public YouTube video --
a second, cheaper fallback tier (used in ingest_pipeline._run_enrichment())
for items where the video's title+description doesn't name a specific place
but the spoken narration might (e.g. "check out this amazing rooftop bar!"
with the bar's name only ever spoken aloud, never typed).

Uses the unofficial `youtube-transcript-api` package, which scrapes YouTube's
own auto-generated/creator-uploaded caption tracks -- same legal-gray-area
category as Instagram's og:description scraping, but without an auth wall
and (historically) more stable, since YouTube serves caption tracks to any
client including guests.

Best-effort only: returns None if transcripts are disabled for the video,
none exist, the video is unavailable/age-restricted, or any other error
occurs -- callers must treat this exactly like a cache-miss and fall back
to whatever they already had (see analyze_video_llm.py for the next,
more expensive fallback tier: full video download + Gemini visual analysis).
"""

from youtube_transcript_api import YouTubeTranscriptApi

MAX_TRANSCRIPT_CHARS = 6000


def fetch_transcript(video_id: str, max_chars: int = MAX_TRANSCRIPT_CHARS):
    """Best-effort fetch of the transcript text for a public YouTube video ID.
    Returns a plain-text string (auto-captions have no real punctuation, but
    still carry venue-name signal) truncated to max_chars, or None if
    unavailable for any reason (transcripts disabled, none found, video
    unavailable/age-restricted/private, network error, etc.)."""
    if not video_id:
        return None
    try:
        snippets = YouTubeTranscriptApi().fetch(video_id)
        text = " ".join(s.text for s in snippets).strip()
    except Exception:
        return None
    if not text:
        return None
    return text[:max_chars]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python3 scripts/fetch_youtube_transcript.py <youtube_video_id>")
        sys.exit(1)

    result = fetch_transcript(sys.argv[1])
    if result is None:
        print("No transcript found (disabled, unavailable, or network error).")
    else:
        print(result)
