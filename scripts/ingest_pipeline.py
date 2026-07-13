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
from fetch_instagram_caption import fetch_caption  # noqa: E402
from analyze_video_llm import analyze_video  # noqa: E402

HASHTAG_RE = re.compile(r"#(\w+)")


def extract_hashtags(text):
    return HASHTAG_RE.findall(text or "")


def resolve_caption(source_url, note_text, owner_username=None):
    """Auto-fetches the real Instagram caption (best-effort, see
    fetch_instagram_caption.py) and combines it with any manual note the
    user supplied. Scraped caption comes first (it's the actual post
    content); the manual note -- if any -- is appended as extra user
    context/clarification. Falls back to the manual note alone if scraping
    is unavailable (private post, network error, markup change, etc.)."""
    note_text = (note_text or "").strip()
    fetched = fetch_caption(source_url)

    if not fetched:
        return note_text, owner_username

    caption = fetched["caption"]
    if note_text and note_text not in caption:
        caption = f"{caption}\n\n{note_text}"
    resolved_owner = owner_username or fetched.get("owner_username")
    return caption, resolved_owner


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


TAGGABLE_STATUSES = {"ready", "saved_no_place"}


def _place_result_dict(place_id, enriched):
    return {
        "place_id": place_id,
        "status": enriched["enrichment_status"],
        "name": enriched.get("place_name") or enriched.get("name"),
        "address": enriched.get("address"),
    }


def _insert_and_process(con, user_id, enriched, gemini_key):
    """Inserts one or more saved_places rows for a single enrichment result
    and tags/embeds each that's in a taggable state. Returns a list of result
    dicts (length 1 in the common case, or one per venue when enrichment_status
    == 'multi_place' -- e.g. a roundup/itinerary post naming several distinct
    real places, each gets its own row/tags/embedding/map pin)."""
    to_insert = enriched.get("resolved_places") if enriched["enrichment_status"] == "multi_place" else [enriched]

    results = []
    for one in to_insert:
        place_id = insert_single_place(con, user_id, one)
        tags, embedded = [], False
        if one["enrichment_status"] in TAGGABLE_STATUSES:
            tags, embedded = tag_and_embed_single(con, place_id, one, gemini_key)
        result = _place_result_dict(place_id, one)
        result.update({"tags": tags, "tagged": bool(tags), "embedded": embedded})
        results.append(result)
    return results


def ingest_single_item(con, user_id, source_url, note_text, gemini_key, places_key, owner_username=None):
    """Main entry point. Returns a dict summarizing what happened, suitable
    for the share-target confirmation page / API response. The primary
    (first) resolved place is at the top level for backward compatibility;
    if a post named more than one distinct real place (multi_place), the
    rest are under "additional_places"."""
    caption, owner_username = resolve_caption(source_url, note_text, owner_username)
    item = {
        "source_url": source_url,
        "platform": "instagram",
        "raw_caption": caption,
        "hashtags": extract_hashtags(caption),
        "title": "",
        "owner_username": owner_username,
        "owner_name": None,
        "collection_name": None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }

    enriched = _run_enrichment(item, caption, gemini_key, places_key)
    results = _insert_and_process(con, user_id, enriched, gemini_key)

    primary, extras = results[0], results[1:]
    primary["additional_places"] = extras
    return primary


def _run_enrichment(item, note_text, gemini_key, places_key):
    """Shared by ingest_single_item (new share) and retry_single_item
    (re-running an existing 'needs review' item with a better note)."""
    if not note_text:
        return {**item, "enrichment_status": "needs_manual_caption"}
    enriched = enrich_item(item, places_key)
    if enriched["enrichment_status"] in ("skipped_needs_llm_extraction", "no_match"):
        enriched = process_item(enriched, gemini_key, places_key)
    if enriched["enrichment_status"] == "no_place_in_caption":
        # Last resort: the real caption exists and plausibly names a place,
        # but extraction/Places lookup couldn't confidently resolve it --
        # try analyzing the actual video (signboards, spoken address,
        # on-screen text). Best-effort; only runs if HIKER_API_KEY is
        # configured, and never fails the whole ingestion (see
        # analyze_video_llm.py). Falls back to the caption-only result if
        # this doesn't succeed. NOT run for `saved_no_place` -- that status
        # means Gemini is confident the post isn't about a place at all
        # (recipe, DIY/craft, product post, etc.), so a video-analysis
        # attempt would just waste a paid API call on content with nothing
        # to find.
        video_enriched = analyze_video(enriched, gemini_key, places_key)
        if video_enriched:
            enriched = video_enriched
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
    row doesn't exist or doesn't belong to user_id (ownership-checked).

    If the new note now resolves to multiple distinct places (multi_place --
    e.g. the user's better note turned out to describe a multi-stop
    itinerary), the first resolved place updates this existing row and any
    additional ones are inserted as new rows, same as a fresh multi-place
    share."""
    row = con.execute(
        "SELECT source_url, platform, owner_username FROM saved_places WHERE id = ? AND user_id = ?",
        [row_id, user_id],
    ).fetchone()
    if not row:
        return None
    source_url, platform, owner_username = row
    caption, owner_username = resolve_caption(source_url, note_text, owner_username)
    item = {
        "source_url": source_url,
        "platform": platform or "instagram",
        "raw_caption": caption,
        "hashtags": extract_hashtags(caption),
        "title": "",
        "owner_username": owner_username,
        "owner_name": None,
        "collection_name": None,
    }
    enriched = _run_enrichment(item, caption, gemini_key, places_key)

    to_apply = enriched.get("resolved_places") if enriched["enrichment_status"] == "multi_place" else [enriched]
    primary, extras = to_apply[0], to_apply[1:]

    update_existing_place(con, row_id, primary)
    con.execute("DELETE FROM place_tags WHERE place_id = ?", [row_id])
    con.execute("DELETE FROM embeddings WHERE place_id = ?", [row_id])
    tags, embedded = [], False
    if primary["enrichment_status"] in TAGGABLE_STATUSES:
        tags, embedded = tag_and_embed_single(con, row_id, primary, gemini_key)

    result = _place_result_dict(row_id, primary)
    result.update({"tags": tags, "tagged": bool(tags), "embedded": embedded})
    result["additional_places"] = _insert_and_process(con, user_id, {"enrichment_status": "multi_place", "resolved_places": extras}, gemini_key) if extras else []
    return result

