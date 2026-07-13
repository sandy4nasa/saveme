#!/usr/bin/env python3
"""
serve_app.py

Small self-contained web server (stdlib only, no Flask/FastAPI dependency)
that serves the static dashboard (web/index.html), the dynamic per-user
places feed (/api/places), the POST /api/chat RAG search endpoint
(chat_search.py), the real-time single-post ingestion flow
(IMPLEMENTATION_PLAN.md Section 14), and username/password multi-user auth
gated by an invite code (IMPLEMENTATION_PLAN.md Section 16):

  GET  /login, /signup        -- auth pages (public)
  POST /api/login              -- verify credentials, set session cookie
  POST /api/signup             -- create account (requires invite code), set session cookie
  GET  /api/logout              -- clear session, redirect to /login
  GET  /                        -- dashboard (auth required)
  GET  /api/places               -- this user's saved places as JSON (auth required)
  POST /api/chat                 -- RAG search over this user's places (auth required)
  GET  /share-target            -- Android share-sheet landing page (auth required)
  POST /api/ingest               -- ingest_pipeline.ingest_single_item() for one shared post (auth required)
  GET  /review, /api/needs-review, POST /api/retry -- non-'ready' items + retry-with-better-note flow
  GET  /content, /api/content    -- browsable list of content-only saves (no map location: recipes, DIY/craft, etc.)
  GET  /import                   -- Instagram export upload page (auth required)
  POST /api/import                -- upload a DYI export .zip, bulk-import in a background thread
  GET  /api/import/status         -- latest import job status for this user

Keeps GOOGLE_AI_API_KEY / GOOGLE_PLACES_API_KEY / SIGNUP_INVITE_CODE entirely server-side.

Usage:
  python3 scripts/serve_app.py --db data/saveme.duckdb --port 8765
  # then open http://localhost:8765/
"""

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import load_dotenv  # noqa: E402
from chat_search import fetch_candidate_pool, run_chat_query  # noqa: E402
from ingest_pipeline import ingest_single_item, retry_single_item  # noqa: E402
from share_target_template import SHARE_TARGET_HTML  # noqa: E402
from export_map_data import build_places_list, list_needs_review, list_saved_content  # noqa: E402
from auth_templates import LOGIN_HTML, SIGNUP_HTML  # noqa: E402
from review_template import REVIEW_HTML  # noqa: E402
from content_template import CONTENT_HTML  # noqa: E402
from import_template import IMPORT_HTML  # noqa: E402
from settings_template import SETTINGS_HTML  # noqa: E402
from import_instagram import (  # noqa: E402
    ensure_import_jobs_schema,
    parse_export_to_items,
    create_job,
    run_import_job,
    latest_job_for_user,
    has_running_job,
)
from nearby_recommendations import get_recommendations, get_recommendations_for_coords  # noqa: E402
import auth  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"

INSTAGRAM_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/\S+")

# Paths servable without a valid session -- auth pages themselves + the
# static assets they (and the browser's PWA install flow) need to load.
PUBLIC_PATHS = {"/login", "/signup", "/manifest.json", "/service-worker.js",
                 "/icon-192.png", "/icon-512.png"}


def extract_shared_url(qs):
    """The OS share sheet's field usage is inconsistent across apps -- scan
    title/text/url params (in that priority) for an actual Instagram link,
    falling back to whatever 'text' contains."""
    for field in ("url", "text", "title"):
        value = (qs.get(field, [""])[0] or "").strip()
        match = INSTAGRAM_URL_RE.search(value)
        if match:
            return match.group(0)
    return (qs.get("url", [""])[0] or qs.get("text", [""])[0] or "").strip()


# Serializes the "check for a running import job" + "start a new one" critical
# section across request threads -- without this, two near-simultaneous
# uploads (e.g. a double-click, or two tabs) can both pass the has_running_job
# check before either job row is written, then race on the same DuckDB
# catalog (INSERT OR IGNORE INTO users / CREATE TABLE IF NOT EXISTS), causing
# a "write-write conflict" error from DuckDB's MVCC transactions.
_import_lock = threading.Lock()


def make_handler(db_path, api_key, places_key, invite_code):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write(f"[serve_app] {self.address_string()} - {fmt % args}\n")

        # ---- low-level response helpers ----

        def _send_json(self, status, payload, set_cookie=None, clear_cookie=False):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._maybe_set_cookie(set_cookie, clear_cookie)
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status, html, set_cookie=None, clear_cookie=False):
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._maybe_set_cookie(set_cookie, clear_cookie)
            self.end_headers()
            self.wfile.write(body)

        def _send_redirect(self, location, clear_cookie=False, set_cookie=None):
            self.send_response(302)
            self.send_header("Location", location)
            self._maybe_set_cookie(set_cookie, clear_cookie)
            self.end_headers()

        def _maybe_set_cookie(self, token, clear):
            if clear:
                self.send_header(
                    "Set-Cookie",
                    f"{auth.SESSION_COOKIE_NAME}=; Path=/; HttpOnly; Max-Age=0",
                )
            elif token:
                max_age = auth.SESSION_TTL_DAYS * 24 * 3600
                self.send_header(
                    "Set-Cookie",
                    f"{auth.SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}",
                )

        # ---- auth helpers ----

        def _current_user(self):
            cookies = auth.parse_cookies(self.headers.get("Cookie", ""))
            token = cookies.get(auth.SESSION_COOKIE_NAME)
            if token:
                con = duckdb.connect(db_path)
                try:
                    user_id = auth.get_user_from_session(con, token)
                finally:
                    con.close()
                if user_id:
                    return user_id

            # No valid session cookie -- fall back to a personal API token via
            # Authorization: Bearer <token>. This is how clients that can't
            # hold a browser cookie authenticate (e.g. an iOS Shortcut posting
            # to /api/ingest directly from the share sheet, since Shortcuts
            # runs outside Safari's cookie jar).
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_token = auth_header[len("Bearer "):].strip()
                con = duckdb.connect(db_path)
                try:
                    return auth.get_user_from_api_token(con, api_token)
                finally:
                    con.close()
            return None

        def _require_auth_or_respond(self, is_api):
            """Returns user_id if authenticated, else sends the appropriate
            401 (API) / redirect (HTML) response and returns None."""
            user_id = self._current_user()
            if user_id:
                return user_id
            if is_api:
                self._send_json(401, {"error": "Not logged in"})
            else:
                self._send_redirect("/login")
            return None

        # ---- static files ----

        def _serve_static(self):
            rel_path = self.path.split("?", 1)[0]
            if rel_path == "/":
                rel_path = "/index.html"
            file_path = (WEB_DIR / rel_path.lstrip("/")).resolve()
            # Prevent path traversal outside web/
            if WEB_DIR not in file_path.parents and file_path != WEB_DIR:
                self.send_error(403, "Forbidden")
                return
            if not file_path.is_file():
                self.send_error(404, "Not found")
                return
            content_type = "text/html"
            if file_path.suffix == ".json":
                content_type = "application/json"
            elif file_path.suffix == ".js":
                content_type = "application/javascript"
            elif file_path.suffix == ".css":
                content_type = "text/css"
            elif file_path.suffix == ".webmanifest":
                content_type = "application/manifest+json"
            elif file_path.suffix == ".png":
                content_type = "image/png"
            body = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_share_target(self):
            qs = parse_qs(urlparse(self.path).query)
            shared_url = extract_shared_url(qs)
            html = SHARE_TARGET_HTML.format(shared_url=shared_url or "(no link detected)", shared_url_json=json.dumps(shared_url))
            self._send_html(200, html)

        # ---- routing ----

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"

            if path == "/login":
                self._send_html(200, LOGIN_HTML)
                return
            if path == "/signup":
                self._send_html(200, SIGNUP_HTML)
                return
            if path in PUBLIC_PATHS:
                self._serve_static()
                return
            if path == "/api/logout":
                cookies = auth.parse_cookies(self.headers.get("Cookie", ""))
                token = cookies.get(auth.SESSION_COOKIE_NAME)
                if token:
                    con = duckdb.connect(db_path)
                    try:
                        auth.destroy_session(con, token)
                    finally:
                        con.close()
                self._send_redirect("/login", clear_cookie=True)
                return

            if path == "/api/places":
                user_id = self._require_auth_or_respond(is_api=True)
                if not user_id:
                    return
                con = duckdb.connect(db_path)
                try:
                    places = build_places_list(con, user_id)
                finally:
                    con.close()
                self._send_json(200, places)
                return

            if path == "/api/needs-review":
                user_id = self._require_auth_or_respond(is_api=True)
                if not user_id:
                    return
                con = duckdb.connect(db_path)
                try:
                    items = list_needs_review(con, user_id)
                finally:
                    con.close()
                self._send_json(200, items)
                return

            if path == "/review":
                user_id = self._require_auth_or_respond(is_api=False)
                if not user_id:
                    return
                self._send_html(200, REVIEW_HTML)
                return

            if path == "/content":
                user_id = self._require_auth_or_respond(is_api=False)
                if not user_id:
                    return
                self._send_html(200, CONTENT_HTML)
                return

            if path == "/settings":
                user_id = self._require_auth_or_respond(is_api=False)
                if not user_id:
                    return
                con = duckdb.connect(db_path)
                try:
                    api_token = auth.get_or_create_api_token(con, user_id)
                finally:
                    con.close()
                self._send_html(200, SETTINGS_HTML.format(api_token=api_token))
                return

            if path == "/api/content":
                user_id = self._require_auth_or_respond(is_api=True)
                if not user_id:
                    return
                con = duckdb.connect(db_path)
                try:
                    items = list_saved_content(con, user_id)
                finally:
                    con.close()
                self._send_json(200, items)
                return

            if path == "/import":
                user_id = self._require_auth_or_respond(is_api=False)
                if not user_id:
                    return
                self._send_html(200, IMPORT_HTML)
                return

            if path == "/api/import/status":
                user_id = self._require_auth_or_respond(is_api=True)
                if not user_id:
                    return
                con = duckdb.connect(db_path)
                try:
                    job = latest_job_for_user(con, user_id)
                finally:
                    con.close()
                self._send_json(200, job)
                return

            if path == "/api/nearby":
                user_id = self._require_auth_or_respond(is_api=True)
                if not user_id:
                    return
                qs = parse_qs(urlparse(self.path).query)
                place_id_raw = (qs.get("place_id", [""])[0] or "").strip()
                if not place_id_raw.isdigit():
                    self._send_json(400, {"error": "place_id (integer) is required"})
                    return
                con = duckdb.connect(db_path)
                try:
                    result = get_recommendations(con, user_id, int(place_id_raw), places_key)
                finally:
                    con.close()
                if result is None:
                    self._send_json(404, {"error": "Place not found"})
                    return
                self._send_json(200, result)
                return

            if path == "/share-target":
                user_id = self._require_auth_or_respond(is_api=False)
                if not user_id:
                    return
                self._serve_share_target()
                return

            # Everything else (dashboard + its static assets) requires auth
            user_id = self._require_auth_or_respond(is_api=False)
            if not user_id:
                return
            self._serve_static()

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            if path == "/api/login":
                self._handle_login()
            elif path == "/api/signup":
                self._handle_signup()
            elif path == "/api/chat":
                user_id = self._require_auth_or_respond(is_api=True)
                if user_id:
                    self._handle_chat(user_id)
            elif path == "/api/ingest":
                user_id = self._require_auth_or_respond(is_api=True)
                if user_id:
                    self._handle_ingest(user_id)
            elif path == "/api/retry":
                user_id = self._require_auth_or_respond(is_api=True)
                if user_id:
                    self._handle_retry(user_id)
            elif path == "/api/import":
                user_id = self._require_auth_or_respond(is_api=True)
                if user_id:
                    self._handle_import_upload(user_id)
            elif path == "/api/regenerate-token":
                user_id = self._require_auth_or_respond(is_api=True)
                if user_id:
                    self._handle_regenerate_token(user_id)
            else:
                self.send_error(404, "Not found")

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def _parse_multipart_file(self, field_name="file"):
            """Minimal multipart/form-data parser -- avoids depending on the
            deprecated stdlib `cgi` module. Returns (filename, content_bytes)
            for the first part matching field_name, or (None, None)."""
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                return None, None
            boundary = None
            for piece in content_type.split(";"):
                piece = piece.strip()
                if piece.startswith("boundary="):
                    boundary = piece[len("boundary="):].strip('"')
            if not boundary:
                return None, None

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            delimiter = ("--" + boundary).encode()

            for part in raw.split(delimiter):
                part = part.strip(b"\r\n")
                if not part or part == b"--":
                    continue
                if b"\r\n\r\n" not in part:
                    continue
                header_block, content = part.split(b"\r\n\r\n", 1)
                headers_text = header_block.decode("utf-8", errors="ignore")
                if f'name="{field_name}"' not in headers_text:
                    continue
                filename = None
                m = re.search(r'filename="([^"]*)"', headers_text)
                if m:
                    filename = m.group(1)
                if content.endswith(b"\r\n"):
                    content = content[:-2]
                return filename, content
            return None, None

        # ---- auth endpoints ----

        def _handle_login(self):
            try:
                req = self._read_json_body()
                username = (req.get("username") or "").strip()
                password = req.get("password") or ""
                if not username or not password:
                    self._send_json(400, {"error": "Username and password are required"})
                    return

                con = duckdb.connect(db_path)
                try:
                    if not auth.authenticate(con, username, password):
                        self._send_json(401, {"error": "Invalid username or password"})
                        return
                    token = auth.create_session(con, username)
                finally:
                    con.close()

                self._send_json(200, {"ok": True}, set_cookie=token)
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        def _handle_signup(self):
            try:
                req = self._read_json_body()
                username = (req.get("username") or "").strip()
                password = req.get("password") or ""
                submitted_code = (req.get("invite_code") or "").strip()

                if not username or len(username) < 3:
                    self._send_json(400, {"error": "Username must be at least 3 characters"})
                    return
                if not password or len(password) < 8:
                    self._send_json(400, {"error": "Password must be at least 8 characters"})
                    return
                if not invite_code or submitted_code != invite_code:
                    self._send_json(403, {"error": "Invalid invite code"})
                    return
                if not re.match(r"^[a-zA-Z0-9_.-]+$", username):
                    self._send_json(400, {"error": "Username can only contain letters, numbers, _ . -"})
                    return

                con = duckdb.connect(db_path)
                try:
                    existing = con.execute("SELECT 1 FROM users WHERE id = ?", [username]).fetchone()
                    if existing:
                        self._send_json(409, {"error": "That username is already taken"})
                        return
                    auth.create_user(con, username, password)
                    token = auth.create_session(con, username)
                finally:
                    con.close()

                self._send_json(200, {"ok": True}, set_cookie=token)
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        # ---- app endpoints (auth already verified by do_POST/do_GET) ----

        def _handle_chat(self, user_id):
            try:
                req = self._read_json_body()
                question = (req.get("question") or "").strip()
                top_k = int(req.get("top_k", 6))
                user_lat = req.get("lat")
                user_lng = req.get("lng")
                user_lat = float(user_lat) if user_lat is not None else None
                user_lng = float(user_lng) if user_lng is not None else None
                if not question:
                    self._send_json(400, {"error": "question is required"})
                    return

                con = duckdb.connect(db_path)
                pool = fetch_candidate_pool(con, user_id)
                con.close()

                if not pool:
                    self._send_json(500, {"error": "No embedded places found. Run embed_places.py first."})
                    return

                answer, ranked = run_chat_query(pool, question, api_key, top_k, user_lat=user_lat, user_lng=user_lng)
                candidates = [
                    {
                        "id": place["id"],
                        "name": place["name"],
                        "address": place["address"],
                        "lat": place["lat"],
                        "lng": place["lng"],
                        "rating": place["rating"],
                        "user_ratings_total": place["user_ratings_total"],
                        "tags": place["tags"],
                        "category": place["category"],
                        "source_url": place["source_url"],
                        "platform": place.get("platform") or "instagram",
                        "score": round(score, 4),
                    }
                    for place, score in ranked
                ]

                # Attach "similar nearby" recommendations for the single
                # top-ranked candidate -- best-effort, never fails the chat
                # response if the Places lookup errors out.
                nearby = None
                top = candidates[0] if candidates else None
                if top and top.get("lat") is not None and top.get("lng") is not None:
                    try:
                        con = duckdb.connect(db_path)
                        try:
                            nearby = get_recommendations_for_coords(
                                con, user_id, top["id"], top["lat"], top["lng"], top["category"], places_key
                            )
                        finally:
                            con.close()
                    except Exception as e:
                        print(f"[serve_app] nearby recommendations failed for chat: {e}", file=sys.stderr)

                self._send_json(200, {"answer": answer, "candidates": candidates, "nearby_recommendations": nearby})
            except Exception as e:
                print(f"[serve_app] /api/chat failed: {e}\n{traceback.format_exc()}", file=sys.stderr)
                self._send_json(500, {"error": str(e)})

        def _handle_ingest(self, user_id):
            try:
                req = self._read_json_body()
                source_url = (req.get("source_url") or "").strip()
                note = req.get("note") or ""
                if not source_url:
                    self._send_json(400, {"error": "source_url is required"})
                    return

                con = duckdb.connect(db_path)
                try:
                    result = ingest_single_item(con, user_id, source_url, note, api_key, places_key)
                finally:
                    con.close()

                self._send_json(200, result)
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        def _handle_retry(self, user_id):
            try:
                req = self._read_json_body()
                place_id = req.get("place_id")
                note = req.get("note") or ""
                if not place_id:
                    self._send_json(400, {"error": "place_id is required"})
                    return

                con = duckdb.connect(db_path)
                try:
                    result = retry_single_item(con, user_id, int(place_id), note, api_key, places_key)
                finally:
                    con.close()

                if result is None:
                    self._send_json(404, {"error": "Place not found"})
                    return

                self._send_json(200, result)
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        def _handle_regenerate_token(self, user_id):
            try:
                con = duckdb.connect(db_path)
                try:
                    new_token = auth.regenerate_api_token(con, user_id)
                finally:
                    con.close()
                self._send_json(200, {"token": new_token})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        def _handle_import_upload(self, user_id):
            try:
                filename, content = self._parse_multipart_file("file")
                if not content:
                    self._send_json(400, {"error": "No file uploaded (expected multipart form field 'file')"})
                    return

                tmp = tempfile.NamedTemporaryFile(prefix="ig_export_", suffix=".zip", delete=False)
                try:
                    tmp.write(content)
                finally:
                    tmp.close()

                try:
                    items = parse_export_to_items(tmp.name)
                except Exception as e:
                    Path(tmp.name).unlink(missing_ok=True)
                    self._send_json(400, {"error": f"Could not parse export: {e}"})
                    return

                if not items:
                    Path(tmp.name).unlink(missing_ok=True)
                    self._send_json(400, {"error": "No saved posts found in this export"})
                    return

                original_filename = filename or "export.zip"

                # Lock held only across the check-and-launch section (DB work
                # here is just a SELECT + INSERT, both fast) -- the job row is
                # created synchronously here (not inside the background
                # thread) so a concurrent second upload's has_running_job
                # check reliably sees it before racing on job-row creation.
                with _import_lock:
                    con = duckdb.connect(db_path)
                    try:
                        ensure_import_jobs_schema(con)
                        if has_running_job(con, user_id):
                            Path(tmp.name).unlink(missing_ok=True)
                            self._send_json(409, {"error": "An import is already running for your account. Please wait for it to finish."})
                            return
                        job_id = create_job(con, user_id, original_filename, len(items))
                    finally:
                        con.close()

                    thread = threading.Thread(
                        target=run_import_job,
                        args=(db_path, user_id, job_id, items, tmp.name, original_filename, api_key, places_key),
                        daemon=True,
                    )
                    thread.start()

                self._send_json(200, {"status": "started", "total": len(items)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Serve the SaveMe dashboard + chat search + ingestion API + auth")
    parser.add_argument("--db", default="data/saveme.duckdb")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("GOOGLE_AI_API_KEY")
    places_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    invite_code = os.environ.get("SIGNUP_INVITE_CODE")
    if not api_key:
        print("Error: GOOGLE_AI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not places_key:
        print("Error: GOOGLE_PLACES_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not invite_code:
        print("Warning: SIGNUP_INVITE_CODE not set -- signup will be disabled for everyone", file=sys.stderr)

    # Run the auth schema migration (idempotent) before serving.
    con = duckdb.connect(args.db)
    auth.ensure_auth_schema(con)
    ensure_import_jobs_schema(con)
    con.close()

    handler = make_handler(args.db, api_key, places_key, invite_code)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    print(f"Serving SaveMe dashboard on http://localhost:{args.port}/  (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
