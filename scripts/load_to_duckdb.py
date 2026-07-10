#!/usr/bin/env python3
"""
load_to_duckdb.py

Creates (or reuses) a local DuckDB file as the SaveMe datastore and loads
the enriched saved-items JSON (output of enrich_places.py / extract_places_llm.py)
into it. Replaces the Postgres+PostGIS+pgvector design in IMPLEMENTATION_PLAN.md
Section 3 with an equivalent single-file DuckDB schema:

  - Geo: DuckDB `spatial` extension (ST_Point / ST_DWithin) instead of PostGIS.
  - Vector search: DuckDB `vss` extension (HNSW index over FLOAT[] columns)
    instead of pgvector. Embeddings aren't populated yet (that's the RAG/chat
    step) but the column + index are ready for it.

Why DuckDB for now: no server/hosting to stand up, runs as a single file
(`data/saveme.duckdb`) you can open with the `duckdb` CLI or Python/Node
clients directly, and is genuinely fast for this data volume (hundreds to
low millions of rows). If/when the app needs concurrent multi-writer access
across many users at production scale, the schema below maps 1:1 onto the
Postgres version in IMPLEMENTATION_PLAN.md Section 3, so migrating later is
a straightforward port, not a redesign.

Usage:
  python3 scripts/load_to_duckdb.py data/enriched_items_v2.json \
      --db data/saveme.duckdb --user-handle sandy4nasa
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb

SCHEMA_SQL = """
INSTALL spatial; LOAD spatial;
INSTALL vss; LOAD vss;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,               -- e.g. instagram handle, or phone/telegram id later
    auth_provider TEXT,                -- 'instagram_export' | 'whatsapp' | 'telegram' | 'instagram' | 'facebook'
    subscription_tier TEXT DEFAULT 'free',
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE SEQUENCE IF NOT EXISTS saved_places_id_seq;
CREATE TABLE IF NOT EXISTS saved_places (
    id BIGINT PRIMARY KEY DEFAULT nextval('saved_places_id_seq'),
    user_id TEXT REFERENCES users(id),
    source_url TEXT,
    platform TEXT,                     -- 'instagram' | 'tiktok' | 'maps' | 'generic'
    status TEXT,                       -- 'ready' | 'no_place_in_caption' | 'no_match' | 'needs_review_low_confidence' | error/skip states
    place_id TEXT,                     -- Google Places place_id, when matched
    name TEXT,
    lat DOUBLE,
    lng DOUBLE,
    geom GEOMETRY,                     -- generated from lat/lng via ST_Point for spatial queries
    address TEXT,
    rating DOUBLE,
    user_ratings_total INTEGER,
    place_types TEXT[],
    business_status TEXT,
    raw_caption TEXT,
    hashtags TEXT[],
    owner_username TEXT,
    owner_name TEXT,
    collection_name TEXT,              -- Instagram collection this was saved into, if any
    enrichment_query TEXT,
    enrichment_query_source TEXT,      -- 'location_marker' | 'title' | 'llm'
    llm_confidence DOUBLE,
    saved_at TIMESTAMP,
    expires_at TIMESTAMP,              -- for "expiring saves" (pop-up events), null for MVP
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS place_tags (
    place_id BIGINT REFERENCES saved_places(id),
    tag TEXT,
    confidence DOUBLE,
    PRIMARY KEY (place_id, tag)
);

CREATE TABLE IF NOT EXISTS embeddings (
    place_id BIGINT PRIMARY KEY REFERENCES saved_places(id),
    embedding FLOAT[768]                -- dimension matches whichever embedding model is chosen later (e.g. Gemini text-embedding-004 = 768)
);

CREATE TABLE IF NOT EXISTS notification_log (
    id BIGINT PRIMARY KEY DEFAULT nextval('saved_places_id_seq'),
    user_id TEXT REFERENCES users(id),
    place_id BIGINT REFERENCES saved_places(id),
    sent_at TIMESTAMP DEFAULT current_timestamp
);
"""


def to_iso(ts):
    return ts if ts else None


def load_items(con, items, user_id):
    con.execute("INSERT OR IGNORE INTO users (id, auth_provider) VALUES (?, ?)", [user_id, "instagram_export"])

    rows = []
    for it in items:
        status = it.get("enrichment_status", "unknown")
        rows.append((
            user_id,
            it.get("source_url"),
            it.get("platform"),
            status,
            it.get("place_id"),
            it.get("place_name") or it.get("name"),
            it.get("lat"),
            it.get("lng"),
            it.get("address"),
            it.get("rating"),
            it.get("user_ratings_total"),
            it.get("place_types") or [],
            it.get("business_status"),
            it.get("raw_caption"),
            it.get("hashtags") or [],
            it.get("owner_username"),
            it.get("owner_name"),
            it.get("collection_name"),
            it.get("enrichment_query"),
            it.get("enrichment_query_source"),
            it.get("llm_confidence"),
            to_iso(it.get("saved_at")),
        ))

    con.executemany(
        """
        INSERT INTO saved_places (
            user_id, source_url, platform, status, place_id, name, lat, lng,
            address, rating, user_ratings_total, place_types, business_status,
            raw_caption, hashtags, owner_username, owner_name, collection_name,
            enrichment_query, enrichment_query_source, llm_confidence, saved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    # Backfill geometry for rows that have lat/lng.
    con.execute("""
        UPDATE saved_places
        SET geom = ST_Point(lng, lat)
        WHERE lat IS NOT NULL AND lng IS NOT NULL AND geom IS NULL
    """)


def main():
    parser = argparse.ArgumentParser(description="Load enriched saved items into a local DuckDB file")
    parser.add_argument("input", help="Path to enriched_items_v2.json (or enriched_items.json)")
    parser.add_argument("--db", default="data/saveme.duckdb", help="DuckDB file path (created if missing)")
    parser.add_argument("--user-handle", default="me", help="User identifier to attribute these saves to")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        items = json.load(f)

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_SQL)
    load_items(con, items, args.user_handle)

    total = con.execute("SELECT count(*) FROM saved_places").fetchone()[0]
    ready = con.execute("SELECT count(*) FROM saved_places WHERE status = 'ready'").fetchone()[0]
    with_geom = con.execute("SELECT count(*) FROM saved_places WHERE geom IS NOT NULL").fetchone()[0]

    print(f"Loaded {len(items)} items into {db_path}")
    print(f"Total rows in saved_places: {total} (ready: {ready}, with geometry: {with_geom})")
    con.close()


if __name__ == "__main__":
    main()
