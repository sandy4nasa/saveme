#!/usr/bin/env python3
"""
ingest_pipeline.py

Real-time single-item ingestion pipeline -- the backend logic behind the
Android "Share to SaveMe" flow (IMPLEMENTATION_PLAN.md Section 14). Reuses
the exact same extraction/enrichment/tagging/embedding building blocks
already validated against the full 315-item batch dataset:

  enrich_places.enrich_item()          -> heuristic + Google Places lookup
  extract_places_llm.process_item()    -> Gemini caption-NLP fallback
  tag_places_llm.call_gemini()         -> category + tags classification
  embed_places.call_embed()            -> Gemini embedding

Why a single item at a time instead of batch: this runs synchronously inside
an HTTP request (few seconds, acceptable for a "you just shared a post"
UX), triggered by the Android Web Share Target flow. No DYI export needed --
this is the "ongoing new saves" ingestion path the batch scripts don't cover.

Caption source: Instagram's OS share sheet only passes a URL (no caption/
photo -- see IMPLEMENTATION_PLAN.md Section 14 for why scraping it server-side
doesn't work, login-walled). So the user optionally types a quick note/place
hint in the share-target mini page; that note is treated exactly like a
caption for the existing enrichment tiers. If left blank, the item is saved
with status='needs_manual_caption' so it's still visible (and editable later)
in the dashboard instead of being silently dropped.
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import enrich_item  # noqa: E402
from extract_places_llm import process_item  # noqa: E402
from tag_places_llm import call_gemini as call_gemini_tag  # noqa: E402
from embed_places import call_embed, build_embedding_text  # noqa: E402

HASHTAG_RE = re.compile(r"#(\w+)")


def extract_hashtags(text):
    return HASHTAG_RE.findall(text or "")


def insert_single_place(con, user_id, enriched):
    con.execute("INSTALL spatial; LOAD spatial")  # extensions must be installed+loaded per-connection
    con.execute("INSERT OR IGNORE INTO users (id, auth_provider) VALUES (?, ?)", [user_id, "share_target"])
    row = con.execute(
        """
        INSERT INTO saved_places (
            user_id, source_url, platform, status, place_id, name, lat, lng,
            address, rating, user_ratings_total, place_types, business_status,
            raw_caption, hashtags, owner_username, owner_name, collection_name,
            enrichment_query, enrichment_query_source, llm_confidence, saved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        [
            user_id,
            enriched.get("source_url"),
            enriched.get("platform"),
            enriched.get("enrichment_status", "unknown"),
            enriched.get("place_id"),
            enriched.get("place_name") or enriched.get("name"),
            enriched.get("lat"),
            enriched.get("lng"),
            enriched.get("address"),
            enriched.get("rating"),
            enriched.get("user_ratings_total"),
            enriched.get("place_types") or [],
            enriched.get("business_status"),
            enriched.get("raw_caption"),
            enriched.get("hashtags") or [],
            enriched.get("owner_username"),
            enriched.get("owner_name"),
            enriched.get("collection_name"),
            enriched.get("enrichment_query"),
            enriched.get("enrichment_query_source"),
            enriched.get("llm_confidence"),
            enriched.get("saved_at"),
        ],
    ).fetchone()
    con.execute("UPDATE saved_places SET geom = ST_Point(lng, lat) WHERE id = ? AND lat IS NOT NULL AND lng IS NOT NULL", [row[0]])
    return row[0]


def tag_and_embed_single(con, place_id, enriched, gemini_key):
    """Runs the tagging + embedding steps synchronously for one freshly
    inserted place. Best-effort: failures here don't roll back the saved
    place, they just leave it untagged/unembedded for a later batch rerun."""
    place = {
        "name": enriched.get("place_name") or enriched.get("name"),
        "place_types": enriched.get("place_types") or [],
        "raw_caption": enriched.get("raw_caption"),
        "hashtags": enriched.get("hashtags") or [],
    }
    tags_written, embedded = [], False

    try:
        result = call_gemini_tag(place, gemini_key)
        category = result.get("category", "other")
        con.execute("INSERT OR REPLACE INTO place_tags (place_id, tag, confidence) VALUES (?, ?, ?)",
                    [place_id, f"category:{category}", 1.0])
        for t in result.get("tags", []):
            tag, confidence = t.get("tag"), t.get("confidence", 0.5)
            if tag:
                con.execute("INSERT OR REPLACE INTO place_tags (place_id, tag, confidence) VALUES (?, ?, ?)",
                            [place_id, tag, confidence])
                tags_written.append(tag)
    except Exception as e:
        print(f"[ingest_pipeline] tagging failed for place_id={place_id}: {e}", file=sys.stderr)

    try:
        embed_input = {
            "name": place["name"],
            "address": enriched.get("address"),
            "raw_caption": enriched.get("raw_caption"),
            "tags": tags_written,
        }
        vec = call_embed(build_embedding_text(embed_input), gemini_key)
        con.execute("INSERT OR REPLACE INTO embeddings (place_id, embedding) VALUES (?, ?)", [place_id, vec])
        embedded = True
    except Exception as e:
        print(f"[ingest_pipeline] embedding failed for place_id={place_id}: {e}", file=sys.stderr)

    return tags_written, embedded


def ingest_single_item(con, user_id, source_url, note_text, gemini_key, places_key, owner_username=None):
    """Main entry point. Returns a dict summarizing what happened, suitable
    for the share-target confirmation page / API response."""
    note_text = (note_text or "").strip()
    item = {
        "source_url": source_url,
        "platform": "instagram",
        "raw_caption": note_text,
        "hashtags": extract_hashtags(note_text),
        "title": "",
        "owner_username": owner_username,
        "owner_name": None,
        "collection_name": None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }

    enriched = _run_enrichment(item, note_text, gemini_key, places_key)
    place_id = insert_single_place(con, user_id, enriched)

    tags, embedded = [], False
    if enriched["enrichment_status"] == "ready":
        tags, embedded = tag_and_embed_single(con, place_id, enriched, gemini_key)

    return {
        "place_id": place_id,
        "status": enriched["enrichment_status"],
        "name": enriched.get("place_name") or enriched.get("name"),
        "address": enriched.get("address"),
        "tags": tags,
        "tagged": bool(tags),
        "embedded": embedded,
    }


def _run_enrichment(item, note_text, gemini_key, places_key):
    """Shared by ingest_single_item (new share) and retry_single_item
    (re-running an existing 'needs review' item with a better note)."""
    if not note_text:
        return {**item, "enrichment_status": "needs_manual_caption"}
    enriched = enrich_item(item, places_key)
    if enriched["enrichment_status"] in ("skipped_needs_llm_extraction", "no_match"):
        enriched = process_item(enriched, gemini_key, places_key)
    return enriched


def update_existing_place(con, row_id, enriched):
    con.execute("INSTALL spatial; LOAD spatial")
    con.execute(
        """
        UPDATE saved_places SET
            status = ?, place_id = ?, name = ?, lat = ?, lng = ?, address = ?, rating = ?,
            user_ratings_total = ?, place_types = ?, business_status = ?, raw_caption = ?,
            hashtags = ?, enrichment_query = ?, enrichment_query_source = ?, llm_confidence = ?
        WHERE id = ?
        """,
        [
            enriched.get("enrichment_status", "unknown"),
            enriched.get("place_id"),
            enriched.get("place_name") or enriched.get("name"),
            enriched.get("lat"),
            enriched.get("lng"),
            enriched.get("address"),
            enriched.get("rating"),
            enriched.get("user_ratings_total"),
            enriched.get("place_types") or [],
            enriched.get("business_status"),
            enriched.get("raw_caption"),
            enriched.get("hashtags") or [],
            enriched.get("enrichment_query"),
            enriched.get("enrichment_query_source"),
            enriched.get("llm_confidence"),
            row_id,
        ],
    )
    con.execute(
        "UPDATE saved_places SET geom = ST_Point(lng, lat) WHERE id = ? AND lat IS NOT NULL AND lng IS NOT NULL",
        [row_id],
    )


def retry_single_item(con, user_id, row_id, note_text, gemini_key, places_key):
    """Re-runs enrichment for an existing saved_places row that didn't reach
    status='ready' the first time (e.g. a note without a specific place name),
    using a new/updated note supplied from the 'Needs review' screen. Updates
    the row in place instead of inserting a duplicate. Returns None if the
    row doesn't exist or doesn't belong to user_id (ownership-checked)."""
    row = con.execute(
        "SELECT source_url, platform, owner_username FROM saved_places WHERE id = ? AND user_id = ?",
        [row_id, user_id],
    ).fetchone()
    if not row:
        return None
    source_url, platform, owner_username = row
    note_text = (note_text or "").strip()
    item = {
        "source_url": source_url,
        "platform": platform or "instagram",
        "raw_caption": note_text,
        "hashtags": extract_hashtags(note_text),
        "title": "",
        "owner_username": owner_username,
        "owner_name": None,
        "collection_name": None,
    }
    enriched = _run_enrichment(item, note_text, gemini_key, places_key)
    update_existing_place(con, row_id, enriched)

    tags, embedded = [], False
    if enriched["enrichment_status"] == "ready":
        # Clear any stale tags/embedding from a previous attempt before re-tagging.
        con.execute("DELETE FROM place_tags WHERE place_id = ?", [row_id])
        con.execute("DELETE FROM embeddings WHERE place_id = ?", [row_id])
        tags, embedded = tag_and_embed_single(con, row_id, enriched, gemini_key)

    return {
        "place_id": row_id,
        "status": enriched["enrichment_status"],
        "name": enriched.get("place_name") or enriched.get("name"),
        "address": enriched.get("address"),
        "tags": tags,
        "tagged": bool(tags),
        "embedded": embedded,
    }
