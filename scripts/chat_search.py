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

ANSWER_PROMPT_TEMPLATE = """You are a helpful assistant for a "places I saved" app. The user is asking a question about places THEY previously saved from Instagram/social media. Answer using ONLY the candidate places listed below -- do not invent places or facts not present here. If none of the candidates genuinely fit the question, say so honestly rather than forcing a recommendation.

User question: "{question}"

Candidate saved places (ranked by relevance):
{candidates}

Give a helpful, conversational answer (2-5 sentences or a short list). For each place you recommend, mention its name and one relevant detail (tag, rating, or area). End by inviting a follow-up if useful."""


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def fetch_candidate_pool(con, user_id):
    """Loads every embedded place for this user once, then we rank in Python.
    Fine at hundreds-of-rows scale; swap for an HNSW index query at bigger scale."""
    rows = con.execute("""
        SELECT sp.id, sp.name, sp.address, sp.source_url, sp.rating, sp.user_ratings_total,
               sp.lat, sp.lng, sp.saved_at,
               list(pt.tag) FILTER (WHERE pt.tag NOT LIKE 'category:%') AS tags,
               list(pt.tag) FILTER (WHERE pt.tag LIKE 'category:%') AS category_tags,
               e.embedding
        FROM saved_places sp
        JOIN embeddings e ON e.place_id = sp.id
        LEFT JOIN place_tags pt ON pt.place_id = sp.id
        WHERE sp.user_id = ? AND sp.status = 'ready'
        GROUP BY sp.id, sp.name, sp.address, sp.source_url, sp.rating, sp.user_ratings_total,
                 sp.lat, sp.lng, sp.saved_at, e.embedding
    """, [user_id]).fetchall()
    cols = ["id", "name", "address", "source_url", "rating", "user_ratings_total",
            "lat", "lng", "saved_at", "tags", "category_tags", "embedding"]
    places = []
    for r in rows:
        d = dict(zip(cols, r))
        cat_tags = d.pop("category_tags") or []
        d["category"] = cat_tags[0].replace("category:", "") if cat_tags else "other"
        d["tags"] = d["tags"] or []
        places.append(d)
    return places


def call_gemini_generate(prompt, api_key):
    url = GEMINI_URL_TMPL.format(model=GEMINI_MODEL, key=api_key)
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return raw["candidates"][0]["content"]["parts"][0]["text"]


def format_candidates(ranked):
    lines = []
    for i, (place, score) in enumerate(ranked, 1):
        tags = ", ".join(place["tags"] or [])
        rating = f"{place['rating']}★ ({place['user_ratings_total']} reviews)" if place["rating"] else "no rating"
        saved_at = place.get("saved_at")
        saved_at_str = f" | saved: {str(saved_at)[:10]}" if saved_at else ""
        lines.append(
            f"{i}. {place['name']} -- {place['address']}\n"
            f"   tags: {tags} | {rating}{saved_at_str} | source: {place['source_url']}"
        )
    return "\n".join(lines)


def run_chat_query(pool, question, api_key, top_k=6):
    """Shared by the CLI and the web server (server.py): embeds the question,
    ranks the given candidate pool, and asks Gemini to answer grounded only
    in the top-K. Returns (answer_text, ranked_list_of_(place, score)).

    Recency-flavored questions ("what's the most recent place I saved?") skip
    embedding similarity entirely and rank by saved_at instead -- semantic
    search has no notion of time and would otherwise return an arbitrary
    "similar sounding" place rather than the actually-most-recent one."""
    if RECENCY_RE.search(question):
        ranked = sorted(
            ((place, 1.0) for place in pool if place.get("saved_at")),
            key=lambda x: x[0]["saved_at"],
            reverse=True,
        )[:top_k]
    else:
        query_vec = call_embed(question, api_key)
        ranked = sorted(
            ((place, cosine_similarity(query_vec, place["embedding"])) for place in pool),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]
    prompt = ANSWER_PROMPT_TEMPLATE.format(question=question, candidates=format_candidates(ranked))
    answer = call_gemini_generate(prompt, api_key)
    return answer, ranked


def main():
    parser = argparse.ArgumentParser(description="RAG chat search over a user's saved places")
    parser.add_argument("question", help="Natural language question")
    parser.add_argument("--db", default="data/saveme.duckdb")
    parser.add_argument("--user", default="sandy4nasa", help="user_id to scope the search to")
    parser.add_argument("--top-k", type=int, default=6, help="Number of candidates to pass to the LLM")
    parser.add_argument("--show-candidates", action="store_true", help="Print the ranked candidate list before the answer")
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

    answer, ranked = run_chat_query(pool, args.question, api_key, args.top_k)

    if args.show_candidates:
        print("--- Top candidates ---")
        print(format_candidates(ranked))
        print()

    print("--- Answer ---")
    print(answer)


if __name__ == "__main__":
    main()
