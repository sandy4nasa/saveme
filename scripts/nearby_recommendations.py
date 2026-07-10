#!/usr/bin/env python3
"""
nearby_recommendations.py

Recommends places near a given saved place, in two flavours:

  1. saved_nearby   -- other places this same user already saved that happen
                       to be geographically close (pure DB query, no API
                       cost, uses a haversine distance formula in SQL).
  2. discover_nearby -- brand-new places (not yet saved by this user)
                       discovered via the Google Places API (New) Nearby
                       Search endpoint, restricted to the same broad category
                       as the source place so results stay relevant (e.g.
                       other cafes near a saved cafe, not a shoe store).

Used by GET /api/nearby (serve_app.py, triggered when a user selects a place
on the map) and by the chat endpoint (attached to the top-ranked candidate
of a chat answer, so "tell me about X" answers also surface a few nearby
options worth checking out).
"""

import json
import urllib.error
import urllib.request

PLACES_SEARCHNEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
NEARBY_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.googleMapsUri"
)

# Maps our internal broad category (see tag_places_llm.py's classification
# prompt) to Google Places (New) includedTypes. Categories with no sensible
# "discover something similar nearby" meaning (real_estate listings, "other")
# are intentionally omitted -- discover_nearby just returns [] for those;
# saved_nearby (pure local DB query) still works for every category.
CATEGORY_TO_PLACE_TYPES = {
    "restaurant": ["restaurant"],
    "cafe": ["cafe", "bakery"],
    "travel_destination": ["tourist_attraction"],
    "shopping": ["shopping_mall", "store"],
    "nature": ["park"],
    "activity": ["tourist_attraction", "amusement_park"],
}

DEFAULT_RADIUS_M = 2000
DEFAULT_LIMIT = 5
DEFAULT_RADIUS_KM = 5


def _category_for_place(con, place_id):
    row = con.execute(
        "SELECT tag FROM place_tags WHERE place_id = ? AND tag LIKE 'category:%' LIMIT 1",
        [place_id],
    ).fetchone()
    return row[0].replace("category:", "") if row else "other"


def find_nearby_saved(con, user_id, place_id, lat, lng, radius_km=DEFAULT_RADIUS_KM, limit=DEFAULT_LIMIT):
    """Other 'ready' places already saved by this user within radius_km,
    nearest-first. Plain haversine-in-SQL rather than the spatial
    extension's ST_DWithin -- keeps this independent of whether `geom` was
    backfilled for a given row, and radius_km is small enough that the
    great-circle vs. planar distinction doesn't matter here."""
    rows = con.execute(
        """
        SELECT sp.id, sp.name, sp.lat, sp.lng, sp.address, sp.rating,
               (
                 6371 * acos(
                   least(1.0, greatest(-1.0,
                     cos(radians(?)) * cos(radians(sp.lat)) * cos(radians(sp.lng) - radians(?))
                     + sin(radians(?)) * sin(radians(sp.lat))
                   ))
                 )
               ) AS distance_km,
               list(pt.tag) FILTER (WHERE pt.tag LIKE 'category:%') AS category_tags
        FROM saved_places sp
        LEFT JOIN place_tags pt ON pt.place_id = sp.id
        WHERE sp.user_id = ? AND sp.status = 'ready' AND sp.id != ?
              AND sp.lat IS NOT NULL AND sp.lng IS NOT NULL
        GROUP BY sp.id, sp.name, sp.lat, sp.lng, sp.address, sp.rating
        HAVING distance_km <= ?
        ORDER BY distance_km ASC
        LIMIT ?
        """,
        [lat, lng, lat, user_id, place_id, radius_km, limit],
    ).fetchall()
    cols = ["id", "name", "lat", "lng", "address", "rating", "distance_km", "category_tags"]
    results = []
    for r in rows:
        d = dict(zip(cols, r))
        cat_tags = d.pop("category_tags") or []
        d["category"] = cat_tags[0].replace("category:", "") if cat_tags else "other"
        d["distance_km"] = round(d["distance_km"], 2)
        results.append(d)
    return results


def find_nearby_discover(lat, lng, category, places_key, exclude_google_place_ids=None,
                          radius_m=DEFAULT_RADIUS_M, limit=DEFAULT_LIMIT):
    """New (not-yet-saved) places near (lat, lng) via Places API Nearby
    Search, restricted to Google types matching `category`. Returns []
    (no API call) for categories with no sensible discover mapping."""
    included_types = CATEGORY_TO_PLACE_TYPES.get(category)
    if not included_types:
        return []
    exclude_google_place_ids = exclude_google_place_ids or set()

    body = json.dumps({
        "includedTypes": included_types,
        "maxResultCount": 20,  # over-fetch; we filter exclusions client-side then cap to `limit`
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius_m,
            }
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        PLACES_SEARCHNEARBY_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": places_key,
            "X-Goog-FieldMask": NEARBY_FIELD_MASK,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return []  # best-effort -- discover recommendations are a nice-to-have, never fatal

    results = []
    for p in data.get("places", []):
        gid = p.get("id")
        if not gid or gid in exclude_google_place_ids:
            continue
        loc = p.get("location", {})
        results.append({
            "google_place_id": gid,
            "name": (p.get("displayName") or {}).get("text"),
            "address": p.get("formattedAddress"),
            "lat": loc.get("latitude"),
            "lng": loc.get("longitude"),
            "rating": p.get("rating"),
            "user_ratings_total": p.get("userRatingCount"),
            "maps_url": p.get("googleMapsUri"),
        })
        if len(results) >= limit:
            break
    return results


def get_recommendations(con, user_id, place_id, places_key):
    """Main entry point for GET /api/nearby. Returns None if the place
    doesn't exist / isn't owned by user_id / has no coordinates yet."""
    row = con.execute(
        "SELECT lat, lng FROM saved_places WHERE id = ? AND user_id = ? AND status = 'ready'",
        [place_id, user_id],
    ).fetchone()
    if not row or row[0] is None or row[1] is None:
        return None
    lat, lng = row
    category = _category_for_place(con, place_id)

    saved_nearby = find_nearby_saved(con, user_id, place_id, lat, lng)

    exclude_ids = {
        r[0] for r in con.execute(
            "SELECT place_id FROM saved_places WHERE user_id = ? AND place_id IS NOT NULL",
            [user_id],
        ).fetchall()
    }
    discover_nearby = find_nearby_discover(lat, lng, category, places_key, exclude_google_place_ids=exclude_ids)

    return {"saved_nearby": saved_nearby, "discover_nearby": discover_nearby}


def get_recommendations_for_coords(con, user_id, place_id, lat, lng, category, places_key):
    """Variant used by the chat endpoint, where the caller already has the
    top candidate's lat/lng/category in hand (from fetch_candidate_pool) and
    shouldn't re-query the DB for them."""
    saved_nearby = find_nearby_saved(con, user_id, place_id, lat, lng)
    exclude_ids = {
        r[0] for r in con.execute(
            "SELECT place_id FROM saved_places WHERE user_id = ? AND place_id IS NOT NULL",
            [user_id],
        ).fetchall()
    }
    discover_nearby = find_nearby_discover(lat, lng, category, places_key, exclude_google_place_ids=exclude_ids)
    return {"saved_nearby": saved_nearby, "discover_nearby": discover_nearby}
