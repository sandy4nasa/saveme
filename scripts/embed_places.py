#!/usr/bin/env python3
"""
embed_places.py

Generates a semantic embedding for each "ready" saved place (name + address
+ category/tags + caption snippet) via the Gemini embedding API, and stores
it in the DuckDB `embeddings` table. This powers the RAG chat search step
(IMPLEMENTATION_PLAN.md Section 1 Phase 3 / Section 5).

Usage:
  python3 scripts/embed_places.py --db data/saveme.duckdb
  python3 scripts/embed_places.py --db data/saveme.duckdb --limit 20 --dry-run
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

socket.setdefaulttimeout(25)

EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = 768
EMBED_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key={key}"


def build_embedding_text(place):
    parts = [place["name"] or ""]
    if place["address"]:
        parts.append(place["address"])
    if place["tags"]:
        parts.append("Tags: " + ", ".join(place["tags"]))
    if place["raw_caption"]:
        parts.append((place["raw_caption"] or "")[:800])
    return "\n".join(p for p in parts if p)


def call_embed(text, api_key, max_retries=3):
    url = EMBED_URL_TMPL.format(model=EMBED_MODEL, key=api_key)
    body = json.dumps({
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": EMBED_DIM,
    }).encode("utf-8")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            return raw["embedding"]["values"]
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))
    raise last_err


def main():
    parser = argparse.ArgumentParser(description="Generate embeddings for saved_places (DuckDB)")
    parser.add_argument("--db", default="data/saveme.duckdb")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.1)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    api_key = os.environ.get("GOOGLE_AI_API_KEY")
    if not api_key:
        print("Error: GOOGLE_AI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(args.db)

    query = """
        SELECT sp.id, sp.name, sp.address, sp.raw_caption,
               list(pt.tag) FILTER (WHERE pt.tag NOT LIKE 'category:%') AS tags
        FROM saved_places sp
        LEFT JOIN place_tags pt ON pt.place_id = sp.id
        WHERE sp.status = 'ready'
          AND sp.id NOT IN (SELECT place_id FROM embeddings)
        GROUP BY sp.id, sp.name, sp.address, sp.raw_caption
        ORDER BY sp.id
    """
    if args.limit:
        query += f" LIMIT {args.limit}"

    rows = con.execute(query).fetchall()
    cols = ["id", "name", "address", "raw_caption", "tags"]
    places = [dict(zip(cols, r)) for r in rows]

    print(f"{len(places)} places eligible for embedding")

    done = 0
    for n, place in enumerate(places, 1):
        text = build_embedding_text(place)
        try:
            vec = call_embed(text, api_key)
        except Exception as e:
            print(f"[{n}/{len(places)}] id={place['id']} ERROR: {e}")
            time.sleep(args.sleep)
            continue

        print(f"[{n}/{len(places)}] id={place['id']} {place['name']!r:<40} dims={len(vec)}")

        if not args.dry_run:
            con.execute("INSERT OR REPLACE INTO embeddings (place_id, embedding) VALUES (?, ?)",
                        [place["id"], vec])
            done += 1

        time.sleep(args.sleep)

    print(f"\nDone. Embedded {done} places in {args.db}")
    con.close()


if __name__ == "__main__":
    main()
