#!/usr/bin/env python3
"""
import_instagram.py

Self-serve bulk import of a user's Instagram "Download Your Information"
export -- lets any user (not just the original hand-run batch) upload their
own saved_posts/saved_collections export and have it processed through the
exact same enrichment waterfall used for one-off shares
(ingest_pipeline._run_enrichment), without needing a developer to run
parse_instagram_export.py / load_to_duckdb.py by hand.

Runs as a background thread kicked off by serve_app.py's POST /api/import
handler (processing a few hundred posts against the Places + Gemini APIs
takes minutes, too slow for a single HTTP request). Progress/result is
recorded in the `import_jobs` table so the /import page can show the status
of the most recent job next time the user checks -- no live polling needed.

Duplicate posts (same source_url already saved by this user, e.g. via the
Android share-to-app flow) are skipped automatically.
"""

import sys
import time
import traceback
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_instagram_export import (  # noqa: E402
    load_export_root,
    find_saved_dir,
    parse_saved_posts,
    parse_saved_collections,
    dedupe_by_url,
    SAVED_POSTS_FILE,
    SAVED_COLLECTIONS_FILE,
)
from ingest_pipeline import _run_enrichment, insert_single_place, tag_and_embed_single  # noqa: E402

import duckdb
import json

SLEEP_BETWEEN_ITEMS = 0.3  # courtesy delay between Places/Gemini calls, mirrors other batch scripts' --sleep defaults

JOBS_SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS import_jobs_id_seq;
CREATE TABLE IF NOT EXISTS import_jobs (
    id BIGINT PRIMARY KEY DEFAULT nextval('import_jobs_id_seq'),
    user_id TEXT REFERENCES users(id),
    filename TEXT,
    status TEXT,                 -- 'running' | 'done' | 'error'
    total INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    ready_count INTEGER DEFAULT 0,
    skipped_duplicate INTEGER DEFAULT 0,
    needs_review_count INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMP DEFAULT current_timestamp,
    finished_at TIMESTAMP
);
"""


def ensure_import_jobs_schema(con):
    con.execute(JOBS_SCHEMA_SQL)


def parse_export_to_items(zip_or_folder_path):
    """Reuses parse_instagram_export's logic to turn a DYI export (zip path
    or already-extracted folder) into the normalized item-dict list expected
    by ingest_pipeline._run_enrichment / enrich_places.enrich_item."""
    root = load_export_root(Path(zip_or_folder_path))
    saved_dir = find_saved_dir(root)

    all_records = []
    posts_file = saved_dir / SAVED_POSTS_FILE
    if posts_file.exists():
        with open(posts_file, encoding="utf-8") as f:
            all_records.extend(parse_saved_posts(json.load(f)))

    collections_file = saved_dir / SAVED_COLLECTIONS_FILE
    if collections_file.exists():
        with open(collections_file, encoding="utf-8") as f:
            all_records.extend(parse_saved_collections(json.load(f)))

    deduped = dedupe_by_url(all_records)
    return [r for r in deduped if r["source_url"]]


def create_job(con, user_id, filename, total):
    """Public: creates a 'running' import_jobs row synchronously. Must be
    called (with ensure_import_jobs_schema already run) before spawning the
    background thread, so a concurrent has_running_job() check reliably
    sees it -- see run_import_job's docstring for why."""
    row = con.execute(
        "INSERT INTO import_jobs (user_id, filename, status, total) VALUES (?, ?, 'running', ?) RETURNING id",
        [user_id, filename, total],
    ).fetchone()
    return row[0]


def run_import_job(db_path, user_id, job_id, items, upload_path, original_filename, gemini_key, places_key):
    """Entry point for the background thread. Owns its own DuckDB connection
    (safe to run alongside request-handling threads/connections against the
    same db_path within one process). `items` is the already-parsed list from
    parse_export_to_items() -- parsing is fast/local (no API calls) so the
    caller does it synchronously before spawning this thread, to report the
    total count immediately in the upload response. `job_id` is created
    synchronously by the caller (under a lock) before this thread starts, so
    that a concurrent second upload's has_running_job() check reliably sees
    it -- creating the job row from inside this thread would race with that
    check since thread scheduling is asynchronous."""
    con = duckdb.connect(db_path)
    try:
        con.execute("INSTALL spatial; LOAD spatial")

        ready_count = 0
        skipped_duplicate = 0
        needs_review_count = 0

        for item in items:
            existing = con.execute(
                "SELECT 1 FROM saved_places WHERE user_id = ? AND source_url = ?",
                [user_id, item["source_url"]],
            ).fetchone()
            if existing:
                skipped_duplicate += 1
            else:
                note_text = item.get("raw_caption") or item.get("title") or ""
                enriched = _run_enrichment(item, note_text, gemini_key, places_key)
                place_id = insert_single_place(con, user_id, enriched)
                if enriched.get("enrichment_status") == "ready":
                    tag_and_embed_single(con, place_id, enriched, gemini_key)
                    ready_count += 1
                else:
                    needs_review_count += 1

            con.execute(
                """
                UPDATE import_jobs SET processed = processed + 1, ready_count = ?,
                    skipped_duplicate = ?, needs_review_count = ? WHERE id = ?
                """,
                [ready_count, skipped_duplicate, needs_review_count, job_id],
            )
            time.sleep(SLEEP_BETWEEN_ITEMS)

        con.execute(
            "UPDATE import_jobs SET status = 'done', finished_at = current_timestamp WHERE id = ?",
            [job_id],
        )
    except Exception as e:
        print(f"[import_instagram] job failed: {e}\n{traceback.format_exc()}", file=sys.stderr)
        try:
            con.execute(
                "UPDATE import_jobs SET status = 'error', error_message = ?, finished_at = current_timestamp WHERE id = ?",
                [str(e), job_id],
            )
        except Exception as inner_e:
            print(f"[import_instagram] could not record job failure: {inner_e}", file=sys.stderr)
    finally:
        con.close()
        try:
            Path(upload_path).unlink(missing_ok=True)
        except Exception:
            pass


def latest_job_for_user(con, user_id):
    row = con.execute(
        """
        SELECT id, filename, status, total, processed, ready_count, skipped_duplicate,
               needs_review_count, error_message, started_at, finished_at
        FROM import_jobs WHERE user_id = ? ORDER BY id DESC LIMIT 1
        """,
        [user_id],
    ).fetchone()
    if not row:
        return None
    cols = ["id", "filename", "status", "total", "processed", "ready_count",
            "skipped_duplicate", "needs_review_count", "error_message", "started_at", "finished_at"]
    return dict(zip(cols, row))


def has_running_job(con, user_id):
    row = con.execute(
        "SELECT 1 FROM import_jobs WHERE user_id = ? AND status = 'running' LIMIT 1",
        [user_id],
    ).fetchone()
    return bool(row)
