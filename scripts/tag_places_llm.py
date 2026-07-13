#!/usr/bin/env python3
"""
tag_places_llm.py

LLM auto-tagging step (IMPLEMENTATION_PLAN.md, Section 1 Phase 2 step 7).
Reads "ready" rows from the DuckDB saved_places table and asks Gemini to
classify each into a broad category plus a handful of descriptive tags
(kid-friendly, vegetarian, outdoor-seating, romantic, etc.), grounded in
the caption text, hashtags, and Google Places `place_types`. Writes results
into `place_tags`.

Tags are intentionally NOT a fixed closed vocabulary — captions here span
food, travel destinations, real estate, and shopping (see collections:
Food Street, Travel, Properties, Jewelry), so the LLM proposes tags freely
but is steered by a suggested list + few-shot-style guidance to keep tags
consistent/reusable across places rather than one-off phrasing.

Usage:
  python3 scripts/tag_places_llm.py --db data/saveme.duckdb
  python3 scripts/tag_places_llm.py --db data/saveme.duckdb --limit 10 --dry-run
  python3 scripts/tag_places_llm.py --db data/saveme.duckdb --retag   # re-tag places that already have tags
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import load_dotenv  # noqa: E402

socket.setdefaulttimeout(25)  # safety net in case per-request timeout is bypassed by a proxy

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

SUGGESTED_TAGS = [
    "kid-friendly", "vegetarian", "vegan", "pure-veg", "non-veg", "outdoor-seating",
    "romantic", "family-friendly", "budget-friendly", "fine-dining", "street-food",
    "cafe", "dessert", "breakfast-spot", "late-night", "live-music", "scenic-view",
    "heritage", "photogenic", "farmland", "real-estate", "shopping", "nature",
    "hiking", "beach", "waterfall", "adventure", "wellness", "pet-friendly",
]

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {
            "type": "STRING",
            "description": "One broad category: restaurant, cafe, travel_destination, real_estate, shopping, nature, activity, recipe, diy_craft, or other. Use recipe/diy_craft for content-only saves with no real-world venue (cooking posts, craft/DIY tutorials).",
        },
        "tags": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "tag": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["tag", "confidence"],
            },
            "description": "3-6 short kebab-case descriptive tags with confidence 0-1 each.",
        },
    },
    "required": ["category", "tags"],
}

PROMPT_TEMPLATE = """Classify this saved place/post for a "places I saved" app so a user can later filter/search by tags.

Place name: {name}
Google Places categories: {place_types}
Caption: \"\"\"{caption}\"\"\"
Hashtags: {hashtags}

Pick ONE broad category (restaurant, cafe, travel_destination, real_estate, shopping, nature, activity, recipe, diy_craft, or other), and 3-6 short kebab-case descriptive tags with a confidence score each (0-1), grounded strictly in what the caption/categories actually say -- don't guess facts not present. If this is content with no real-world venue (a cooking/recipe post or a DIY/craft tutorial, with no Google Places categories given), use category "recipe" or "diy_craft" as appropriate rather than forcing it into a place-type category. Prefer reusing tags from this suggested list where they fit: {suggested_tags}. You may add other tags not in the list if clearly warranted."""


def build_request(place):
    prompt = PROMPT_TEMPLATE.format(
        name=place["name"] or "unknown",
        place_types=", ".join(place["place_types"] or []),
        caption=(place["raw_caption"] or "")[:1200],
        hashtags=", ".join((place["hashtags"] or [])[:10]),
        suggested_tags=", ".join(SUGGESTED_TAGS),
    )
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    }


def call_gemini(place, api_key, max_retries=3):
    url = GEMINI_URL_TMPL.format(model=GEMINI_MODEL, key=api_key)
    body = json.dumps(build_request(place)).encode("utf-8")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            text = raw["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))  # backoff on transient network stalls
    raise last_err


def main():
    parser = argparse.ArgumentParser(description="LLM auto-tagging for saved_places (DuckDB)")
    parser.add_argument("--db", default="data/saveme.duckdb", help="DuckDB file path")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N eligible places")
    parser.add_argument("--dry-run", action="store_true", help="Print classifications without writing to DB")
    parser.add_argument("--retag", action="store_true", help="Re-tag places that already have tags (default: skip them)")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds between LLM calls")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    api_key = os.environ.get("GOOGLE_AI_API_KEY")
    if not api_key:
        print("Error: GOOGLE_AI_API_KEY not set (env var or .env file)", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(args.db)

    if args.retag:
        where_clause = "status = 'ready'"
    else:
        where_clause = """
            status = 'ready'
            AND id NOT IN (SELECT DISTINCT place_id FROM place_tags)
        """

    query = f"""
        SELECT id, name, place_types, raw_caption, hashtags
        FROM saved_places
        WHERE {where_clause}
        ORDER BY id
    """
    if args.limit:
        query += f" LIMIT {args.limit}"

    places = con.execute(query).fetchall()
    columns = ["id", "name", "place_types", "raw_caption", "hashtags"]
    places = [dict(zip(columns, row)) for row in places]

    print(f"{len(places)} places eligible for tagging")

    tagged_count = 0
    error_count = 0

    for n, place in enumerate(places, 1):
        try:
            result = call_gemini(place, api_key)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:200]
            print(f"[{n}/{len(places)}] id={place['id']} ERROR HTTP {e.code}: {detail}")
            error_count += 1
            time.sleep(args.sleep)
            continue
        except Exception as e:
            print(f"[{n}/{len(places)}] id={place['id']} ERROR: {e}")
            error_count += 1
            time.sleep(args.sleep)
            continue

        category = result.get("category", "other")
        tags = result.get("tags", [])
        tag_names = [t["tag"] for t in tags]
        print(f"[{n}/{len(places)}] id={place['id']} {place['name']!r:<40} category={category:<20} tags={tag_names}")

        if not args.dry_run:
            con.execute("DELETE FROM place_tags WHERE place_id = ?", [place["id"]])
            con.execute("INSERT OR REPLACE INTO place_tags (place_id, tag, confidence) VALUES (?, ?, ?)",
                        [place["id"], f"category:{category}", 1.0])
            for t in tags:
                con.execute("INSERT OR REPLACE INTO place_tags (place_id, tag, confidence) VALUES (?, ?, ?)",
                            [place["id"], t["tag"], t.get("confidence", 0.5)])
            tagged_count += 1

        time.sleep(args.sleep)

    print(f"\nDone. Tagged {tagged_count} places ({error_count} errors) in {args.db}")
    con.close()


if __name__ == "__main__":
    main()
