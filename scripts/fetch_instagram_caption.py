#!/usr/bin/env python3
"""
fetch_instagram_caption.py

Best-effort server-side fetch of the real Instagram caption for a shared
post URL, so users no longer have to manually retype/paste the caption
into the share-target note box.

How it works: Instagram serves an `og:description` meta tag on public post
pages (used by other platforms -- iMessage, WhatsApp, Twitter -- to render
link-preview cards). That tag contains the real caption text in the form
`"{likes} likes, {comments} comments - {username} on {date}: \"{caption}\"."`
and is present WITHOUT requiring a logged-in session, even though the page
itself shows a login wall for viewing the actual photo/video.

This only works for PUBLIC posts/accounts. Private accounts, deleted posts,
or any change to Instagram's page markup will make this silently fail --
callers must treat `fetch_caption()` returning None as a normal, expected
outcome and fall back to whatever manual note (if any) the user supplied.

This is unofficial scraping of a public HTML page, not a supported Meta
API -- keep request volume low (one request per share) and never retry
aggressively; a failure here should never block or crash the save flow.
"""

import html
import re
import urllib.error
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

OG_DESCRIPTION_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')

# Matches Instagram's "{N} likes, {N} comments - {username} on {date}: "{caption}"."
# wrapper that og:description uses. Falls back to using the raw content as-is
# if this doesn't match (format changes, or a shape we haven't seen).
CAPTION_PREFIX_RE = re.compile(
    r'^[\d,]+\s+(?:like|likes)(?:,\s*[\d,]+\s+(?:comment|comments))?\s*-\s*'
    r'([\w.]+)\s+on\s+([^:]+):\s*(.*)$',
    re.DOTALL,
)

REQUEST_TIMEOUT_SECONDS = 8


def _clean_caption(rest: str) -> str:
    rest = rest.strip()
    if rest.startswith('"'):
        rest = rest[1:]
    if rest.endswith('".'):
        rest = rest[:-2]
    elif rest.endswith('"'):
        rest = rest[:-1]
    return rest.strip()


def fetch_caption(source_url: str, timeout: float = REQUEST_TIMEOUT_SECONDS):
    """Best-effort fetch of the real caption + author username for a public
    Instagram post/reel URL. Returns {"caption": str, "owner_username": str|None}
    on success, or None if unavailable for any reason (private post, network
    error, markup change, etc.) -- callers must handle None gracefully."""
    if not source_url or "instagram.com" not in source_url:
        return None

    req = urllib.request.Request(source_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    except Exception:
        return None

    match = OG_DESCRIPTION_RE.search(body)
    if not match:
        return None

    raw = html.unescape(match.group(1))
    prefix_match = CAPTION_PREFIX_RE.match(raw)
    if prefix_match:
        owner_username, _date, rest = prefix_match.groups()
        caption = _clean_caption(rest)
    else:
        owner_username, caption = None, raw.strip()

    if not caption:
        return None

    return {"caption": caption, "owner_username": owner_username}


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python3 scripts/fetch_instagram_caption.py <instagram_post_url>")
        sys.exit(1)

    result = fetch_caption(sys.argv[1])
    if result is None:
        print("No caption found (private post, network error, or markup changed).")
    else:
        print(f"owner_username: {result['owner_username']}")
        print(f"caption:\n{result['caption']}")
