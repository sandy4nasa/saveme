#!/usr/bin/env python3
"""
parse_instagram_export.py

Parses an Instagram "Download Your Information" (DYI) export and extracts
all saved posts + saved collection items into a single normalized JSON file
ready to feed into the SaveMe extraction waterfall (scrape -> NLP -> Places
enrichment -> tagging).

Input can be:
  - a path to the exported .zip file, OR
  - a path to an already-extracted export folder

Usage:
  python3 parse_instagram_export.py <path_to_zip_or_folder> [--out output.json]

Google Drive note:
  If the export lives in Drive instead of local disk, download it first
  (e.g. via the Google Drive API `files.get_media` / `files.download`, or
  the `gdown`/`google-api-python-client` libs) to a local temp path, then
  point this script at that downloaded .zip. The parsing logic below is
  identical either way -- Drive access only changes how the bytes get to
  local disk.
"""

import argparse
import json
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Relative path (inside the export) to the two files we care about.
SAVED_DIR = Path("your_instagram_activity") / "saved"
SAVED_POSTS_FILE = "saved_posts.json"
SAVED_COLLECTIONS_FILE = "saved_collections.json"

URL_LABEL = "URL"
CAPTION_LABEL = "Caption"
NAME_LABEL = "Name"
USERNAME_LABEL = "Username"


def fix_mojibake(s):
    """
    Instagram's export JSON stores non-ASCII text (emoji, accented chars) as
    UTF-8 bytes that were mis-decoded as Latin-1, so every original byte shows
    up as its own codepoint (e.g. an emoji becomes 4 garbage characters).
    Round-tripping through latin1 -> utf8 restores the original text.
    """
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin1").decode("utf8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s  # already clean / not mojibake, leave as-is


def detect_platform(url):
    if not url:
        return "unknown"
    url_l = url.lower()
    if "instagram.com" in url_l:
        return "instagram"
    if "tiktok.com" in url_l:
        return "tiktok"
    if "google.com/maps" in url_l or "maps.app.goo.gl" in url_l:
        return "maps"
    if "facebook.com" in url_l or "fb.watch" in url_l:
        return "facebook"
    return "generic"


def find_label_value(label_values, label):
    """label_values is a list of {"label": ..., "value": ...} dicts (plus
    occasional {"dict": [...], "title": ...} nested groups). Returns the
    first plain value matching `label`, fixed for encoding."""
    for entry in label_values:
        if entry.get("label") == label:
            return fix_mojibake(entry.get("value", ""))
    return ""


def find_nested_group(label_values, title):
    """Finds a nested {"dict": [...], "title": title} block, e.g. 'Hashtags'
    or 'Owner', and returns its raw list of dict-entries."""
    for entry in label_values:
        if entry.get("title") == title and "dict" in entry:
            return entry["dict"]
    return []


def extract_hashtags(label_values):
    tags = []
    for group in find_nested_group(label_values, "Hashtags"):
        for sub in group.get("dict", []):
            if sub.get("label") == NAME_LABEL:
                tags.append(fix_mojibake(sub.get("value", "")))
    return tags


def extract_owner(label_values):
    owner_groups = find_nested_group(label_values, "Owner")
    owner = {"name": "", "username": "", "url": ""}
    for group in owner_groups:
        for sub in group.get("dict", []):
            label = sub.get("label")
            if label == NAME_LABEL:
                owner["name"] = fix_mojibake(sub.get("value", ""))
            elif label == USERNAME_LABEL:
                owner["username"] = fix_mojibake(sub.get("value", ""))
            elif label == URL_LABEL:
                owner["url"] = sub.get("value", "")
    return owner


def normalize_post_entry(raw_entry, collection_name=None):
    label_values = raw_entry.get("label_values", [])
    url = find_label_value(label_values, URL_LABEL)
    caption = find_label_value(label_values, CAPTION_LABEL)
    title = find_label_value(label_values, "Title")
    hashtags = extract_hashtags(label_values)
    owner = extract_owner(label_values)
    ts = raw_entry.get("timestamp")
    saved_at = (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    )

    return {
        "source_url": url,
        "platform": detect_platform(url),
        "title": title,
        "raw_caption": caption,
        "hashtags": hashtags,
        "owner_name": owner["name"],
        "owner_username": owner["username"],
        "fbid": raw_entry.get("fbid"),
        "saved_at": saved_at,
        "collection_name": collection_name,  # None => came from "All posts you've saved"
    }


def parse_saved_posts(saved_posts_json):
    return [normalize_post_entry(entry) for entry in saved_posts_json]


def parse_saved_collections(saved_collections_json):
    """Each collection entry has its own label_values (Name, Type, Privacy,
    Update time) plus a nested group titled "Media" holding the actual saved
    post entries within that collection."""
    results = []
    for collection in saved_collections_json:
        label_values = collection.get("label_values", [])
        collection_name = find_label_value(label_values, NAME_LABEL) or "Untitled Collection"

        for entry in label_values:
            if entry.get("title") == "Media" and "dict" in entry:
                for post_wrapper in entry["dict"]:
                    if "dict" in post_wrapper:
                        fake_entry = {"label_values": post_wrapper["dict"], "timestamp": collection.get("timestamp")}
                        results.append(normalize_post_entry(fake_entry, collection_name=collection_name))
    return results


def load_export_root(input_path: Path) -> Path:
    """Returns a directory containing your_instagram_activity/..., extracting
    the zip to a temp dir first if a .zip was passed."""
    if input_path.is_dir():
        return input_path

    if input_path.suffix.lower() == ".zip":
        tmp_dir = Path(tempfile.mkdtemp(prefix="ig_export_"))
        with zipfile.ZipFile(input_path) as zf:
            zf.extractall(tmp_dir)
        return tmp_dir

    raise ValueError(f"Unsupported input: {input_path} (expected a .zip file or extracted folder)")


def find_saved_dir(root: Path) -> Path:
    """The export may extract directly to root, or nest one level deeper
    (e.g. root/instagram-username-date/your_instagram_activity/...)."""
    direct = root / SAVED_DIR
    if direct.exists():
        return direct
    matches = list(root.glob(f"*/{SAVED_DIR}"))
    if matches:
        return matches[0]
    raise FileNotFoundError(
        f"Could not find '{SAVED_DIR}' under {root} -- is this a valid Instagram DYI export?"
    )


def dedupe_by_url(records):
    """Posts saved directly AND saved into a collection appear in both source
    files with the same URL. Merge them, keeping collection_name if any copy
    has one."""
    by_url = {}
    for r in records:
        key = r["source_url"] or id(r)
        if key not in by_url:
            by_url[key] = r
        else:
            existing = by_url[key]
            if not existing["collection_name"] and r["collection_name"]:
                existing["collection_name"] = r["collection_name"]
    return list(by_url.values())


def main():
    parser = argparse.ArgumentParser(description="Parse Instagram DYI export -> normalized saved items JSON")
    parser.add_argument("input", help="Path to the export .zip file or an already-extracted folder")
    parser.add_argument("--out", default="saved_items.json", help="Output JSON path (default: saved_items.json)")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        print(f"Error: {input_path} does not exist", file=sys.stderr)
        sys.exit(1)

    root = load_export_root(input_path)
    saved_dir = find_saved_dir(root)

    all_records = []

    posts_file = saved_dir / SAVED_POSTS_FILE
    if posts_file.exists():
        with open(posts_file, encoding="utf-8") as f:
            all_records.extend(parse_saved_posts(json.load(f)))
    else:
        print(f"Warning: {posts_file} not found, skipping", file=sys.stderr)

    collections_file = saved_dir / SAVED_COLLECTIONS_FILE
    if collections_file.exists():
        with open(collections_file, encoding="utf-8") as f:
            all_records.extend(parse_saved_collections(json.load(f)))
    else:
        print(f"Warning: {collections_file} not found, skipping", file=sys.stderr)

    deduped = dedupe_by_url(all_records)
    # Drop entries with no URL at all -- nothing to enrich.
    deduped = [r for r in deduped if r["source_url"]]

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(deduped, f, indent=2, ensure_ascii=False)

    # Quick summary
    platforms = {}
    collections = set()
    for r in deduped:
        platforms[r["platform"]] = platforms.get(r["platform"], 0) + 1
        if r["collection_name"]:
            collections.add(r["collection_name"])

    print(f"Parsed {len(deduped)} unique saved items -> {out_path}")
    print(f"By platform: {platforms}")
    print(f"Collections found: {sorted(collections) if collections else 'none'}")


if __name__ == "__main__":
    main()
