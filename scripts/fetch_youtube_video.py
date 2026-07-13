#!/usr/bin/env python3
"""
fetch_youtube_video.py

Downloads a public YouTube video for the video-visual-analysis fallback
(analyze_video_llm.py) -- the last-resort tier used when even title,
description, AND transcript (fetch_youtube_transcript.py) don't name a
specific place, e.g. a POV walking-tour video whose only signal is
on-screen text, signboards, or something shown but never said/written.

Unlike Instagram (which requires a paid third-party API because yt-dlp
fails without an authenticated session -- see fetch_instagram_video.py for
the full explanation of why), yt-dlp works directly against YouTube with NO
cookies/login for the vast majority of public videos. No paid API needed
for this platform.

Best-effort only: returns False on any failure (video too large/long,
private/age-restricted, network error, yt-dlp extractor breakage, etc.) --
callers must handle this exactly like Instagram's download_video() failure
and fall back to whatever caption/transcript-based result they already had.
"""

import os

import yt_dlp

MAX_VIDEO_BYTES = 60 * 1024 * 1024  # 60MB safety cap, same as fetch_instagram_video.py
MAX_DURATION_SECONDS = 20 * 60  # this is a shorts/vlog fallback, not a movie transcriber


def download_video(source_url, dest_path, max_bytes=MAX_VIDEO_BYTES, max_duration=MAX_DURATION_SECONDS):
    """Downloads a public YouTube video to dest_path (should end in .mp4).
    Returns True on success, False on any failure -- never raises."""
    ydl_opts = {
        "outtmpl": dest_path,
        # Prefer a single progressive mp4 stream so no separate audio+video
        # merge step (and no ffmpeg dependency) is needed; the size cap is
        # enforced via max_filesize below rather than an inline format-string
        # filesize filter, which proved unreliable across different videos'
        # format-list shapes during testing (some formats report no filesize
        # estimate up front, causing the string filter to mis-select).
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": max_bytes,
        "outtmpl_na_placeholder": "",
        # Callers create dest_path via tempfile.NamedTemporaryFile(delete=False),
        # which pre-creates an empty file at that path -- without this, yt-dlp's
        # default "already downloaded, skip" logic treats that 0-byte file as
        # a complete prior download and silently no-ops (confirmed during
        # testing: reports "100% of 0.00B" and leaves the file empty).
        "overwrites": True,
        # As of late 2025, YouTube's default "web" client forces SABR
        # streaming for most formats, which strips the direct URL and
        # requires a PO (proof-of-origin) token we don't have -- resulting
        # in a silent 0-byte "download". The "android" client still serves
        # plain progressive/adaptive URLs without a PO token for the vast
        # majority of public videos (confirmed working during development;
        # this is an actively-evolving cat-and-mouse area of yt-dlp, so a
        # future YouTube change could require revisiting this).
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
            duration = info.get("duration") or 0
            if duration and duration > max_duration:
                return False
            ydl.download([source_url])
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    import tempfile

    if len(sys.argv) != 2:
        print("Usage: python3 scripts/fetch_youtube_video.py <youtube_video_url>")
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    ok = download_video(sys.argv[1], tmp_path)
    if ok:
        print(f"Downloaded to {tmp_path} ({os.path.getsize(tmp_path)} bytes)")
    else:
        print("Download failed (too large/long, private/age-restricted, or network error).")
        os.remove(tmp_path)
