#!/usr/bin/env python3
"""
chat_search.py

The Conversational Query Service (IMPLEMENTATION_PLAN.md Section 1 Phase 3 /
Section 5): user asks a natural-language question ("where should I eat
today?", "any kid-friendly places near JP Nagar?"), we embed the question,
find the most semantically similar saved places (scoped to one user), and
ask Gemini to answer using ONLY those candidates -- grounded RAG, not
open-web knowledge -- citing the source place name + link for each
recommendation.

Similarity search is done in-process with plain cosine similarity (numpy-free,
pure Python) rather than the `vss` HNSW index -- at ~250 rows a brute-force
scan is microseconds and avoids extension/index-maintenance complexity for
this data size. If the dataset grows to tens of thousands+ rows per user,
switch to the `vss` HNSW index already reserved in the schema (Section 3).

Usage:
  python3 scripts/chat_search.py --db data/saveme.duckdb --user sandy4nasa "where should I eat today?"
  python3 scripts/chat_search.py --db data/saveme.duckdb --user sandy4nasa --top-k 8 "kid friendly places near JP Nagar"
"""

import argparse
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import load_dotenv  # noqa: E402
from embed_places import call_embed, EMBED_MODEL  # noqa: E402

socket.setdefaulttimeout(25)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# Pure semantic (embedding) search has no notion of save-time at all -- a
# question like "what's the most recent place I saved?" would otherwise just
# match whatever place's text happens to be closest to that phrasing, which
# looks like a wrong/random answer. Detect recency-flavored questions and
# rank by saved_at instead of cosine similarity in that case.
RECENCY_RE = re.compile(r"\b(recent|latest|last saved|just saved|most recently|newest)\b", re.I)

# Same problem for location: "what's near me?" has no connection, semantically,
# to the places themselves (a cafe's embedding doesn't encode "nearness to the
# user"), so without this the app would just return whatever place sounds most
# topically similar to the word "near" -- effectively random. Detect
# proximity-flavored questions and rank by actual distance from the user's
# current device location (if the browser provided one) instead.
NEARBY_RE = re.compile(r"\b(near me|nearby|close to me|close by|around me|around here|around my location|closest)\b", re.I)

EARTH_RADIUS_KM = 6371.0

ANSWER_PROMPT_TEMPLATE = """You are a helpful assistant for a "places I saved" app. The user is asking a question about places THEY previously saved from Instagram/social media. Answer using ONLY the candidate places listed below -- do not invent places or facts not present here. If none of the candidates genuinely fit the question, say so honestly rather than forcing a recommendation.
{location_context}
User question: "{question}"

Candidate saved places (ranked by relevance):
{candidates}

Give a helpful, conversational answer (2-5 sentences or a short list). For each place you recommend, mention its name and one relevant detail (tag, rating, distance, or area). End by inviting a follow-up if useful."""


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km between two lat/lng points."""
    lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def fetch_candidate_pool(con, user_id):
    """Loads every embedded place for this user once, then we rank in Python.
    Fine at hundreds-of-rows scale; swap for an HNSW index query at bigger scale."""
    rows = con.execute("""
        SELECT sp.id, sp.name, sp.address, sp.source_url, sp.rating, sp.user_ratings_total,
               sp.lat, sp.lng, sp.saved_at, sp.platform,
               list(pt.tag) FILTER (WHERE pt.tag NOT LIKE 'category:%') AS tags,
               list(pt.tag) FILTER (WHERE pt.tag LIKE 'category:%') AS category_tags,
               e.embedding
        FROM saved_places sp
        JOIN embeddings e ON e.place_id = sp.id
        LEFT JOIN place_tags pt ON pt.place_id = sp.id
        WHERE sp.user_id = ? AND sp.status IN ('ready', 'saved_no_place')
        GROUP BY sp.id, sp.name, sp.address, sp.source_url, sp.rating, sp.user_ratings_total,
                 sp.lat, sp.lng, sp.saved_at, sp.platform, e.embedding
    """, [user_id]).fetchall()
    cols = ["id", "name", "address", "source_url", "rating", "user_ratings_total",
            "lat", "lng", "saved_at", "platform", "tags", "category_tags", "embedding"]
    places = []
    for r in rows:
        d = dict(zip(cols, r))
        cat_tags = d.pop("category_tags") or []
        d["category"] = cat_tags[0].replace("category:", "") if cat_tags else "other"
        d["tags"] = d["tags"] or []
        d["platform"] = d["platform"] or "instagram"
        places.append(d)
    return places


def call_gemini_generate(prompt, api_key, max_retries=3):
    """Same retry-with-backoff pattern as embed_places.call_embed() -- a
    transient Gemini hiccup (rate limit, brief 5xx) used to fail the whole
    chat request outright with no retry, surfacing as an unexplained 500 to
    the user."""
    url = GEMINI_URL_TMPL.format(model=GEMINI_MODEL, key=api_key)
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4}}).encode("utf-8")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            return raw["candidates"][0]["content"]["parts"][0]["text"]
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))
    raise last_err


def format_candidates(ranked, user_lat=None, user_lng=None):
    lines = []
    for i, (place, score) in enumerate(ranked, 1):
        tags = ", ".join(place["tags"] or [])
        rating = f"{place['rating']}★ ({place['user_ratings_total']} reviews)" if place["rating"] else "no rating"
        saved_at = place.get("saved_at")
        saved_at_str = f" | saved: {str(saved_at)[:10]}" if saved_at else ""
        distance_str = ""
        if user_lat is not None and user_lng is not None and place.get("lat") is not None and place.get("lng") is not None:
            distance_str = f" | {haversine_km(user_lat, user_lng, place['lat'], place['lng']):.1f} km from you"
        location_line = place["address"] if place.get("address") else "(saved content, no map location)"
        lines.append(
            f"{i}. {place['name']} -- {location_line}\n"
            f"   tags: {tags} | {rating}{saved_at_str}{distance_str} | source: {place['source_url']}"
        )
    return "\n".join(lines)


def run_chat_query(pool, question, api_key, top_k=6, user_lat=None, user_lng=None):
    """Shared by the CLI and the web server (server.py): embeds the question,
    ranks the given candidate pool, and asks Gemini to answer grounded only
    in the top-K. Returns (answer_text, ranked_list_of_(place, score)).

    Recency-flavored questions ("what's the most recent place I saved?") skip
    embedding similarity entirely and rank by saved_at instead -- semantic
    search has no notion of time and would otherwise return an arbitrary
    "similar sounding" place rather than the actually-most-recent one.

    Proximity-flavored questions ("what's nearby?", "closest cafe?") behave
    the same way: they rank by actual haversine distance from the user's
    current device location (user_lat/user_lng, from the browser's
    Geolocation API) instead of semantic similarity, which has no notion of
    "where the user physically is right now". If no location was provided
    (permission denied, unsupported browser, etc.), this tier is skipped and
    falls through to normal semantic ranking."""
    if RECENCY_RE.search(question):
        ranked = sorted(
            ((place, 1.0) for place in pool if place.get("saved_at")),
            key=lambda x: x[0]["saved_at"],
            reverse=True,
        )[:top_k]
    elif NEARBY_RE.search(question) and user_lat is not None and user_lng is not None:
        ranked = sorted(
            (
                (place, -haversine_km(user_lat, user_lng, place["lat"], place["lng"]))
                for place in pool
                if place.get("lat") is not None and place.get("lng") is not None
            ),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]
    else:
        query_vec = call_embed(question, api_key)
        ranked = sorted(
            ((place, cosine_similarity(query_vec, place["embedding"])) for place in pool),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

    location_context = ""
    if user_lat is not None and user_lng is not None:
        location_context = f"\nThe user's current location is approximately ({user_lat:.4f}, {user_lng:.4f}) -- distances to each candidate below are measured from there.\n"

    prompt = ANSWER_PROMPT_TEMPLATE.format(
        question=question,
        candidates=format_candidates(ranked, user_lat, user_lng),
        location_context=location_context,
    )
    answer = call_gemini_generate(prompt, api_key)
    return answer, ranked


def main():
    parser = argparse.ArgumentParser(description="RAG chat search over a user's saved places")
    parser.add_argument("question", help="Natural language question")
    parser.add_argument("--db", default="data/saveme.duckdb")
    parser.add_argument("--user", default="sandy4nasa", help="user_id to scope the search to")
    parser.add_argument("--top-k", type=int, default=6, help="Number of candidates to pass to the LLM")
    parser.add_argument("--show-candidates", action="store_true", help="Print the ranked candidate list before the answer")
    parser.add_argument("--lat", type=float, default=None, help="User's current latitude (simulates browser geolocation)")
    parser.add_argument("--lng", type=float, default=None, help="User's current longitude (simulates browser geolocation)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    api_key = os.environ.get("GOOGLE_AI_API_KEY")
    if not api_key:
        print("Error: GOOGLE_AI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(args.db, read_only=True)
    pool = fetch_candidate_pool(con, args.user)
    con.close()

    if not pool:
        print("No embedded places found for this user. Run embed_places.py first.", file=sys.stderr)
        sys.exit(1)

    answer, ranked = run_chat_query(pool, args.question, api_key, args.top_k, user_lat=args.lat, user_lng=args.lng)

    if args.show_candidates:
        print("--- Top candidates ---")
        print(format_candidates(ranked, args.lat, args.lng))
        print()

    print("--- Answer ---")
    print(answer)


if __name__ == "__main__":
    main()
