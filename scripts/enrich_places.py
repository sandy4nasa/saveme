#!/usr/bin/env python3
"""
enrich_places.py

Takes the normalized saved-items JSON produced by parse_instagram_export.py
and enriches each item with a real-world place (lat/lng, address, rating,
opening hours) via the Google Places API — the "Enrichment" step of the
SaveMe extraction waterfall (see IMPLEMENTATION_PLAN.md, Section 1 Phase 2).

Candidate place-name extraction (interim heuristic, pre-LLM):
  1. Look for a "📍" location-marker emoji in the caption — the text right
     after it is usually the venue name/address (very common IG convention).
  2. Fall back to the post `title` field if non-empty.
  3. Otherwise mark as `needs_llm_extraction` (caption is too vague — this
     is exactly the case the LLM-NLP fallback step is designed for; see
     IMPLEMENTATION_PLAN.md Section 1 Phase 2 step 4).

Only items detected as platform-agnostic "place-like" posts get a Places
API call — this keeps API usage (and cost) low by skipping obvious
non-place saves.

Usage:
  python3 scripts/enrich_places.py data/saved_items.json --out data/enriched_items.json
  python3 scripts/enrich_places.py data/saved_items.json --limit 10   # test on a small batch
  python3 scripts/enrich_places.py data/saved_items.json --dry-run    # show what would be queried, no API calls

Requires GOOGLE_PLACES_API_KEY in the environment or in a local .env file
(auto-loaded from the project root if present).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLACES_SEARCHTEXT_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.types,places.businessStatus"
)
LOCATION_MARKER_RE = re.compile(r"📍\s*([^\n]{3,150})")


def load_dotenv(env_path: Path):
    """Minimal .env loader (no external dependency). Only sets vars that
    aren't already present in the environment."""
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        os.environ.setdefault(key, value)


MAX_QUERY_LEN = 70

# Generic descriptor phrases that sometimes follow a 📍 marker but aren't an
# actual place name (common in real-estate/listing-style captions). These
# get routed to the LLM-extraction fallback instead of wasting a Places call.
NON_PLACE_PHRASES = (
    "location in video",
    "location:",
    "prime location",
    "east-facing",
    "west-facing",
    "north-facing",
    "south-facing",
)


def extract_candidate_query(item):
    """Returns (query_string, source) or (None, 'needs_llm_extraction')."""
    caption = item.get("raw_caption") or ""
    match = LOCATION_MARKER_RE.search(caption)
    if match:
        candidate = match.group(1).strip()
        # Stop at the first strong delimiter (newline already excluded by
        # regex; also cut at double-spaces, en/em-dash, or emoji runs) so we
        # don't drag in an entire multi-clause sentence as the "place name".
        candidate = re.split(r"\s{2,}|\s[–—-]\s|#", candidate)[0].strip(" .,-")
        candidate = candidate[:MAX_QUERY_LEN].strip()

        if len(candidate) >= 3 and candidate.lower() not in NON_PLACE_PHRASES \
                and not any(p in candidate.lower() for p in NON_PLACE_PHRASES):
            return candidate, "location_marker"

    title = (item.get("title") or "").strip()
    if title:
        return title, "title"

    return None, "needs_llm_extraction"


def call_places_textsearch(query, api_key):
    """Calls Places API (New) searchText endpoint (POST + JSON body)."""
    body = json.dumps({"textQuery": query}).encode("utf-8")
    req = urllib.request.Request(
        PLACES_SEARCHTEXT_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def enrich_item(item, api_key, dry_run=False):
    query, source = extract_candidate_query(item)

    if query is None:
        return {
            **item,
            "enrichment_status": "skipped_needs_llm_extraction",
            "enrichment_query": None,
        }

    if dry_run:
        return {
            **item,
            "enrichment_status": "dry_run",
            "enrichment_query": query,
            "enrichment_query_source": source,
        }

    try:
        result = call_places_textsearch(query, api_key)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return {
            **item,
            "enrichment_status": f"error: HTTP {e.code}",
            "enrichment_error_detail": body[:500],
            "enrichment_query": query,
            "enrichment_query_source": source,
        }
    except Exception as e:  # network/API errors shouldn't kill the whole batch
        return {
            **item,
            "enrichment_status": f"error: {e}",
            "enrichment_query": query,
            "enrichment_query_source": source,
        }

    places = result.get("places", [])
    if not places:
        return {
            **item,
            "enrichment_status": "no_match",
            "enrichment_query": query,
            "enrichment_query_source": source,
        }

    top = places[0]
    location = top.get("location", {})

    return {
        **item,
        "enrichment_status": "ready",
        "enrichment_query": query,
        "enrichment_query_source": source,
        "place_id": top.get("id"),
        "place_name": top.get("displayName", {}).get("text"),
        "address": top.get("formattedAddress"),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
        "rating": top.get("rating"),
        "user_ratings_total": top.get("userRatingCount"),
        "place_types": top.get("types", []),
        "business_status": top.get("businessStatus"),
    }


def main():
    parser = argparse.ArgumentParser(description="Enrich saved items with Google Places data")
    parser.add_argument("input", help="Path to saved_items.json (output of parse_instagram_export.py)")
    parser.add_argument("--out", default="data/enriched_items.json", help="Output JSON path")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N items (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be queried without calling the API")
    parser.add_argument("--sleep", type=float, default=0.05, help="Seconds to sleep between API calls (rate-limit courtesy)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not args.dry_run and not api_key:
        print("Error: GOOGLE_PLACES_API_KEY not set (env var or .env file)", file=sys.stderr)
        sys.exit(1)

    input_path = Path(args.input)
    with open(input_path, encoding="utf-8") as f:
        items = json.load(f)

    if args.limit:
        items = items[: args.limit]

    enriched = []
    for i, item in enumerate(items, 1):
        result = enrich_item(item, api_key, dry_run=args.dry_run)
        enriched.append(result)
        print(f"[{i}/{len(items)}] {result['enrichment_status']:<30} "
              f"query={result.get('enrichment_query')!r:<50} "
              f"-> {result.get('place_name', '')}")
        if not args.dry_run:
            time.sleep(args.sleep)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(enriched, f, indent=2, ensure_ascii=False)

    # Summary
    statuses = {}
    for r in enriched:
        key = r["enrichment_status"].split(" (")[0].split(":")[0]
        statuses[key] = statuses.get(key, 0) + 1

    print(f"\nDone. {len(enriched)} items -> {out_path}")
    print(f"Status breakdown: {statuses}")


if __name__ == "__main__":
    main()
