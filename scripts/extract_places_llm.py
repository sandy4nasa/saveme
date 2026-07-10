#!/usr/bin/env python3
"""
extract_places_llm.py

Handles the "Multimodal/NLP fallback" step of the extraction waterfall
(IMPLEMENTATION_PLAN.md, Section 1 Phase 2, step 4) for items that the
cheap 📍-marker heuristic in enrich_places.py couldn't resolve
(enrichment_status == "skipped_needs_llm_extraction" or "no_match").

For each such item, sends the caption + hashtags + owner info to Gemini
with a strict JSON schema asking it to identify the actual place name and
city/area mentioned (if any), plus a confidence score. High-confidence
extractions are then run back through the same Google Places lookup used
in enrich_places.py. Low-confidence / no-place-found items are marked
`needs_review` rather than silently enriched with a bad guess.

Usage:
  python3 scripts/extract_places_llm.py data/enriched_items.json --out data/enriched_items_v2.json
  python3 scripts/extract_places_llm.py data/enriched_items.json --limit 10   # test on a small batch
  python3 scripts/extract_places_llm.py data/enriched_items.json --dry-run    # show LLM prompts/extractions, skip Places calls

Requires GOOGLE_AI_API_KEY (Gemini) and GOOGLE_PLACES_API_KEY in the
environment or a local .env file.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Reuse the Places API call + response-mapping logic already built and
# validated in enrich_places.py instead of duplicating it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import call_places_textsearch, load_dotenv  # noqa: E402

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

CONFIDENCE_THRESHOLD = 0.5

STATUSES_TO_RETRY = {"skipped_needs_llm_extraction", "no_match"}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_place_mentioned": {
            "type": "BOOLEAN",
            "description": "True if the caption describes/recommends a specific real-world venue (restaurant, shop, park, hotel, landmark, etc.), false if it's just a general scene, meme, or non-place content.",
        },
        "place_name": {
            "type": "STRING",
            "description": "The specific venue/place name, as best identified from the caption. Empty string if none.",
        },
        "city_or_area": {
            "type": "STRING",
            "description": "City, neighborhood, or area mentioned that helps disambiguate the place (e.g. 'Jayanagar, Bengaluru'). Empty string if not mentioned.",
        },
        "confidence": {
            "type": "NUMBER",
            "description": "0.0-1.0 confidence that place_name is a real, findable venue.",
        },
    },
    "required": ["is_place_mentioned", "place_name", "city_or_area", "confidence"],
}

PROMPT_TEMPLATE = """You are extracting a real-world venue name from a social media caption for a "saved places" app.

Caption:
\"\"\"{caption}\"\"\"

Hashtags: {hashtags}
Posted by: {owner_name} (@{owner_username})

Identify the specific place/venue this post is about (restaurant, cafe, shop, hotel, park, landmark, etc.), and the city or area it's in if mentioned. If the caption is not about a specific real-world place (e.g. it's a meme, a generic clip, generic advice, or a product-only post with no venue), set is_place_mentioned to false."""


def build_gemini_request(item):
    prompt = PROMPT_TEMPLATE.format(
        caption=(item.get("raw_caption") or "")[:1500],
        hashtags=", ".join(item.get("hashtags", [])[:10]),
        owner_name=item.get("owner_name") or "unknown",
        owner_username=item.get("owner_username") or "unknown",
    )
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.1,
        },
    }


def call_gemini(item, api_key):
    url = GEMINI_URL_TMPL.format(model=GEMINI_MODEL, key=api_key)
    body = json.dumps(build_gemini_request(item)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    text = raw["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def enrich_with_places(item, query, api_key):
    """Mirrors enrich_item() in enrich_places.py but takes an explicit query
    string (from the LLM) instead of the 📍-marker heuristic."""
    try:
        result = call_places_textsearch(query, api_key)
    except urllib.error.HTTPError as e:
        return {**item, "enrichment_status": f"error: HTTP {e.code}"}
    except Exception as e:
        return {**item, "enrichment_status": f"error: {e}"}

    places = result.get("places", [])
    if not places:
        return {**item, "enrichment_status": "no_match", "enrichment_query": query, "enrichment_query_source": "llm"}

    top = places[0]
    location = top.get("location", {})
    return {
        **item,
        "enrichment_status": "ready",
        "enrichment_query": query,
        "enrichment_query_source": "llm",
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


def process_item(item, gemini_key, places_key, dry_run=False):
    try:
        extraction = call_gemini(item, gemini_key)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        return {**item, "enrichment_status": f"llm_error: HTTP {e.code}", "llm_error_detail": detail}
    except Exception as e:
        return {**item, "enrichment_status": f"llm_error: {e}"}

    item = {
        **item,
        "llm_is_place": extraction.get("is_place_mentioned"),
        "llm_place_name": extraction.get("place_name"),
        "llm_city_or_area": extraction.get("city_or_area"),
        "llm_confidence": extraction.get("confidence"),
    }

    if not extraction.get("is_place_mentioned") or not extraction.get("place_name"):
        return {**item, "enrichment_status": "no_place_in_caption"}

    if extraction.get("confidence", 0) < CONFIDENCE_THRESHOLD:
        return {**item, "enrichment_status": "needs_review_low_confidence"}

    query = f"{extraction['place_name']}, {extraction['city_or_area']}".strip(", ")

    if dry_run:
        return {**item, "enrichment_status": "dry_run", "enrichment_query": query, "enrichment_query_source": "llm"}

    return enrich_with_places(item, query, places_key)


def main():
    parser = argparse.ArgumentParser(description="LLM-based place extraction fallback (Gemini + Places API)")
    parser.add_argument("input", help="Path to enriched_items.json (output of enrich_places.py)")
    parser.add_argument("--out", default="data/enriched_items_v2.json", help="Output JSON path")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N eligible items (testing)")
    parser.add_argument("--dry-run", action="store_true", help="Run LLM extraction, skip Places API calls")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to sleep between LLM calls")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    gemini_key = os.environ.get("GOOGLE_AI_API_KEY")
    places_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not gemini_key:
        print("Error: GOOGLE_AI_API_KEY not set (env var or .env file)", file=sys.stderr)
        sys.exit(1)
    if not args.dry_run and not places_key:
        print("Error: GOOGLE_PLACES_API_KEY not set (env var or .env file)", file=sys.stderr)
        sys.exit(1)

    with open(args.input, encoding="utf-8") as f:
        items = json.load(f)

    eligible_idx = [i for i, r in enumerate(items) if r.get("enrichment_status") in STATUSES_TO_RETRY]
    if args.limit:
        eligible_idx = eligible_idx[: args.limit]

    print(f"{len(eligible_idx)} items eligible for LLM extraction (of {len(items)} total)")

    for n, idx in enumerate(eligible_idx, 1):
        items[idx] = process_item(items[idx], gemini_key, places_key, dry_run=args.dry_run)
        r = items[idx]
        print(f"[{n}/{len(eligible_idx)}] {r['enrichment_status']:<28} "
              f"llm_place={r.get('llm_place_name')!r:<35} conf={r.get('llm_confidence')} "
              f"-> {r.get('place_name', '')}")
        time.sleep(args.sleep)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

    statuses = {}
    for r in items:
        key = r["enrichment_status"].split(" (")[0].split(":")[0]
        statuses[key] = statuses.get(key, 0) + 1
    print(f"\nDone -> {out_path}")
    print(f"Full status breakdown: {statuses}")


if __name__ == "__main__":
    main()
