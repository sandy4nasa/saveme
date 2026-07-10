#!/usr/bin/env python3
"""
export_map_data.py

Exports all "ready" saved places (with lat/lng + tags) from DuckDB into a flat
JSON file for the web map/dashboard (web/index.html) to fetch and render.
Re-run this any time the DB changes (new places enriched/tagged) and refresh
the browser -- no rebuild step needed.

Usage:
  python3 scripts/export_map_data.py --db data/saveme.duckdb --out web/map_data.json
"""

import argparse
import json
from pathlib import Path

import duckdb


def build_places_list(con, user_id=None):
    """Core query shared by the CLI static-export path and the dynamic
    /api/places route in serve_app.py. Returns a list of place dicts scoped
    to one user (or all users if user_id is None)."""
    where = ["sp.status = 'ready'", "sp.lat IS NOT NULL", "sp.lng IS NOT NULL"]
    params = []
    if user_id:
        where.append("sp.user_id = ?")
        params.append(user_id)

    query = f"""
        SELECT sp.id, sp.name, sp.lat, sp.lng, sp.address, sp.rating, sp.user_ratings_total,
               sp.source_url, sp.collection_name, sp.owner_username,
               list(pt.tag) FILTER (WHERE pt.tag NOT LIKE 'category:%') AS tags,
               list(pt.tag) FILTER (WHERE pt.tag LIKE 'category:%') AS category_tags
        FROM saved_places sp
        LEFT JOIN place_tags pt ON pt.place_id = sp.id
        WHERE {' AND '.join(where)}
        GROUP BY sp.id, sp.name, sp.lat, sp.lng, sp.address, sp.rating, sp.user_ratings_total,
                 sp.source_url, sp.collection_name, sp.owner_username
        ORDER BY sp.id
    """
    rows = con.execute(query, params).fetchall()
    cols = ["id", "name", "lat", "lng", "address", "rating", "user_ratings_total",
            "source_url", "collection_name", "owner_username", "tags", "category_tags"]

    places = []
    for r in rows:
        d = dict(zip(cols, r))
        cat_tags = d.pop("category_tags") or []
        category = cat_tags[0].replace("category:", "") if cat_tags else "other"
        d["category"] = category
        d["tags"] = d["tags"] or []
        places.append(d)
    return places


def list_needs_review(con, user_id):
    """Places that were saved but never reached status='ready' (e.g. the
    share note didn't contain a specific enough place name for our
    extraction pipeline to confidently match a Google Places result). These
    are invisible from the main map/chat (which only show 'ready' places) --
    surfaced separately in /review so the user can add a better note and
    retry via /api/retry."""
    rows = con.execute(
        """
        SELECT id, source_url, raw_caption, status, saved_at
        FROM saved_places
        WHERE user_id = ? AND status != 'ready'
        ORDER BY saved_at DESC
        """,
        [user_id],
    ).fetchall()
    cols = ["id", "source_url", "raw_caption", "status", "saved_at"]
    return [dict(zip(cols, r)) for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Export saved_places -> JSON for the web map")
    parser.add_argument("--db", default="data/saveme.duckdb")
    parser.add_argument("--out", default="web/map_data.json")
    parser.add_argument("--user", default=None, help="Filter to one user_id (default: all)")
    args = parser.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    places = build_places_list(con, args.user)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(places, indent=2, default=str))

    categories = sorted(set(p["category"] for p in places))
    print(f"Exported {len(places)} places to {out_path}")
    print(f"Categories: {', '.join(categories)}")

    con.close()


if __name__ == "__main__":
    main()
