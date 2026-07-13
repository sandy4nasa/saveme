# SaveMe — Implementation Plan

App concept: Organize scattered saved places (Instagram reels, WhatsApp forwards, FB posts, Google Maps) into one searchable, conversational map. Ask "where should I eat today?" and get answers from your own saved content. Get proximity nudges when near a saved spot.

This doc is the step-by-step build plan, the "moving parts" (env vars / API keys / config) needed for the AI pipeline, the data model, and the onboarding flow.

---

## 1. Phased Build Plan (MVP → V2)

### Phase 0 — Foundations (Week 1)
1. Register accounts / get API keys (see Section 2 — do this first, some approvals take days):
   - Meta for Developers app (Facebook/Instagram Login OAuth)
   - Twilio (WhatsApp Business API) or Telegram BotFather token
   - Anthropic or Google AI Studio (Claude/Gemini API key)
   - Google Cloud project + Places API key
   - Supabase or Railway project (Postgres + pgvector)
   - Firebase project (Auth + push, optional for MVP)
2. Repo scaffold: single Node.js/TypeScript (or Python/FastAPI) backend service, no microservices yet.
3. Provision Postgres with `pgvector` and `postgis` extensions enabled.

### Phase 1 — Ingestion Bot (Week 1–2)
1. Stand up WhatsApp (Twilio) or Telegram bot webhook endpoint.
2. On incoming message: extract URL(s), store raw message + user identifier (phone/telegram id) in `saved_places` with status `pending`.
3. Reply immediately: "Got it, processing..." (keeps UX responsive while async work happens).

### Phase 2 — Extraction Waterfall (Week 2)
1. **URL normalization** — detect platform (Instagram/TikTok/Maps/generic) via regex/domain match.
2. **Scrape** the public URL (Playwright headless, or oEmbed endpoint where available) → caption text, geotag if present, media URL.
3. **Geotag check** — if lat/lng present in scraped metadata, done, skip to enrichment.
4. **Caption NLP fallback** — call LLM (Claude/Gemini) with a structured prompt: extract place name + city from caption text/hashtags/@mentions. Return strict JSON.
5. *(Defer to V2)* Multimodal video/audio extraction — only if caption NLP fails.
6. **Enrichment** — call Google Places API (Text Search / Find Place) with the extracted name to get place_id, lat/lng, address, hours, rating.
7. **Tagging** — LLM call classifies the place (kid-friendly, vegetarian, outdoor-seating, etc.) → store as `place_tags`.
8. Update `saved_places` row status → `ready`, notify user ("Added: [Place Name] 📍").

### Phase 3 — Chat Search / RAG (Week 2–3)
1. On each `ready` place, generate an embedding (OpenAI/Gemini embedding API) from a text summary (name + tags + caption) → store in `embeddings` (pgvector column).
2. Chat endpoint: user question → embed query → `pgvector` cosine similarity search scoped to `user_id` → top-K candidates → pass candidates + question to LLM → generate grounded natural-language answer citing the source place(s).
3. Expose via same bot (WhatsApp/Telegram) — no separate UI needed for MVP.

### Phase 4 — Map View (Week 3)
1. Simple authenticated web page (no native app yet): list/map of the user's `saved_places`, filter by tag.
2. Use Mapbox GL JS or Google Maps JS SDK for pins.

### Phase 5 — Proximity Nudge (Week 3–4)
1. User shares location once (simple web geolocation prompt) or periodically via bot command ("here").
2. Cron job (every 15–30 min) — PostGIS radius query: any `saved_places` within X meters of last known location, not already notified today.
3. Anti-fatigue rules: max 1–2 notifications/day, time-of-day relevance (breakfast spot only 7–10am), dedupe via `notification_log`.
4. Send push (FCM) or WhatsApp/Telegram message.

### Phase 6 — Fake Paywall + Metrics (Week 4)
1. "Upgrade to Pro" button/message → track clicks (no real billing yet).
2. Metrics to log: % users forwarding >1 link unprompted, repeat chat usage, nudge reaction (reply vs ignored), upgrade click-through.

### V2 (post-validation)
- Native app (React Native/Flutter) + Share Sheet extension (skip app-store friction).
- Instagram/Facebook "Download Your Information" (DYI) export parser for bulk historical import (see Section 5).
- Multimodal video/audio extraction fallback.
- Collaborative buckets, route optimization, real billing (Stripe/RevenueCat), affiliate booking links, B2B analytics.

---

## 2. Moving Parts — AI & API Variables Required

These are the external services and config values ("moving parts") the app depends on. Treat as environment variables / secrets — never hardcode.

| Variable | Purpose | Where to get it |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` or `google` — which LLM to use for NLP/tagging/chat | your choice |
| `ANTHROPIC_API_KEY` | Claude API access (caption NLP, tagging, chat answers) | console.anthropic.com |
| `GOOGLE_AI_API_KEY` | Gemini API access (alt/backup LLM, also good for multimodal in V2) | aistudio.google.com |
| `EMBEDDING_PROVIDER` | `openai` or `gemini` — which embedding model | your choice |
| `OPENAI_API_KEY` | Embeddings for RAG search (if using OpenAI embeddings) | platform.openai.com |
| `GOOGLE_PLACES_API_KEY` | Enrichment — lat/lng, hours, ratings, phone from place name | Google Cloud Console → Places API |
| `MAPBOX_ACCESS_TOKEN` | Map rendering on the web dashboard | mapbox.com |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_NUMBER` | WhatsApp Business API bot | twilio.com (WhatsApp sandbox for dev, approved sender for prod) |
| `TELEGRAM_BOT_TOKEN` | Alt/simpler bot channel (no approval wait, good for fastest MVP) | BotFather on Telegram |
| `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` | "Login with Instagram/Facebook" OAuth (auth only, not data access) | developers.facebook.com |
| `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET` | Same, Instagram-specific OAuth if using IG Login product separately | developers.facebook.com |
| `DATABASE_URL` | Postgres connection string (with `pgvector` + `postgis` extensions) | Supabase / Railway / Neon |
| `FIREBASE_PROJECT_ID` / `FIREBASE_SERVICE_ACCOUNT_JSON` | Push notifications (FCM) + optional Auth | Firebase console |
| `SCRAPER_PROXY_KEY` | *(defer)* Apify/Bright Data key if public scraping gets rate-limited/blocked | apify.com / brightdata.com |
| `JWT_SECRET` / `SESSION_SECRET` | App session/auth token signing | generate locally (32+ random bytes) |
| `APP_BASE_URL` | Public URL of your deployed backend (for webhook callbacks, OAuth redirect URIs) | your hosting provider |

**Per-request "variables" the pipeline itself needs at runtime** (not secrets, but data the AI steps consume):
- `user_id` — scopes every embedding search and place record (critical: RAG must never leak across users)
- `source_url` — the raw shared link
- `platform` — detected source (instagram/tiktok/maps/generic) — changes which scraper/parser path runs
- `caption_text` — raw scraped caption, fed to NLP extraction prompt
- `geotag` (optional) — if present, skips LLM extraction entirely (cheapest path)
- `extraction_confidence` — score returned by the LLM extraction step; low-confidence rows should be flagged for user confirmation rather than silently enriching bad data
- `user_query` — the natural-language question in chat search
- `last_known_location` (lat/lng) — required input for the proximity engine
- `time_of_day` — used to filter which nudges are relevant (breakfast vs dinner spots)

---

## 3. Data Model — DuckDB (local-first, MVP/validation phase)

**Decision: use a single-file DuckDB database instead of hosted Postgres for now.** No server/hosting to stand up — the whole datastore is one file (`data/saveme.duckdb`) you can query directly with the `duckdb` CLI or Python/Node clients. It has direct equivalents for everything the original Postgres design needed:

| Need | Postgres (original plan) | DuckDB (current) |
|---|---|---|
| Geo queries (radius search, distance) | PostGIS `geography` + `ST_DWithin` | `spatial` extension — `ST_Point`, `ST_Distance_Sphere` |
| Vector similarity (RAG chat search) | `pgvector` | `vss` extension — `FLOAT[]` column + HNSW index |
| Hosting | Supabase/Railway/Neon (external service) | Local file, zero hosting |

**When to migrate to Postgres**: once you need concurrent multi-writer access at real production scale (many simultaneous users/bots writing), or managed backups/replication. The schema below is designed to port 1:1 — same tables/columns, so migration is a straightforward `COPY`/export job, not a redesign.

Implemented in `scripts/load_to_duckdb.py`:
```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,               -- instagram handle for now; phone/telegram id later
    auth_provider TEXT,
    subscription_tier TEXT DEFAULT 'free',
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE saved_places (
    id BIGINT PRIMARY KEY DEFAULT nextval('saved_places_id_seq'),
    user_id TEXT REFERENCES users(id),
    source_url TEXT, platform TEXT, status TEXT,
    place_id TEXT, name TEXT, lat DOUBLE, lng DOUBLE,
    geom GEOMETRY,                     -- ST_Point(lng, lat), backfilled on load
    address TEXT, rating DOUBLE, user_ratings_total INTEGER,
    place_types TEXT[], business_status TEXT,
    raw_caption TEXT, hashtags TEXT[], owner_username TEXT, owner_name TEXT,
    collection_name TEXT,
    enrichment_query TEXT, enrichment_query_source TEXT, llm_confidence DOUBLE,
    saved_at TIMESTAMP, expires_at TIMESTAMP, created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE place_tags (
    place_id BIGINT REFERENCES saved_places(id), tag TEXT, confidence DOUBLE,
    PRIMARY KEY (place_id, tag)
);

CREATE TABLE embeddings (
    place_id BIGINT PRIMARY KEY REFERENCES saved_places(id),
    embedding FLOAT[768]               -- dimension matches chosen embedding model
);

CREATE TABLE notification_log (
    id BIGINT PRIMARY KEY, user_id TEXT REFERENCES users(id),
    place_id BIGINT REFERENCES saved_places(id), sent_at TIMESTAMP DEFAULT current_timestamp
);
```

**Usage:**
```bash
python3 scripts/load_to_duckdb.py data/enriched_items_v2.json --db data/saveme.duckdb --user-handle sandy4nasa
```

**Validated against the real dataset** — loaded all 315 rows (250 with geometry). Confirmed both core query patterns work correctly:
- Proximity search: `ST_Distance_Sphere(geom, ST_Point(lng, lat))` correctly found the nearest saved cafes/restaurants within meters of a test point in Jayanagar, Bengaluru — this is the exact query the Proximity Engine (Section 1, Phase 5) needs.
- Rating/filter queries (top-rated places, breakdown by collection) all work with plain SQL.

Original Postgres schema (kept below for reference / future migration target):

```sql
create extension if not exists postgis;
create extension if not exists vector;

create table users (
  id uuid primary key default gen_random_uuid(),
  phone_or_telegram_id text unique,
  auth_provider text,           -- 'whatsapp' | 'telegram' | 'instagram' | 'facebook'
  subscription_tier text default 'free',
  created_at timestamptz default now()
);

create table saved_places (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id),
  source_url text not null,
  platform text,                -- 'instagram' | 'tiktok' | 'maps' | 'generic'
  status text default 'pending', -- 'pending' | 'ready' | 'failed'
  name text,
  lat double precision,
  lng double precision,
  geom geography(Point, 4326),  -- generated from lat/lng for PostGIS radius queries
  address text,
  raw_caption text,
  extraction_confidence numeric,
  expires_at timestamptz,        -- for "expiring saves" (pop-up events)
  created_at timestamptz default now()
);

create table place_tags (
  place_id uuid references saved_places(id),
  tag text,
  confidence numeric,
  primary key (place_id, tag)
);

create table embeddings (
  place_id uuid primary key references saved_places(id),
  embedding vector(1536)         -- dimension depends on embedding model chosen
);

create table notification_log (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id),
  place_id uuid references saved_places(id),
  sent_at timestamptz default now()
);

create index on saved_places using gist (geom);
create index on embeddings using ivfflat (embedding vector_cosine_ops);
```

---

## 4. Onboarding Flow (Auth vs Data Import — kept as two distinct steps)

```
Screen 1: Welcome
   "Turn your scattered saves into one searchable map."
   [Continue with Instagram]  [Continue with Phone/WhatsApp]
        │
        ▼
Screen 2: Account created (auth only — profile name/photo, no saved-post access yet)
        │
        ▼
Screen 3: "Import your existing saves" (optional, can skip)
   Explains: Instagram/Facebook don't allow apps to auto-read your Saved posts.
   Button: [How to export your saved posts] → deep link to Meta's
           Accounts Center → "Download Your Information" flow.
   User uploads the resulting .zip once it's ready (can take minutes to 48h).
        │
        ▼
Screen 4: Bulk import processing
   Backend parses saved_posts.json/collections.json → extracts post URLs →
   runs each through the extraction waterfall (Section 1, Phase 2) as a batch job.
        │
        ▼
Screen 5: "Keep saving going forward"
   Prompt to save the WhatsApp number / Telegram bot as a contact,
   or (V2) install the app + enable native Share Sheet.
```

Key UX point: never conflate "Login with Instagram" (auth convenience) with "Import saved posts" (separate manual export/upload step) — users will assume login = auto-sync, which is not possible with Meta's APIs.

---

## 5. Notes on Meta API Constraint (why the flow above looks like this)

- Instagram Graph API / Facebook Graph API do **not** expose a user's Saved posts/Collections at any permission level — confirmed, not a review gate, the endpoint doesn't exist.
- Compliant path: Meta's own "Download Your Information" export (JSON/HTML, includes saved posts) — user-initiated, ToS-compliant, one-time bulk import.
- Ongoing new saves: WhatsApp/Telegram forwarding (MVP) or native Share Sheet extension (V2, requires app store presence).
- Avoid browser-extension DOM scraping of the Saved tab — against platform ToS, fragile, risks account flags.

---

## 6. DYI Export Parser — Implemented

`scripts/parse_instagram_export.py` is a working parser (stdlib-only, no dependencies) that turns an Instagram "Download Your Information" export into normalized JSON ready for the extraction waterfall (Section 1, Phase 2).

**What it handles:**
- Accepts either the raw `.zip` export or an already-extracted folder.
- Reads `your_instagram_activity/saved/saved_posts.json` (direct saves) and `saved_collections.json` (saves organized into named collections).
- Fixes Instagram's mojibake encoding bug (captions/emoji are stored as UTF-8 bytes mis-decoded as Latin-1 — the script round-trips them back to correct text).
- Extracts per item: `source_url`, `platform` (detected from domain), `title`, `raw_caption`, `hashtags`, `owner_name`, `owner_username`, `fbid`, `saved_at` (ISO timestamp), `collection_name` (null if it's just a direct save, not in any collection).
- Dedupes items that appear in both the direct-saves file and a collection (same post saved + organized), keeping the collection name.
- Prints a summary (counts by platform, collections found).

**Usage:**
```bash
python3 scripts/parse_instagram_export.py <path_to_export.zip> --out data/saved_items.json
```

**Validated against a real export** (`instagram-sandy4nasa-2026-07-08-cDKKqF4f.zip`): correctly parsed 315 unique saved items (84 direct saves, plus items filed into 4 collections — "Food Street", "Travel", "Properties", "Jewelry"), with clean emoji/accented-text decoding and no duplicate URLs.

**Output** (`data/saved_items.json`) is a JSON array where each object maps directly onto a `saved_places` row (Section 3) — feed it straight into the Phase 2 extraction waterfall: URL → scrape/geotag check → caption already present here (skips re-scraping for caption!) → Places enrichment → tagging.

**Google Drive input:** if the export lives in Drive rather than local disk, download it first via the Drive API (`files.get_media`) to a local temp path, then run this same script against that path — parsing logic is unchanged, only the download step differs. A thin wrapper (`scripts/fetch_from_drive.py`, not yet built) would just need `GOOGLE_DRIVE_CREDENTIALS_JSON` / a Drive file ID and could pipe its output straight into this parser.

---

## 8. Google Places Enrichment — Implemented

`scripts/enrich_places.py` takes `data/saved_items.json` (parser output) and enriches each item with real-world place data via the **Places API (New)** `searchText` endpoint.

**Candidate place-name extraction (interim heuristic, ahead of building the LLM step):**
1. Look for a `📍` location-marker emoji in the caption — the text right after it is almost always the venue name/address (extremely common IG convention in this dataset).
2. Fall back to the post `title` field if non-empty.
3. Otherwise mark `skipped_needs_llm_extraction` — caption is too vague for a heuristic and is exactly what the LLM-NLP fallback (Phase 2, step 4) is for.
4. A blocklist filters out generic non-place descriptor phrases (e.g. "location in video", "prime location", "east-facing") that follow a 📍 in real-estate-style captions but aren't actual venue names.

**API details:**
- Endpoint: `POST https://places.googleapis.com/v1/places:searchText` (the legacy `maps.googleapis.com/maps/api/place/textsearch` endpoint returned `REQUEST_DENIED` for this project — the new Places API uses a different host, POST+JSON body, and header-based auth: `X-Goog-Api-Key` + `X-Goog-FieldMask`).
- **Gotcha discovered during setup**: enabling "Places API (New)" on the Cloud project is necessary but not sufficient — the API key's own **API restrictions** list (Credentials page, separate from project-level enablement) must also explicitly include "Places API (New)", or calls fail with `API_KEY_SERVICE_BLOCKED` even though the project has it enabled.
- Field mask requested: `id, displayName, formattedAddress, location, rating, userRatingCount, types, businessStatus`.

**Validated against the full real dataset** (315 parsed items):
| Status | Count | Meaning |
|---|---|---|
| `ready` | 111 | Successfully matched to a real place — got place_id, lat/lng, address, rating |
| `skipped_needs_llm_extraction` | 195 | No 📍 marker/title found — needs the LLM caption-NLP fallback step next |
| `no_match` | 9 | Had a candidate query but Places API found nothing |

Example enriched record:
```json
{
  "place_name": "Rasadhare",
  "address": "Ground Floor, 158/2, 6th Main Rd, 4th Block, Jayanagar, Bengaluru, Karnataka 560011, India",
  "lat": 12.9277861, "lng": 77.5809967,
  "rating": 3.7, "user_ratings_total": 387
}
```

**Known limitation**: Text Search occasionally returns a loose/wrong top match for vague or short queries (e.g. a query like "Sasive Fine Dining Restaurant" matched a different, unrelated restaurant once in testing). A future refinement should: bias search with `locationBias` (e.g. city/region from context), and/or have the LLM tagging step sanity-check the returned place name against the original caption before marking `ready`.

**Usage:**
```bash
python3 scripts/enrich_places.py data/saved_items.json --out data/enriched_items.json   # full run
python3 scripts/enrich_places.py data/saved_items.json --limit 15                        # quick test batch
python3 scripts/enrich_places.py data/saved_items.json --dry-run                         # no API calls, just show queries
```

Requires `GOOGLE_PLACES_API_KEY` in a local `.env` file (gitignored) or the environment.

---

## 10. LLM Auto-Tagging — Implemented (fully run)

`scripts/tag_places_llm.py` classifies each `ready` place into a broad `category` (restaurant/cafe/travel_destination/real_estate/shopping/nature/activity/other) plus 3-6 confidence-scored descriptive tags (kid-friendly, vegetarian, outdoor-seating, romantic, heritage, etc.), grounded in the caption + Google `place_types`. Results are written to the `place_tags` table (one row per tag, plus a synthetic `category:<value>` tag).

- Not a fixed closed vocabulary — a suggested tag list steers consistency, but the LLM can propose new tags since captions span food, travel, real estate, and shopping.
- Idempotent/resumable by design: reruns automatically skip places that already have tags (`WHERE id NOT IN (SELECT place_id FROM place_tags)`), unless `--retag` is passed.
- Hardened with retry/backoff (3 attempts, exponential backoff) and a `socket.setdefaulttimeout` safety net after an initial run hit a network stall past the per-request timeout (observed on this network — the request never errored, just sat on an open TCP connection).

**Status: 250 of 250 `ready` places tagged (100%)** — first pass stopped at 205/250 due to slow network throughput, resumed cleanly and finished the remaining 45 with 0 errors on the second run, proving the idempotent-resume design works as intended.

**Important interaction with `embed_places.py` (Section 11)**: the first full embedding run happened before tagging fully finished, so the 45 places that got tagged afterward had embeddings generated *without* their tags in the source text. Fixed by deleting those 45 rows from `embeddings` and re-running `embed_places.py` (which only embeds rows missing from the table) — now every embedding reflects the final tag set. **Lesson for future pipeline runs: always finish tagging before embedding, or re-embed anything tagged after the fact.**

**Usage:**
```bash
python3 scripts/tag_places_llm.py --db data/saveme.duckdb                    # tag all untagged ready places
python3 scripts/tag_places_llm.py --db data/saveme.duckdb --limit 10 --dry-run # test without writing
python3 scripts/tag_places_llm.py --db data/saveme.duckdb --retag             # re-tag everything
```

**Gotcha — DuckDB single-writer lock vs. the running web server**: `tag_places_llm.py` holds one write connection open for its entire run. If `scripts/serve_app.py` (Section 12b) is running at the same time, its read-only connection attempts will fail with a lock error. Stop the web server before running/resuming tagging, then restart it afterward.

Example output tags observed: `['kid-friendly', 'family-friendly', 'scenic-view', 'nature', 'wellness']` for a resort, `['pure-veg', 'fine-dining', 'heritage', 'romantic', 'family-friendly']` for a restaurant, `['theme-park', 'kid-friendly', 'family-friendly', 'adventure', 'tourist-attraction']` for an amusement park, `['street-food', 'chats', 'churmuri', 'food-court']` for a street-food stall, `['shopping', 'jewelry', 'gold', 'bridal', 'fashion']` for a jewelry store.

---

## 11. RAG Chat Search — Implemented

Two scripts implement the Conversational Query Service (Section 1 Phase 3 / Section 5 design):

**`scripts/embed_places.py`** — generates a semantic embedding for each `ready` place (text = name + address + tags from `place_tags` + first 800 chars of caption) via the Gemini embedding API and writes it to the `embeddings` table.

- Model: `gemini-embedding-001` (env override: `GEMINI_EMBED_MODEL`), called with `outputDimensionality: 768` to match the `embeddings.embedding FLOAT[768]` column already in the DuckDB schema.
- Idempotent/resumable — skips place IDs already present in `embeddings` (`WHERE sp.id NOT IN (SELECT place_id FROM embeddings)`).
- Same retry/timeout hardening pattern as `tag_places_llm.py` (`socket.setdefaulttimeout(25)` + manual retry with backoff).

```bash
python3 scripts/embed_places.py --db data/saveme.duckdb                 # embed all un-embedded ready places
python3 scripts/embed_places.py --db data/saveme.duckdb --limit 10 --dry-run
```

**`scripts/chat_search.py`** — the actual chat search: takes a free-text question, embeds it with the same model, ranks every saved place for that user by cosine similarity (pure Python, no `vss` index needed at this scale — see below), then sends the top-K candidates + the question to Gemini (`gemini-flash-latest`) with an instruction to answer **only** from the given candidates and cite the place name + source link. This grounding prevents hallucinated recommendations.

```bash
python3 scripts/chat_search.py --db data/saveme.duckdb --user sandy4nasa "where should I eat today?"
python3 scripts/chat_search.py --db data/saveme.duckdb --user sandy4nasa --top-k 8 --show-candidates "kid friendly places near JP Nagar"
```

**Why brute-force cosine similarity instead of the `vss` HNSW index**: at ~250 rows per user, a full Python scan is sub-millisecond and avoids extension/index-maintenance overhead. The `embeddings` table + `vss` extension are already reserved in the schema (Section 3) — swap `fetch_candidate_pool` + ranking in `chat_search.py` for an HNSW-indexed `array_cosine_similarity` query if/when a user's saved-place count grows into the thousands+.

**Validated with a real query** against the live dataset (candidates ranked correctly by semantic relevance — e.g. a "where should I eat today?" query surfaced only restaurants, not travel/real-estate items, and the model correctly cited names, ratings, and tags from the retrieved rows without inventing anything):

```
$ python3 scripts/chat_search.py --db data/saveme.duckdb --user sandy4nasa "where should I eat today?"
--- Answer ---
Here are a few great options from your saved places depending on what you are in the mood for today:
* Mon Cheri Crafted Continental Dining (Indiranagar): 4.7★, handmade pasta and pizza.
* ALE - Jayanagar: pure-veg, fine-dining, romantic heritage vibe.
* Sasive : Exclusively South Indian Restaurant (JP Nagar): traditional and authentic South Indian meal.
Would you like help narrowing these down by cuisine, location, or budget?
```

**Status: 250 of 250 `ready` places embedded (100%)**. `chat_search.py` works against whatever subset is embedded at any point — no need to wait for 100% coverage to test it, but full coverage is now in place.

---

## 12. Web Map / Dashboard — Implemented

A zero-build static dashboard for browsing the saved-places dataset visually, in `web/`.

**`scripts/export_map_data.py`** — dumps `saved_places` (status='ready', with lat/lng) joined with `place_tags` into `web/map_data.json`: one flat JSON array with `name, lat, lng, address, rating, user_ratings_total, source_url, collection_name, owner_username, tags[], category`. Re-run any time the DB changes; no rebuild step for the frontend, it just re-fetches the JSON.

```bash
python3 scripts/export_map_data.py --db data/saveme.duckdb --out web/map_data.json
python3 scripts/export_map_data.py --db data/saveme.duckdb --user sandy4nasa   # scope to one user
```

**`web/index.html`** — single-file static frontend (Leaflet + Leaflet.markercluster via CDN, vanilla JS, no npm/build tooling):
- OpenStreetMap tile base layer, marker clustering (so 250 pins don't overwhelm at low zoom).
- Color-coded pins by `category` (restaurant, cafe, travel_destination, real_estate, shopping, nature, activity, other).
- Sidebar: live place-count-per-category filter chips (click to toggle), free-text search across name/address/tags/collection, and a scrollable place-card list synced to the map (clicking a card pans/zooms the map and opens that place's popup).
- Marker popups show name, address, tags, rating, and a direct link back to the original Instagram post.

**Serving it locally** (any static file server works, no backend required for this read-only view):
```bash
cd web && python3 -m http.server 8765
# open http://localhost:8765/index.html in a browser
```

**Validated with Playwright browser automation** against the full live dataset (250/250 places): tile map loads, clusters expand correctly on zoom (verified drilling from an all-Europe/India world view down to a 63-pin Bengaluru cluster down to individual street-level pins), marker popups render correctly (tested "Rasadhare" — full address, tags, 3.7★ rating, working "View original post" link), and category filter chips correctly narrow both the map pins and the "N of 250 places shown" sidebar count (tested isolating to "restaurant" only → 35 of 250 shown, all-red pins).

**Known limitations / not yet built**: read-only (no add/edit/delete from the UI), single-user only (no auth), no server-side proximity/"near me" view yet (that's the separate proximity-nudge script, still pending).

---

## 12b. Combined Map + Chat Search Dashboard — Implemented

Merged the RAG chat search (Section 11) directly into the web dashboard (Section 12) so it's one integrated experience instead of two separate tools.

**`scripts/serve_app.py`** — replaces the plain `python3 -m http.server` with a small stdlib-only HTTP server (`http.server.ThreadingHTTPServer`, no Flask/FastAPI dependency added) that:
- Serves the static `web/` directory (same as before — `index.html`, `map_data.json`).
- Adds `POST /api/chat` — accepts `{"question": "...", "top_k": 8}`, runs the exact same `fetch_candidate_pool` + `run_chat_query` logic from `chat_search.py` (refactored into a shared, importable function so the CLI and server stay in sync), and returns `{"answer": "...", "candidates": [...]}` with full place details (lat/lng/tags/rating/source_url) per candidate.
- Keeps `GOOGLE_AI_API_KEY` entirely server-side — the browser never sees it.

```bash
python3 scripts/serve_app.py --db data/saveme.duckdb --user sandy4nasa --port 8765
# open http://localhost:8765/
```

**`web/index.html`** additions — a collapsible "💬 Ask SaveMe" chat panel above the category filters:
- Free-text question box + "Ask" button, posts to `/api/chat`.
- On response: renders the natural-language answer in the sidebar, and **takes over the map + place list** to show only the matched candidates (clustering/filters temporarily bypassed), auto-fits the map bounds to the results.
- "Clear search & show all places" link resets back to the normal browse/filter view of all 250 places.

**Refactor note**: `chat_search.py`'s `fetch_candidate_pool` now also returns `lat`, `lng`, and a derived `category` field (previously only in `export_map_data.py`) so the same candidate objects can be dropped straight onto the map without a second DB round-trip. The core ranking/prompt logic was extracted into `run_chat_query(pool, question, api_key, top_k)`, shared by both the CLI entry point and `serve_app.py`.

**Validated end-to-end with Playwright** against the live 250-place dataset: asked *"kid friendly places in the Netherlands"* → got a grounded 8-candidate answer citing real saved places (Goatfarm Ridammerhoeve, Hans & Gretel pancake house, etc.), map auto-zoomed to the Netherlands showing only those 8 pins, sidebar updated to "8 places matched your question", and "Clear search" correctly restored the full 250-place view.

**Not yet built**: no persistent hosting/deployment (still local-only via `serve_app.py`), no conversation memory (each question is independent, no multi-turn follow-up context yet), no loading-state cancellation if a user fires a second question before the first resolves.

---

## 14. Android Share-to-App Ingestion + Mobile PWA — Implemented

Extended SaveMe from a batch pipeline + browse dashboard into a live app you can share Instagram posts to directly from your phone, install like a native app, and use comfortably on a small screen.

### 14.1 Why not fetch the caption automatically

Tried fetching a public Instagram reel/post URL server-side (no login, mobile User-Agent) to auto-extract the caption. Confirmed via `curl` that Instagram returns a login-walled HTML shell: zero `og:` meta tags, page title is just "Instagram", body contains login/challenge markers. This holds even for public posts. Combined with the earlier finding that Meta's oEmbed API requires app review (same blocker as the original DM/API approach), there is no free/instant way to pull caption text from a bare URL. **Chosen UX**: "share + quick note" pattern (like Pocket/Instapaper) — user shares the post link, optionally types a short note (place name or pasted caption fragment), and that note substitutes for the caption in the extraction pipeline. If no note is given, the item is saved with `enrichment_status = needs_manual_caption` instead of being dropped or guessed at, so it's recoverable later.

### 14.2 Single-item ingestion pipeline — `scripts/ingest_pipeline.py`

Reuses every stage of the batch pipeline for one item at a time instead of duplicating logic:
- `enrich_places.enrich_item()` — heuristic 📍/title parsing + Google Places lookup.
- `extract_places_llm.process_item()` — Gemini caption-NLP fallback when the heuristic can't resolve a place.
- `tag_places_llm.call_gemini()` — category + tags.
- `embed_places.call_embed()` + `build_embedding_text()` — 768-dim embedding for chat search.

`ingest_single_item(con, user_id, source_url, note_text, gemini_key, places_key, owner_username=None)` is the single entry point; `insert_single_place()` handles the DuckDB insert (with `con.execute("LOAD spatial")` required per-connection for `ST_Point`) and returns the new row's `id` via `RETURNING`. Tested standalone for both the "note resolves a place" and "no note → needs_manual_caption" cases.

### 14.3 Share target + ingestion API — `scripts/serve_app.py`

Added two routes to the existing stdlib HTTP server (alongside `/` and `/api/chat`):
- `GET /share-target` — the page Android navigates to when you use the OS share sheet. Parses `?title=&text=&url=` (apps populate these three fields inconsistently, so `extract_shared_url()` scans all three with an Instagram URL regex, priority url → text → title), renders a quick-note form (`scripts/share_target_template.py`) pre-filled with the detected link.
- `POST /api/ingest` — accepts `{"source_url": "...", "note": "..."}`, runs `ingest_single_item()`, returns the resulting place (or `needs_manual_caption` status).

Now requires both `GOOGLE_AI_API_KEY` and `GOOGLE_PLACES_API_KEY` at startup. Verified end-to-end via curl and from a real Android share: shared a reel → landed on the quick-note page with the link pre-filled → typed a note → place was resolved, tagged, embedded, and immediately showed up in chat search results.

### 14.4 PWA installability

- `web/manifest.json` — name/icons/theme colour, `display: standalone`, and a `share_target` block (`method: GET`, `action: /share-target`, params mapping `title`/`text`/`url`) — this is what makes "SaveMe" appear as a share destination once installed.
- `web/service-worker.js` — minimal pass-through fetch handler (no offline caching, since the app is read/write against a live local backend); required by Chrome for a PWA to be considered installable.
- `web/icon-192.png` / `icon-512.png` — generated placeholder pin icons (Pillow).
- `web/index.html` — links the manifest, registers the service worker, and adds an explicit **"⬇ Install App" button** wired to the `beforeinstallprompt` event (Chrome doesn't always surface its automatic install banner, so an in-page trigger is more reliable) plus a fallback hint for browsers that don't support it.

### 14.5 HTTPS tunnel for phone testing

`localtunnel` was tried first but hung indefinitely — its data channel needs a raw high TCP port that appears to be blocked in this environment (the initial HTTP handshake to loca.lt succeeded, but the tunnel itself never established). Switched to **`cloudflared`** (`brew install cloudflared`, then `cloudflared tunnel --protocol http2 --url http://localhost:8765`), forcing `--protocol http2` since the default QUIC/UDP transport was also blocked (its own preflight check flagged this and suggested the fallback). This produces a public `https://<random>.trycloudflare.com` URL that proxies to the local dev server — used to install the PWA and test the share flow on a real device without needing production hosting.

**Important**: both `serve_app.py` and `cloudflared` must be started with the bash tool's `detach: true` option, not just `nohup ... &`, or they get killed when the session recycles.

### 14.6 Mobile-friendly layout (bottom sheet + FAB chat)

The initial mobile view reused the desktop's fixed 320px sidebar-next-to-map flex layout, which on a phone-width screen left almost no room for the map. Redesigned for `max-width: 768px` via a media query, without touching desktop styling:
- **Map is full-screen**, the sidebar becomes a fixed-position bottom sheet (peek height showing just the search box + a drag handle; tap the handle, or focus the search box, to expand to 75vh and reveal category filters/stats/place list).
- **Chat moved out of the sidebar** into a floating action button (💬, bottom-right) that opens the existing chat panel as a full-screen modal (reusing the same DOM/IDs — no duplicated markup — toggled via a `modal-open` class plus a `chat-modal-active` class on `<body>` to hide the FAB while the modal is open).
- Selecting a place (map marker or list card) auto-collapses the sheet back down so the map is visible again.
- Verified visually with Playwright at a 390×844 viewport: collapsed sheet, expanded sheet, chat modal open (with a live query answered), chat modal closed with results applied to the map — and confirmed the 1400×900 desktop view is pixel-identical to before the change.

### 14.7 Known limitations / not yet built

- Android-only; iOS would need a Shortcuts-based share action (no native Share Extension without Xcode) — explicitly deferred.
- `enrich_places.py` and `extract_places_llm.py` still lack the retry/timeout hardening already present in `tag_places_llm.py`.
- No offline mode — the service worker is a pass-through only; the app requires the backend to be reachable.

---

## 16. Production Hosting + Multi-User Auth — Implemented

### 16.1 Hosting: DigitalOcean VPS + Cloudflare

Moved off `cloudflared`'s ephemeral quick-tunnel (URL changed on every restart, no uptime guarantee) to permanent hosting:

- **Droplet**: DigitalOcean, Ubuntu 24.04, Basic plan (1 vCPU / 1GB RAM, ~$6/mo), static IPv4.
- **App deployment**: `scripts/`, `web/`, `data/saveme.duckdb`, `.env` rsync'd to `/opt/saveme/` on the droplet. Python deps installed into a venv at `/opt/saveme_venv` (only pip dependency: `duckdb` — the codebase uses stdlib `urllib.request` and a hand-rolled `.env` loader, no `requests`/`flask`/etc).
- **Process management**: a `systemd` unit (`/etc/systemd/system/saveme.service`) runs `serve_app.py --db data/saveme.duckdb --port 80` as root, `Restart=always`, logs to `/var/log/saveme.log`. Survives reboots (`enabled`) and crashes.
- **Domain + HTTPS**: `saveme.blog` registered via Namecheap, nameservers pointed at Cloudflare (free plan). Cloudflare DNS has proxied A records for both the apex (`saveme.blog`) and `www` → the droplet's IP. Cloudflare terminates HTTPS at the edge and proxies to the origin over plain HTTP on port 80 — no certificate management needed on the droplet itself.
- **Hardening**: UFW firewall on the droplet allows only SSH (22), HTTP (80), HTTPS (443). SSH password auth disabled — key-only login via a dedicated ED25519 keypair.
- **Known gap**: no backup/redundancy for the droplet or the DuckDB file yet — the droplet's copy of `data/saveme.duckdb` is now the single production source of truth and can drift from any local copy. A backup/sync strategy is a future TODO.

### 16.2 Multi-user auth (username + password + invite-code-gated signup)

Since the app is now a permanently-hosted public URL, open signup would let anyone rack up costs against the owner's Gemini/Places API keys. Auth was added specifically to close that gap, not for its own sake — kept intentionally minimal (no OAuth/email, stdlib-only crypto).

**`scripts/auth.py`** — core auth logic, no third-party dependencies:
- Passwords hashed with `hashlib.pbkdf2_hmac("sha256", ..., 260_000 iterations)`, stored as `pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>` in `users.password_hash`.
- Sessions are opaque random tokens (`secrets.token_urlsafe(32)`, not JWT) stored in a new `sessions` table (`token PK, user_id, created_at, expires_at`), set as an `HttpOnly` cookie (`saveme_session`), 30-day expiry.
- `ensure_auth_schema(con)` idempotently adds `users.password_hash` / `users.display_name` and creates `sessions` if missing — run automatically on every `serve_app.py` startup, so no separate migration step is needed when deploying.

**`scripts/auth_templates.py`** — standalone `LOGIN_HTML` / `SIGNUP_HTML` strings (same dark-theme style as the dashboard and `share_target_template.py`), each with inline JS that POSTs to `/api/login` / `/api/signup` and redirects to `/` on success.

**Signup gating**: a `SIGNUP_INVITE_CODE` value in `.env` must match the `invite_code` field submitted at signup, or the request is rejected with 403. Only people the owner has told the code to can create an account.

**`scripts/serve_app.py` routes** (now auth-aware):

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/login`, `/signup` | GET | public | Auth pages |
| `/manifest.json`, `/service-worker.js`, `/icon-*.png` | GET | public | So the login page itself and PWA install flow work pre-auth |
| `/api/login` | POST | public | Verify credentials, set session cookie |
| `/api/signup` | POST | public | Create account (requires matching invite code), set session cookie |
| `/api/logout` | GET | — | Destroys the session row, clears the cookie, redirects to `/login` |
| `/` and all other static assets | GET | required | Redirects to `/login` if not authenticated |
| `/api/places` | GET | required | New dynamic route — replaces the old static `map_data.json`; returns only the logged-in user's places |
| `/api/chat` | POST | required | Scoped to the logged-in user's places (was previously scoped by a hardcoded `--user` CLI flag) |
| `/share-target` | GET | required | Redirects to `/login` if not authenticated |
| `/api/ingest` | POST | required | 401 JSON if not authenticated |

API routes return `401 {"error": ...}` when unauthenticated; HTML page routes issue a `302` redirect to `/login`. The old `--user` CLI flag on `serve_app.py` was removed entirely — user scoping now always comes from the session cookie.

**`scripts/export_map_data.py`** refactored: the SQL query logic was extracted into an importable `build_places_list(con, user_id=None)`, used both by the original CLI (still works, for static-file generation/back-compat) and by the new `/api/places` route.

**`web/index.html`** changes: fetches `/api/places` instead of the static `map_data.json`; both the initial places load and the chat POST now redirect to `/login` on a `401` response; added a "Log out" link in the sidebar header pointing at `/api/logout`.

**`scripts/share_target_template.py`**: its `/api/ingest` fetch now also redirects to `/login` on `401`, so sharing a post while logged out (e.g. session expired) doesn't just silently fail.

**Migration of the existing account**: the pre-existing `sandy4nasa` user (previously identified only by `auth_provider='instagram_export'` with no password) had a `password_hash` set directly via a one-off script using `auth.hash_password()`, both locally and on the droplet, so it could log in through the new flow without re-registering.

**Verified end-to-end** (locally via curl, then again against `https://saveme.blog` in production): unauthenticated access to `/`, `/api/places`, `/api/chat`, `/api/ingest`, `/share-target` all correctly blocked; login with correct/incorrect credentials; signup with correct/incorrect/duplicate invite code+username; logout clears the session and re-blocks access; public paths (`/login`, `/signup`, `/manifest.json`) remain reachable while logged out.

### 16.3 Known limitations / not yet built

- No password reset/forgot-password flow — if a user forgets their password, the owner must reset it manually via a DB script.
- No rate-limiting on `/api/login` or `/api/signup` (brute-force / signup-spam protection relies solely on the invite code being secret).
- No admin UI for managing users — done via direct DuckDB queries.
- No automated DB backup — the droplet's `data/saveme.duckdb` is a single point of failure.

---

## 17. Needs Review / Retry Flow, Recency-Aware Chat, Self-Serve Instagram Import — Implemented

### 17.1 Background: the "only `status='ready'` is visible" gap

Both `build_places_list()` (dashboard) and `fetch_candidate_pool()` (chat search) only ever selected `WHERE status = 'ready'`. Any saved post where Google Places enrichment didn't land on a specific match — most commonly `no_place_in_caption` (Gemini's caption-NLP step couldn't find a specific named place/project in the note, e.g. "Villa plot in jigani" with no builder/project name) — was completely invisible everywhere, silently. This affected a meaningful fraction of pre-existing saves (67 of 318 for the original account) and any new share where the note is too vague.

### 17.2 Needs Review + Retry

- **`scripts/export_map_data.py`**: added `list_needs_review(con, user_id)` — returns all of a user's non-`ready` places ordered by `saved_at DESC`.
- **`scripts/ingest_pipeline.py`**: extracted a shared `_run_enrichment(item, note_text, gemini_key, places_key)` helper used by both fresh ingests and retries. Added `update_existing_place()` (in-place `UPDATE`, not a new `INSERT`) and `retry_single_item(con, user_id, row_id, note_text, gemini_key, places_key)` — ownership-checked (`WHERE id = ? AND user_id = ?`), clears stale `place_tags`/`embeddings` before re-tagging if the retry reaches `status='ready'`.
- **`scripts/review_template.py`** (new): `REVIEW_HTML` — the `/review` page. Lists needs-review items with an editable note field + "Retry" button per item; calls `/api/needs-review` and `/api/retry` client-side.
- **New routes in `serve_app.py`**: `GET /api/needs-review`, `GET /review`, `POST /api/retry` (all auth-required, ownership-checked).
- **`web/index.html`**: added a "🔍 Needs review (N)" sidebar link with a live count badge.

### 17.3 Recency-aware chat search

`chat_search.py`'s RAG search is pure cosine-similarity over Gemini embeddings — it has no built-in concept of save time, so "what's the most recent place I saved?" was just a semantic match on that phrasing, not a chronological sort, and could return an unrelated old item. Fixed by adding a `RECENCY_RE` regex (`recent|latest|last saved|just saved|most recently|newest`) that, when the question matches, sorts candidates by `saved_at DESC` and skips the embedding call entirely. `saved_at` was added to `fetch_candidate_pool()`'s output and to `format_candidates()`'s display.

### 17.4 Self-serve Instagram export import (`/import`)

Productizes the original one-off `parse_instagram_export.py` + `load_to_duckdb.py` batch scripts (previously only runnable by a developer by hand) into a self-serve upload feature any signed-up user can use to bulk-import their existing Instagram saves, instead of sharing every post individually via the Android share-to-app flow.

- **`scripts/import_instagram.py`** (new): `parse_export_to_items()` reuses `parse_instagram_export.py`'s parsing logic (zip → `your_instagram_activity/saved/{saved_posts,saved_collections}.json` → normalized item dicts). `run_import_job()` is the background-thread entry point: for each parsed item, skips it if `source_url` is already saved by that user (dedupe against posts already shared individually), otherwise runs the same `ingest_pipeline._run_enrichment()` waterfall, inserts it, and tags/embeds it if it reached `status='ready'`. Progress (`processed`/`ready_count`/`skipped_duplicate`/`needs_review_count`) is written to a new `import_jobs` table after every item, so a `/api/import/status` poll always reflects current progress even mid-run.
- **Why a background thread, not inline in the request**: a few hundred posts each need a Places lookup + Gemini caption/tag/embedding call (~1-3s each with a 0.3s courtesy delay between items), so a full import can take several minutes — far too slow for a single HTTP request/response cycle.
- **No live progress bar (by design/user preference)**: the `/import` page shows the *last* job's status (running/done/error + counts) on page load only; the user checks back later rather than watching a live-updating bar.
- **`scripts/serve_app.py` routes**: `GET /import` (upload page), `POST /api/import` (multipart file upload — hand-rolled minimal multipart parser in `_parse_multipart_file()`, avoiding the deprecated stdlib `cgi` module), `GET /api/import/status` (latest job for the logged-in user).
- **`web/index.html`**: added a "📥 Import from Instagram" sidebar link.
- **Concurrency fixes discovered while building this** (both applied to `serve_app.py`):
  1. All `duckdb.connect(db_path, read_only=True)` calls were changed to plain `duckdb.connect(db_path)`. DuckDB refuses to open a connection with a *different configuration* (read-only vs read-write) than an existing open connection to the same file within the same process — previously latent (no connection was ever held open long enough to collide), it became a guaranteed failure once the import job holds a writable connection open for the full multi-minute run, breaking every other route (dashboard, chat, needs-review) for the whole import duration. Standardizing on one config for all connections eliminates the conflict.
  2. A module-level `_import_lock = threading.Lock()` now serializes "check for an already-running import job" + "create its `import_jobs` row" as one atomic section. Without it, two near-simultaneous uploads (double-click, two tabs) could both pass the `has_running_job()` check before either job row existed, then race on DuckDB catalog writes (`TransactionContext Error: Catalog write-write conflict`). The job row is now created synchronously in the request handler (inside the lock) before the background thread even starts, not from inside the thread itself.
- **Verified locally**: parsing a synthetic export zip, full upload → background processing → status polling while the dashboard/`api/places` route is hit concurrently (no more read-only conflict), duplicate-post detection on re-upload, and the concurrent-upload race (second request correctly gets `409` instead of corrupting the DB). Deployed and smoke-tested against `https://saveme.blog`.

---

## 18. Nearby Places Recommendations (Saved + Discover) — Implemented

Recommends similar places near a given saved place — both from the user's own saved list ("you saved these nearby too") and newly-discovered places via Google Places ("places you haven't saved yet nearby"). Triggered two ways: (1) clicking "🧭 Show nearby places" on a map marker popup, (2) automatically attached to a chat answer's top-ranked candidate.

- **`scripts/nearby_recommendations.py`** (new):
  - `CATEGORY_TO_PLACE_TYPES` maps our internal categories (from `tag_places_llm.py`'s classifier) to Google Places included types: `restaurant→[restaurant]`, `cafe→[cafe,bakery]`, `travel_destination→[tourist_attraction]`, `shopping→[shopping_mall,store]`, `nature→[park]`, `activity→[tourist_attraction,amusement_park]`. `real_estate` and `other` are intentionally unmapped — recommending "nearby land parcels" via a places API isn't meaningful — so `discover_nearby` returns `[]` for those categories, but `saved_nearby` still works for every category.
  - `find_nearby_saved(con, user_id, place_id, lat, lng, radius_km=5)` — pure SQL haversine-distance query against the user's own `saved_places` (`status='ready'`), no external API cost, returns up to 5 closest excluding the place itself.
  - `find_nearby_discover(lat, lng, category, exclude_google_place_ids, places_key, radius_m=3000)` — calls Google Places API (New) `searchNearby` (POST, `includedTypes`/`maxResultCount`/`locationRestriction.circle`, same `X-Goog-Api-Key`/`X-Goog-FieldMask` header pattern as the existing `searchText` call in `enrich_places.py`), filters out places the user already saved.
  - `get_recommendations(con, user_id, place_id, places_key)` — main entry for `/api/nearby`; looks up the place's lat/lng/category from the DB (ownership-checked via `WHERE id=? AND user_id=?`) then calls both finders.
  - `get_recommendations_for_coords(con, user_id, place_id, lat, lng, category, places_key)` — variant for chat integration, where lat/lng/category are already known from the candidate pool (avoids a redundant DB lookup).
- **`scripts/serve_app.py`**: new route `GET /api/nearby?place_id=N` (auth + ownership enforced); `_handle_chat()` now best-effort attaches `nearby_recommendations` (wrapped in try/except so a Places API hiccup never breaks the chat answer itself) for the top-ranked candidate.
- **`web/index.html`**: `popupHtml()` marker popups gained a "🧭 Show nearby places" button (lazy-loaded on click via `loadNearby(placeId)` → `/api/nearby`, rendered by `renderNearbyHtml()`); chat panel gained a `#chat-nearby` div populated from `data.nearby_recommendations` in `submitChat()` and cleared in `clearChat()`; clicking a saved-nearby item calls `focusPlace(id)` to pan/open its popup on the map.
- **Verified locally and in production** (`https://saveme.blog`): `/api/nearby?place_id=<cafe>` returns 5 saved + 5 discovered places with correct distance/rating; `/api/nearby?place_id=<real_estate>` correctly returns empty `discover_nearby` (no type mapping) while `saved_nearby` still runs; 404 for a nonexistent/not-owned place, 400 for missing `place_id`, 401 when logged out; chat question about a specific place correctly attaches matching `nearby_recommendations`.

---

## 19. Automatic Instagram Caption Fetch (No More Manual Note Required) — Implemented

**Bug reported**: sharing real posts to SaveMe kept landing in `no_place_in_caption` even though "the caption has details in them." Root cause: Instagram's OS share sheet only ever passes a bare URL (no caption text) — the app was relying entirely on whatever short note the user manually typed on the share screen (e.g. "Korean dress shop", "Land near srirangapatna"), which was too vague for Gemini/Places to resolve to one venue. The enrichment pipeline itself was healthy (verified with a detailed manual note, which enriched/tagged/embedded correctly) — the gap was the missing real caption.

**Fix**: discovered that Instagram serves the real caption via the `og:description` meta tag on public post pages — the same mechanism used for link-preview cards on WhatsApp/iMessage/Twitter — accessible via a plain unauthenticated HTTP GET (no login wall for this specific meta tag, even though the page itself shows a login wall for viewing the photo/video). Tag content format: `"{likes} likes, {comments} comments - {username} on {date}: \"{caption}\"."`.

- **`scripts/fetch_instagram_caption.py`** (new): `fetch_caption(source_url, timeout=8)` — fetches the post URL with a mobile Safari User-Agent, regex-extracts `og:description`, HTML-unescapes it, and strips Instagram's `"N likes, N comments - username on date: "` wrapper (regex-based, falls back to using the raw text as-is if the wrapper pattern doesn't match) to recover the clean caption + poster's username. Returns `None` on any failure (private account, network error, markup change, no post found) — this is unofficial scraping of a public page, not a supported Meta API, so callers must treat failure as a normal, expected outcome and fall back gracefully rather than erroring.
- **`scripts/ingest_pipeline.py`**: new `resolve_caption(source_url, note_text, owner_username=None)` helper — calls `fetch_caption()` first; if it succeeds, the scraped caption becomes the primary text (with any manual note appended after it as extra user context, only if not already contained in the scraped text) and the scraped `owner_username` fills in if none was passed. Falls back to the manual note alone if scraping is unavailable. Wired into both `ingest_single_item()` (fresh shares) and `retry_single_item()` (Needs Review retries) so both paths benefit — a retry with an empty note now still re-attempts the real caption fetch instead of requiring the user to retype it.
- **`scripts/share_target_template.py`**: redesigned for full automation per user preference — the share-target page now auto-submits to `/api/ingest` immediately on load (spinner: "Fetching caption & saving...") with no note required. Only if the result doesn't reach `status=ready` does a fallback note textarea + "Retry" button appear (calling `/api/retry` with the returned `place_id`), so there's still a manual recovery path for private accounts or genuinely ambiguous posts.
- **Verified against the two real stuck production items**: retried place_id 328 ("Korean dress shop" note) → auto-fetched caption revealed the actual shop name/area → resolved to `VARA THE HOUSE OF ETHNIC`, tagged (`shopping`, `clothing-store`, `ethnic-wear`), embedded. Retried place_id 327 ("Land near srirangapatna" note) → auto-fetched caption's `📍 Srirangapatna Side` line resolved to a location match, tagged (`real-estate`, `farmland`, `land-investment`), embedded. Also verified a brand-new fully-automatic share (empty note) end-to-end reaches `status=ready` without any user typing.
- **Known limitation**: only works for public Instagram accounts/posts; private accounts still fall back to the manual note flow. This is unofficial page scraping (not a documented API), so it could stop working if Instagram changes their page markup — the graceful `None`-on-failure fallback means that would silently degrade back to today's manual-note behavior rather than breaking anything.

---

## 20. Video-Content Analysis Fallback (Last Resort When Caption Has No Place) — Implemented

**Problem**: even with the real caption auto-fetched (Section 19), some posts genuinely never name a specific place in text — e.g. a real-estate Reel whose caption just says "Details 9620187675", or the location is only shown on an on-screen signboard / spoken aloud in the video, never typed anywhere. These land in `no_place_in_caption` with no further recovery path.

**Why this needed a paid third-party API**: Instagram's public post pages expose the caption via `og:description` (no login required), but do **not** expose a direct video URL the same way. Confirmed via 5 independent dead-ends: no `og:video` meta tag / embedded src in the page HTML; `yt-dlp` fails with "Instagram sent an empty media response... use --cookies"; the `/embed/` page confirms the media type (`GraphVideo`) but strips the actual video src (rendered client-side via authenticated JS); the legacy `?__a=1&__d=dis` JSON endpoint returns HTTP 500; the private mobile API endpoint (`/api/v1/media/{id}/info/`) redirects to a login page. This is a genuine authorization wall, not a scraping-technique gap — actual video bytes require *someone's* authenticated Instagram session. Official OAuth ("Login with Instagram") does **not** solve this either — Instagram's Graph/Instagram-Login APIs only ever expose the *linked account's own posts*, never Saved Posts or arbitrary third-party public posts (this is exactly why the "save-from-Instagram" app category — Tabi, Someday Map, SaveMe — relies on OS share-sheet workarounds instead of official integrations). Verified real-world precedent: independent research into how competitors/power users solve this (a public how-to guide for this exact niche) explicitly describes "a Reel downloader (browser tools or scriptable APIs)" as the standard method — confirming a third-party scraping API is the normal, expected approach here, not an unusual risk.

**Decision**: rather than storing the user's own Instagram session (ToS violation / account-ban risk), used **HikerAPI** (hikerapi.com) — a paid Instagram data API that maintains its own authenticated scraping infrastructure. Pay-per-request, no subscription: $20 deposit unlocks the Start tier (~1,000 requests at $0.02/request); higher deposits ($100/$300/$599) unlock progressively cheaper locked-in rates down to $0.0006/request. Currently on the $20 Start tier to validate value before committing to a bigger top-up.

- **`scripts/fetch_instagram_video.py`** (new): `fetch_video_info(source_url, hiker_api_key=None)` calls HikerAPI's `GET /v1/media/by/url` endpoint (auth via `x-access-key` header) and returns `{video_url, caption_text, video_duration, owner_username}`, or `None` on any failure (no `HIKER_API_KEY` configured, private post, image-only/carousel post with no video, network error). Requires a real browser-like `User-Agent` header — HikerAPI sits behind Cloudflare, which blocks the default `Python-urllib` UA with a 403. `download_video(video_url, dest_path, max_bytes=60MB)` streams the CDN video to a local temp file, best-effort (returns `False` rather than raising).
- **`scripts/analyze_video_llm.py`** (new): `analyze_video(item, gemini_key, places_key, hiker_api_key=None)` — the full last-resort pipeline: fetch video info (HikerAPI) → download the MP4 → upload to Gemini's resumable File API → poll until `state=ACTIVE` → `generateContent` with the video referenced via `file_data` + a prompt asking Gemini to watch visuals/on-screen text/audio and extract a place name + city/area (same JSON schema shape as `extract_places_llm.py`) → route any confident extraction through the existing `enrich_with_places()` Google Places lookup (reused, not duplicated). Fully best-effort: returns `None` if `HIKER_API_KEY` isn't set or any step fails, so callers keep the prior caption-based result rather than blowing up ingestion.
- **`scripts/ingest_pipeline.py`**: `_run_enrichment()` now adds this as a final tier — only triggered when the caption-based waterfall (heuristic + Gemini caption LLM) still lands on `no_place_in_caption`. Never runs on every share (keeps HikerAPI cost proportional to actual failures, not volume). Confirmed safe to run synchronously in-request: the server uses `ThreadingHTTPServer` (one thread per request, so a slow video-analysis call doesn't block other users), there's no reverse proxy timeout (the Python server listens directly on :80), and the share-target page's `fetch()` calls have no client-side timeout.
- **Verified against 2 real stuck production items** (via `retry_single_item`, same code path as the "Needs Review" retry button): place_id 347 (caption was a full real-estate listing but no specific venue name) → video showed a location board → resolved to "Anandgiri layout" with valid lat/lng, tagged (`real-estate`, `family-friendly`, `independent-house`, `house-for-sale`), embedded. Place_id 346 (raw note was just a phone number, no caption match) → video analysis found "Hosachiguru Managed Farmlands Corporate Office" with a full address, tagged (`real-estate`, `farmland`, `corporate-office`), embedded. Both reached `status=ready` with `enrichment_query_source=video_llm` for provenance.
- **Cost model**: 1 HikerAPI request (video URL lookup) + 1 Gemini video-understanding call per fallback attempt — only charged when caption extraction already failed. At the $20 Start tier: ~1,000 attempts before topping up. At 5,000/month volume, a $100 deposit (locked-in $0.001/request rate) would cost ~$5/month in ongoing usage.
- **Known limitations**: same public-account-only constraint as caption fetch (HikerAPI can't bypass private accounts either); unofficial reliance on a third-party scraper that could change its API or pricing; some posts (e.g. generic land/plot listings with no named venue) may still fail to geocode via Google Places even after video analysis, since Places Text Search can only match registered points of interest, not arbitrary addresses without a named business — this is a pre-existing Places-lookup limitation, not something video analysis alone can fix.

---

## 21. Chat Reliability Fix + Location-Aware "Near Me" Answers — Implemented

**Problem 1 — silent 500s on `/api/chat`**: production logs (`/var/log/saveme.log`) showed two unexplained `500` errors on `POST /api/chat` with no traceback (the handler's `except Exception as e` only ever returned `{"error": str(e)}` to the client, never logging server-side). Root cause: `chat_search.py`'s `call_gemini_generate()` (the answer-generation call) made a single HTTP attempt with no retry, unlike `embed_places.py`'s `call_embed()` which already retries 3x with exponential backoff on `URLError`/`TimeoutError`/`ConnectionError`. A transient Gemini hiccup (brief 5xx, rate limit) during the answer call failed the entire chat request outright.

**Problem 2 — no location awareness**: confirmed via grep that zero geolocation code existed anywhere in the app (`web/index.html`, `serve_app.py`, `chat_search.py`). The existing "nearby recommendations" feature only anchors to a *saved place's* own lat/lng — never the user's actual current device location — so a question like "what's near me?" had no way to be answered correctly; the chat would fall back to plain semantic similarity on the word "near", effectively returning an arbitrary place.

**Fixes**:
- **`scripts/chat_search.py`**:
  - `call_gemini_generate()` now retries up to 3x with exponential backoff (mirrors `call_embed()`'s pattern) on `URLError`/`TimeoutError`/`ConnectionError`, instead of raising immediately on the first transient failure.
  - Added `haversine_km(lat1, lng1, lat2, lng2)` for great-circle distance.
  - Added `NEARBY_RE` regex (`near me|nearby|close to me|close by|around me|around here|around my location|closest`), same pattern as the existing `RECENCY_RE` — semantic embeddings have no notion of "where the user physically is right now", so proximity-flavored questions need explicit distance-based ranking instead of cosine similarity.
  - `run_chat_query()` now accepts optional `user_lat`/`user_lng`. When a nearby-intent question is detected AND location is available, candidates are ranked by actual haversine distance instead of embedding similarity. Regardless of intent, when location is available every candidate gets an annotated `"X.X km from you"` in `format_candidates()`, and the prompt's new `{location_context}` placeholder tells Gemini the user's approximate coordinates so it can reason about distance even for questions that don't literally say "near me". When no location is available (permission denied, unsupported browser), everything gracefully degrades to the prior behavior — `{location_context}` renders empty and ranking falls back to semantic/recency as before.
  - CLI (`main()`) gained `--lat`/`--lng` flags for local testing without a browser.
- **`scripts/serve_app.py`**: `_handle_chat()` now reads optional `lat`/`lng` from the JSON request body and passes them through to `run_chat_query()`. Also added `traceback.format_exc()` logging to stderr on any chat failure, so future issues are diagnosable from `/var/log/saveme.log` instead of showing up as an opaque unlogged 500.
- **`web/index.html`**: added `getUserLocation()` — calls `navigator.geolocation.getCurrentPosition()` with a 4s cap so a slow/absent permission prompt never blocks the chat request, caches the resolved `{lat, lng}` for the rest of the session (avoids re-prompting on every message), and resolves to `null` on denial/timeout/unsupported browsers. `submitChat()` now awaits this and includes `lat`/`lng` (or `null`) in the `/api/chat` request body.
- **Verified end-to-end against production data** (Bengaluru coordinates, real `sandy4nasa` saved places): a "what places are near me?" query correctly ranked candidates by real distance (2.1–2.9 km) with accurate haversine values in the answer; a "what was my last saved post?" query combined with a location correctly returned the true most-recent places along with distance context; a plain semantic query ("where should I eat today?") without location still worked exactly as before, confirming graceful degradation.
- Deployed to production (`scripts/chat_search.py`, `scripts/serve_app.py`, `web/index.html` synced, `saveme.service` restarted, site verified `200 OK`).

---

## 22. Content-Only Posts (No Map Location) + Multi-Place Extraction — Implemented

**Problem 1 — content-only posts stuck forever in "Needs Review"**: production DB audit found 77 of 342 saved items (~22%) permanently stuck in `no_place_in_caption` status. Sampling captions showed two distinct causes conflated under one status: (a) genuine non-place content -- recipes, DIY/craft tutorials, product/shipping posts, song covers, movie recommendations -- with NO findable venue at all, and (b) posts that plausibly reference a real place but couldn't be confidently resolved (e.g. a land parcel ad with only a phone number, no registered business name). Both were treated identically: shown forever in "Needs Review" (unfixable for case (a) -- there's no place to find), excluded from map/chat (both require `status='ready'`), and repeatedly eligible for the paid HikerAPI video-analysis fallback even though a recipe video obviously has no venue to extract.

**Problem 2 — only one place extracted per post**: the extraction schema supported a single `place_name`/`city_or_area` pair, so itinerary/roundup posts naming multiple real venues (a multi-city Christmas-market road trip, a "top N cafes" list) were reduced to zero or one place, silently dropping the rest.

**Fixes**:
- **`scripts/extract_places_llm.py`**: `RESPONSE_SCHEMA` replaced the flat `place_name`/`city_or_area`/`confidence` fields with a `places` array (one entry per distinct venue) plus a new `content_title` field. `PROMPT_TEMPLATE` now instructs Gemini to list every distinct venue separately for roundup/itinerary posts, and to set `is_place_mentioned=false` with a short descriptive `content_title` for recipes/DIY/craft/product/meme posts. `process_item()` was rewritten to: return the new terminal `saved_no_place` status (using `content_title`, or the caption's first line as a fallback name) for confirmed non-place content; resolve each confidently-extracted place independently via `enrich_with_places()`; return a single dict for one resolved place (unchanged shape, backward compatible); return `{"enrichment_status": "multi_place", "resolved_places": [...]}` when 2+ venues resolve from one post; and fall back to the original `no_place_in_caption` (still video-analysis-eligible) only when a named place doesn't resolve via Google Places.
- **`scripts/ingest_pipeline.py`**: added `TAGGABLE_STATUSES = {"ready", "saved_no_place"}` and shared helpers `_place_result_dict()`/`_insert_and_process()` so both a fresh share (`ingest_single_item()`) and a "Needs Review" retry (`retry_single_item()`) insert one `saved_places` row per resolved place (`source_url` has no UNIQUE constraint, confirmed safe -- multiple rows can share the same source post), tag+embed each independently, and return the primary result plus an `additional_places` list for the extras. The video-analysis fallback in `_run_enrichment()` only triggers on `no_place_in_caption`, so `saved_no_place` naturally skips it -- no HikerAPI credits wasted on content with nothing to find.
- **`scripts/export_map_data.py`**: `list_needs_review()` now excludes `saved_no_place` (it's a resolved terminal state, not an actionable review item). New `list_saved_content()` powers the new tab.
- **`scripts/chat_search.py`**: `fetch_candidate_pool()` now includes `saved_no_place` alongside `ready`, so content-only saves are chat-searchable (e.g. "what recipes did I save?"). `format_candidates()` shows `"(saved content, no map location)"` instead of crashing/showing `None` when `address` is missing.
- **`scripts/tag_places_llm.py`**: added `recipe` and `diy_craft` categories so content-only posts get a meaningful tag instead of being forced into a place category.
- **`scripts/content_template.py`** (new) + **`serve_app.py`**: new `GET /content` page and `GET /api/content` endpoint -- a browsable, filterable list of `saved_no_place` items (name, category, tags, caption preview, link to original post), separate from the map. Linked from the sidebar (`web/index.html`, "📝 Saved Content").
- **`scripts/share_target_template.py`**: the share confirmation screen now treats `saved_no_place` as a successful save (not an error needing a retry), pointing the user to the new Saved Content tab, and shows "+N more places saved from this post" whenever `additional_places` is non-empty.
- **Verified end-to-end against real production data** before deploying: ran the new `process_item()`/`retry_single_item()` logic against a scratch copy of the production DB for known stuck items -- recipe posts ("Street Style Masal Poori", "Healthy Lunch Ideas") correctly resolved to `saved_no_place` with sensible auto-generated titles and `recipe` tags; the "Europe XMAS Market road trip" itinerary post correctly extracted **9 distinct venues** across 6 cities, each independently geocoded/tagged/embedded. Also smoke-tested the new `/content`, `/api/content`, and `/api/needs-review` endpoints over HTTP with a real session token.
- **Backfilled all 82 existing stuck items in production** (grew from 77 to 82 between the original audit and the fix landing): **64 resolved to `saved_no_place`** (recipes, land/plot ads, product promos, song covers, movie recs -- all now tagged, searchable, and out of Needs Review for good), **15 resolved to `ready`** with real venues found via multi-place extraction, yielding **43 additional map pins** from posts that previously contributed at most one place each (one Amsterdam day-trip roundup post alone yielded +25 places). Zero items remained stuck in `no_place_in_caption` after the backfill -- only 3 unrelated `needs_manual_caption` items remain (private posts where caption fetch fails entirely, a pre-existing separate limitation).
- Deployed to production (`extract_places_llm.py`, `ingest_pipeline.py`, `export_map_data.py`, `chat_search.py`, `tag_places_llm.py`, `content_template.py`, `serve_app.py`, `share_target_template.py`, `web/index.html` synced; DB backed up before backfill; `saveme.service` restarted; site verified `200 OK`).

---

## 23. YouTube Import Support (Share-to-App) — Implemented

**Goal**: extend the existing "Share to SaveMe" pipeline (Section 14) beyond Instagram so sharing a YouTube video/Shorts link works exactly the same way -- no app changes needed on the Android/PWA side, since `web/manifest.json`'s `share_target` already accepts any URL generically (it was never Instagram-restricted).

**Why YouTube was easy compared to Instagram**: Instagram has no official way to fetch an arbitrary public post's caption (hence the `og:description` scraping in `fetch_instagram_caption.py`), and no official way to fetch its video bytes either (hence the paid HikerAPI integration in `fetch_instagram_video.py`). YouTube, by contrast, exposes both title and description for any public video via the official **YouTube Data API v3** `videos.list` endpoint using just an API key -- no OAuth, no per-user consent screen, no scraping fragility. The existing `GOOGLE_PLACES_API_KEY` already works for this (same Google Cloud project, just needed "YouTube Data API v3" enabled once in the console) -- no new secret required.

**Implementation**:
- **`scripts/fetch_youtube_metadata.py`** (new): `extract_video_id()` pulls the 11-character video ID out of any common URL shape (`youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/`, `youtube.com/embed/`). `fetch_metadata()` calls `videos.list?part=snippet` and returns `{"caption": f"{title}\n\n{description}", "owner_username": channelTitle, "title": ..., "video_id": ...}` -- deliberately the same shape as `fetch_instagram_caption.fetch_caption()`'s return value so it plugs into the exact same downstream code. Returns `None` on any failure (private/deleted video, quota exceeded, invalid URL) -- same best-effort contract as the Instagram fetcher.
- **`scripts/ingest_pipeline.py`**:
  - Added `detect_platform(source_url)` -- returns `"instagram"`, `"youtube"`, or `"unknown"` based on the URL's domain.
  - `resolve_caption()` now takes `platform` and `youtube_api_key` params and dispatches to the right fetcher (`fetch_youtube_metadata` for YouTube, `fetch_caption` for Instagram, skipped entirely for `"unknown"` -- falls back to the manual note alone, same graceful-degradation contract as a fetch failure).
  - `ingest_single_item()` now calls `detect_platform()` instead of hardcoding `"platform": "instagram"`, and passes `places_key` through as the YouTube API key (same key, dual-purpose).
  - `retry_single_item()` re-detects platform from the stored URL as a fallback if the DB's `platform` column is empty (covers rows saved before this fix).
  - **No changes needed** to `extract_places_llm.py`, `enrich_places.py`, `tag_places_llm.py`, or `embed_places.py` -- the entire extraction/tagging/embedding pipeline already only cares about `raw_caption`/`hashtags`/`owner_username`, completely platform-agnostic by design.
  - **Video-analysis fallback** (`analyze_video_llm.py`) already no-ops safely for YouTube URLs without any code change: `fetch_instagram_video.fetch_video_info()` explicitly checks `"instagram.com" not in source_url` and returns `None`, so a YouTube item that doesn't resolve from title+description alone just lands on `no_place_in_caption`/`saved_no_place` same as today -- no wasted HikerAPI calls, no crash. A YouTube-native video-analysis fallback (via `yt-dlp` download, which works without cookies for YouTube unlike Instagram, or a transcript API for free spoken-audio signal) is a natural fast-follow, not built in this pass.
- **Verified end-to-end** against real public videos via a scratch copy of the production DB: a restaurant-review video ("Barstool Pizza Review - L & B Spumoni Gardens") correctly resolved to `status=ready`, `platform=youtube`, the real Brooklyn address, tagged `category:restaurant` + `pizza`/`italian-food`/`brooklyn`, and embedded -- with zero changes to the extraction code itself. A music video with no venue (Rick Astley) correctly resolved to `saved_no_place` with a sensible title and `music-video` tag, exactly mirroring the Instagram content-only-post behavior from Section 22.
- Deployed to production (`fetch_youtube_metadata.py` new, `ingest_pipeline.py` updated; `saveme.service` restarted; site verified `200 OK`).

**Not yet built (explicitly out of scope for this pass)**: transcript-based extraction and a free `yt-dlp` video-analysis fallback -- see Section 24, both built in the very next pass. Facebook support (see Section 21's feasibility note: harder, needs a paid third-party scraper like Apify) still not started as of Section 24.

---

## 24. YouTube Fallback Tiers: Free Transcript Extraction + yt-dlp Video Analysis — Implemented

**Goal**: close the two gaps called out at the end of Section 23 -- for YouTube items that don't resolve from title+description alone, try (1) the free transcript text, then (2) full video-visual/audio analysis via Gemini, mirroring Instagram's Section 20 fallback but without any paid API.

**New modules**:
- **`scripts/fetch_youtube_transcript.py`** (new): `fetch_transcript(video_id, max_chars=6000)` uses `youtube-transcript-api` v1.2.4's instance-method API (`YouTubeTranscriptApi().fetch(video_id)`) to pull auto-generated or manual captions, joins the snippet text, truncates to `max_chars`, and returns `None` on any failure (transcripts disabled, video unavailable, network error) -- same best-effort contract as every other fetcher in this pipeline. No API key needed.
- **`scripts/fetch_youtube_video.py`** (new): `download_video(source_url, dest_path, max_bytes, max_duration)` uses `yt-dlp` to download a low-to-mid-res MP4 directly to `dest_path`. Two non-obvious fixes were required to get this working reliably in 2025-era YouTube:
  1. **PO-token/SABR bypass**: YouTube's default `web` client now forces "SABR streaming" for most formats, which strips direct URLs and requires a Proof-of-Origin token yt-dlp doesn't have by default (`ERROR: The page needs to be reloaded` / empty format lists). Fix: `"extractor_args": {"youtube": {"player_client": ["android"]}}` makes yt-dlp use the Android client's API responses instead, which still expose working direct URLs (sometimes only a lower-res progressive format, which is fine for this use case).
  2. **Pre-created empty destination file silently no-ops the download**: callers create `dest_path` via `tempfile.NamedTemporaryFile(delete=False)`, which pre-creates a real (0-byte) file at that path *before* yt-dlp ever runs. yt-dlp's default behavior treats an existing file at the target path as "already downloaded" and skips re-fetching it -- silently reporting `100% of 0.00B` and leaving the file empty, with no exception raised. Fix: `"overwrites": True` in `ydl_opts` forces yt-dlp to always (re)download into that path regardless of what's already there. This was the root cause of a confusing bug where identical code succeeded when tested via `python3 -c "..."` (using a plain literal path that didn't pre-exist) but failed when run through the module's own callers (which always use `NamedTemporaryFile`).
  - No cookies/login required -- unlike Instagram, `yt-dlp` can fetch public YouTube video bytes directly, so this fallback needs no paid API key at all (unlike Section 20's HikerAPI dependency for Instagram).

**Pipeline changes**:
- **`scripts/ingest_pipeline.py`**:
  - New `_try_youtube_transcript(item, gemini_key, places_key)`: extracts the video ID, fetches the transcript, appends it to `raw_caption` as `"...\n\nTranscript:\n{transcript}"`, and re-runs the existing `extract_places_llm.process_item()` on the augmented item -- reusing the exact same Gemini extraction/Places-resolution logic already proven for captions, just with richer input text. Returns `None` (caller keeps the prior result) unless the re-run resolves to `ready` or `multi_place`.
  - `_run_enrichment()`: for items still at `no_place_in_caption` (ambiguous, not the terminal `saved_no_place`), now tries tiers in order for YouTube: (1) transcript fallback, then (2) if still unresolved, the existing video-analysis fallback (`analyze_video()`, now platform-aware -- see below). Instagram items skip straight to tier (2), unchanged from Section 20.
- **`scripts/analyze_video_llm.py`**: `analyze_video()` now dispatches by platform instead of being Instagram-only: Instagram videos still go through `fetch_instagram_video.py` (requires `HIKER_API_KEY`), YouTube videos go through the new `fetch_youtube_video.download_video()` (no key needed). Everything downstream (Gemini File API upload, video-understanding prompt, Places enrichment) is shared and platform-agnostic, unchanged.
- **`scripts/extract_places_llm.py`**: raised the `raw_caption` truncation limit from 1500 to 4000 characters -- transcripts can run to several thousand characters and 1500 was cutting off content before the part most likely to mention a venue.

**Verified end-to-end** against a real public video ("Barstool Pizza Review - L & B Spumoni Gardens") via a scratch copy of the production DB and standalone function calls:
- Transcript tier: with a deliberately generic/sparse caption forced in the test (simulating a video whose real description doesn't name a place), the transcript tier correctly recovered **three** distinct venues from garbled auto-caption text ("L&B spani Gardens", "defaria" for "Di Fara Pizza") -- Gemini's extraction was robust to the transcription errors and all three resolved to real Google Places entries via `multi_place`.
- Video-analysis tier: called directly (bypassing the transcript tier) on the same video, `yt-dlp` downloaded a 20MB clip via the Android-client workaround and the `overwrites` fix, Gemini's video understanding correctly identified the restaurant from on-screen signage, and it resolved to `status=ready` with the correct address.
- Regression check: the music video from Section 23 (no venue at all) still correctly short-circuits to `saved_no_place` via the full `ingest_single_item()` path without wasting any transcript/video-analysis calls (confirmed status/tags/embedding all correct in the DB).

**Deployed to production**: installed `yt-dlp` and `youtube-transcript-api` on `/opt/saveme_venv`, rsynced the 4 changed/new files, restarted `saveme.service`, verified `200 OK` and clean restart in `journalctl`.

**Known risk / caveat**: this entire video-download area is an actively-evolving cat-and-mouse space -- YouTube has changed its anti-bot enforcement multiple times in 2025 alone, and the `player_client: ["android"]` workaround could stop working with a future YouTube-side change. If that happens, the transcript tier (which doesn't depend on `yt-dlp` at all) still provides a free fallback signal, and the pipeline degrades gracefully to `no_place_in_caption`/`saved_no_place` rather than failing.

**Not yet built**: Facebook support -- see Section 25 for the full feasibility findings and proposed approach (researched, not yet implemented).

---

## 25. Facebook Import Feasibility (Researched, Not Yet Built)

**Goal**: extend the same share-target ingestion pipeline (Instagram → Section 14, YouTube → Sections 23-24) to Facebook post/video links.

**Verdict: harder and not free, unlike YouTube -- but architecturally the same pattern already proven for Instagram.**

**Why it's harder than YouTube**:
- No official API path for reading arbitrary public posts. The Graph API only exposes content from Pages/accounts the calling app has been OAuth-authorized against by the owner -- it does not let a third-party app read random public posts the way YouTube's Data API v3 does with just an API key.
- `og:description` scraping (the trick that works for Instagram in `fetch_instagram_caption.py`) is largely blocked on Facebook as of 2026 -- Meta enforces anti-scraping more aggressively there than on Instagram.
- No free tool equivalent to `yt-dlp` exists for downloading Facebook video/photo post content at the public-post level (yt-dlp does support some Facebook video URLs, but not with the same reliability or coverage as YouTube, and it does nothing for photo-only posts or captions).

**Proposed approach: Apify's Facebook Posts Scraper**
- Same architectural pattern as Instagram's HikerAPI integration -- a paid third-party scraping service takes on the auth/anti-bot complexity, and the pipeline just calls its API and gets back caption + media URLs.
- Cost: **~$2 per 1,000 posts** -- notably cheaper than HikerAPI's Instagram pricing (~$0.0006-$0.02/request depending on plan, i.e. roughly $0.60-$20/1,000).
- Supports caption + video/image extraction for public posts and Pages, which is enough to plug into the existing extraction/tagging/embedding pipeline unchanged (same platform-agnostic design already proven for YouTube in Section 23).

**Estimated integration effort** (mirrors the YouTube build in Section 23, roughly 1-2 days):
1. `scripts/fetch_facebook_caption.py` (new) -- calls the Apify actor's API for a given post URL, returns the same `{"caption": ..., "owner_username": ...}` shape already used by `fetch_instagram_caption.fetch_caption()` and `fetch_youtube_metadata.fetch_metadata()`.
2. `scripts/ingest_pipeline.py` -- extend `detect_platform()` to recognize `facebook.com`/`fb.watch` URLs, and add a `"facebook"` branch to `resolve_caption()`'s dispatch (same pattern as the existing `"youtube"` branch).
3. If Facebook video download proves necessary for a video-analysis fallback tier (Section 20/24's pattern), evaluate Apify's video-extraction output vs. attempting `yt-dlp` first (free but less reliable for Facebook specifically) before reaching for a second paid API call.
4. No changes needed to `extract_places_llm.py`, `tag_places_llm.py`, `embed_places.py`, or the frontend -- same platform-agnostic design that made YouTube a clean addition.

**Open questions before building** (would need user input): whether the ~$2/1,000-post cost is acceptable given expected Facebook-share volume, and whether an Apify account/API key needs to be provisioned first (no key currently on hand, unlike YouTube which reused the existing Google Places key).

**Status: not started.** Revisit if/when Facebook import becomes a priority.

---

## 15. Suggested Order of Operations (Do This First)

1. Get Telegram bot token (fastest, zero approval wait) — build ingestion + extraction waterfall against it first.
2. Get an LLM API key (Claude or Gemini) and Google Places API key — these unblock Phases 2–3.
3. Stand up Postgres with pgvector+postgis (Supabase free tier is fastest to start).
4. Build Phases 1–3 end-to-end for yourself as the only test user before adding OAuth/WhatsApp/web map.
5. Only pursue Twilio WhatsApp approval and Meta OAuth app review in parallel once the core pipeline works — those have external approval lead times.

