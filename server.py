# server.py
from __future__ import annotations

import csv
import hashlib
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import fitz  # PyMuPDF
from flask import (
    Flask,
    abort,
    after_this_request,
    g,
    jsonify,
    make_response,
    redirect,
    render_template_string,
    request,
    send_file,
    url_for,
)

# ----------------------------
# Config
# ----------------------------

LIBRARY_DIR = Path(os.environ.get("PDF_LIBRARY_DIR", "./library")).resolve()
DATA_DIR = Path(os.environ.get("PDF_DATA_DIR", "./data")).resolve()
CACHE_DIR = DATA_DIR / "cache"
PDFS_CSV = DATA_DIR / "pdfs.csv"
PROGRESS_CSV = DATA_DIR / "progress.csv"
BOOKMARKS_CSV = DATA_DIR / "bookmarks.csv"

JPEG_QUALITY = 80
RENDER_ZOOM = 1.8  # Higher = sharper, larger files
COOKIE_NAME = "reader_id"
COOKIE_REFRESH_NAME = "reader_id_refreshed_at"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5
COOKIE_REFRESH_INTERVAL = 60 * 60 * 24 * 30

app = Flask(__name__)

csv_lock = threading.Lock()
render_locks: Dict[str, threading.Lock] = {}

# In-memory indexes
pdfs_by_id: Dict[str, dict] = {}
progress_by_reader: Dict[str, Dict[str, int]] = {}
bookmarks_by_reader: Dict[str, Dict[str, set[int]]] = {}


# ----------------------------
# Helpers
# ----------------------------

def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    if not PDFS_CSV.exists():
        PDFS_CSV.write_text("pdf_id,path,title,folder,mtime,page_count\n", encoding="utf-8")
    if not PROGRESS_CSV.exists():
        PROGRESS_CSV.write_text("reader_id,pdf_id,current_page,last_read\n", encoding="utf-8")
    if not BOOKMARKS_CSV.exists():
        BOOKMARKS_CSV.write_text("reader_id,pdf_id,page,created_at\n", encoding="utf-8")


def stable_pdf_id(path: Path) -> str:
    # Stable ID based on absolute path
    h = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
    return h[:12]


def csv_read_dicts(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row for row in reader if any(row.values())]


def csv_write_dicts_atomic(path: Path, fieldnames: List[str], rows: List[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    tmp.replace(path)


def load_state() -> None:
    global pdfs_by_id, progress_by_reader, bookmarks_by_reader

    pdfs_by_id = {}
    for row in csv_read_dicts(PDFS_CSV):
        pdfs_by_id[row["pdf_id"]] = row

    progress_by_reader = {}
    for row in csv_read_dicts(PROGRESS_CSV):
        rid = row["reader_id"]
        pid = row["pdf_id"]
        page = int(row["current_page"])
        progress_by_reader.setdefault(rid, {})[pid] = page

    bookmarks_by_reader = {}
    for row in csv_read_dicts(BOOKMARKS_CSV):
        rid = row["reader_id"]
        pid = row["pdf_id"]
        page = int(row["page"])
        bookmarks_by_reader.setdefault(rid, {}).setdefault(pid, set()).add(page)


def save_pdfs_state() -> None:
    rows = list(pdfs_by_id.values())
    rows.sort(key=lambda r: (r.get("folder", ""), r.get("title", "")))
    csv_write_dicts_atomic(
        PDFS_CSV,
        ["pdf_id", "path", "title", "folder", "mtime", "page_count"],
        rows,
    )


def save_progress_state() -> None:
    # Read the full raw data to preserve other readers' historical progress logs during single-user updates
    existing_rows = csv_read_dicts(PROGRESS_CSV)
    
    # Store everything keyed by (reader_id, pdf_id)
    state_map = {}
    for row in existing_rows:
        state_map[(row["reader_id"], row["pdf_id"])] = row

    # Overwrite/insert with fresh in-memory live tracking updates
    for rid, pdf_map in progress_by_reader.items():
        for pid, page in pdf_map.items():
            state_map[(rid, pid)] = {
                "reader_id": rid,
                "pdf_id": pid,
                "current_page": str(page),
                "last_read": str(int(time.time())),
            }
            
    csv_write_dicts_atomic(
        PROGRESS_CSV,
        ["reader_id", "pdf_id", "current_page", "last_read"],
        list(state_map.values()),
    )


def save_bookmarks_state() -> None:
    rows = []
    existing_rows = csv_read_dicts(BOOKMARKS_CSV)
    created_map = {
        (row.get("reader_id"), row.get("pdf_id"), int(row.get("page", 0))): row.get("created_at", "")
        for row in existing_rows
        if row.get("reader_id") and row.get("pdf_id") and row.get("page")
    }

    for rid, pdf_map in bookmarks_by_reader.items():
        for pid, pages in pdf_map.items():
            for page in sorted(pages):
                key = (rid, pid, page)
                rows.append({
                    "reader_id": rid,
                    "pdf_id": pid,
                    "page": str(page),
                    "created_at": created_map.get(key) or str(int(time.time())),
                })

    rows.sort(key=lambda r: int(r.get("created_at", 0)), reverse=True)
    csv_write_dicts_atomic(
        BOOKMARKS_CSV,
        ["reader_id", "pdf_id", "page", "created_at"],
        rows,
    )


def get_reader_id() -> str:
    rid = getattr(g, "reader_id", None)
    if rid is None:
        rid = request.cookies.get(COOKIE_NAME) or uuid.uuid4().hex
        g.reader_id = rid
    return rid


def get_or_make_lock(pdf_id: str) -> threading.Lock:
    with csv_lock:
        if pdf_id not in render_locks:
            render_locks[pdf_id] = threading.Lock()
        return render_locks[pdf_id]


def clear_pdf_cache(pdf_id: str) -> None:
    cache_path = CACHE_DIR / pdf_id
    if cache_path.exists():
        shutil.rmtree(cache_path, ignore_errors=True)


def scan_library() -> dict:
    """
    Recursively scan LIBRARY_DIR for PDFs.
    Updates pdfs.csv in-place.
    """
    existing = dict(pdfs_by_id)
    found: Dict[str, dict] = {}

    for root, _, files in os.walk(LIBRARY_DIR):
        for name in files:
            if not name.lower().endswith(".pdf"):
                continue

            path = Path(root) / name
            path = path.resolve()
            pdf_id = stable_pdf_id(path)
            stat = path.stat()
            mtime = str(int(stat.st_mtime))
            folder = str(path.parent.relative_to(LIBRARY_DIR)) if path.parent != LIBRARY_DIR else ""
            title = path.stem

            old = existing.get(pdf_id)
            page_count = ""
            if old:
                # Keep existing page_count if file unchanged
                if str(old.get("mtime", "")) == mtime:
                    page_count = old.get("page_count", "")
                else:
                    # File changed: invalidate cache and reset page count
                    clear_pdf_cache(pdf_id)
            row = {
                "pdf_id": pdf_id,
                "path": str(path),
                "title": title,
                "folder": folder,
                "mtime": mtime,
                "page_count": page_count,
            }
            found[pdf_id] = row

    # Remove entries that no longer exist
    pdfs_by_id.clear()
    pdfs_by_id.update(found)
    save_pdfs_state()
    return pdfs_by_id


def get_pdf(pdf_id: str) -> dict:
    pdf = pdfs_by_id.get(pdf_id)
    if not pdf:
        abort(404, "PDF not found")
    path = Path(pdf["path"])
    if not path.exists():
        abort(404, "PDF file missing on disk")
    return pdf


def ensure_page_count(pdf_id: str) -> int:
    pdf = get_pdf(pdf_id)
    if pdf.get("page_count"):
        return int(pdf["page_count"])

    path = Path(pdf["path"])
    with fitz.open(path) as doc:
        count = doc.page_count

    pdf["page_count"] = str(count)
    save_pdfs_state()
    return count


def page_image_path(pdf_id: str, page_num_1based: int) -> Path:
    return CACHE_DIR / pdf_id / f"{page_num_1based:05d}.jpg"


def render_page_to_jpg(pdf_id: str, page_num_1based: int) -> Path:
    pdf = get_pdf(pdf_id)
    path = Path(pdf["path"])
    count = ensure_page_count(pdf_id)

    if page_num_1based < 1 or page_num_1based > count:
        abort(404, "Page out of range")

    out_path = page_image_path(pdf_id, page_num_1based)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        return out_path

    lock = get_or_make_lock(pdf_id)
    with lock:
        if out_path.exists():
            return out_path

        with fitz.open(path) as doc:
            page = doc.load_page(page_num_1based - 1)
            mat = fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(str(out_path))

    return out_path


def get_progress(reader_id: str, pdf_id: str) -> int:
    return progress_by_reader.get(reader_id, {}).get(pdf_id, 1)


def set_progress(reader_id: str, pdf_id: str, page: int) -> None:
    progress_by_reader.setdefault(reader_id, {})[pdf_id] = max(1, int(page))
    save_progress_state()


def is_bookmarked(reader_id: str, pdf_id: str, page: int) -> bool:
    return int(page) in bookmarks_by_reader.get(reader_id, {}).get(pdf_id, set())


def set_bookmark(reader_id: str, pdf_id: str, page: int, bookmarked: bool) -> bool:
    page = max(1, int(page))
    pages = bookmarks_by_reader.setdefault(reader_id, {}).setdefault(pdf_id, set())
    if bookmarked:
        pages.add(page)
    else:
        pages.discard(page)
        if not pages:
            bookmarks_by_reader.get(reader_id, {}).pop(pdf_id, None)
    save_bookmarks_state()
    return page in bookmarks_by_reader.get(reader_id, {}).get(pdf_id, set())


def get_bookmarks(reader_id: str, limit: int = 20) -> List[dict]:
    rows = csv_read_dicts(BOOKMARKS_CSV)
    user_rows = [r for r in rows if r.get("reader_id") == reader_id and r.get("pdf_id") in pdfs_by_id]
    user_rows.sort(key=lambda r: int(r.get("created_at", 0)), reverse=True)

    bookmarks = []
    for row in user_rows[:limit]:
        pdf_data = dict(pdfs_by_id[row["pdf_id"]])
        pdf_data["bookmark_page"] = row.get("page", "1")
        bookmarks.append(pdf_data)
    return bookmarks


def sorted_pdfs() -> List[dict]:
    return sorted(
        pdfs_by_id.values(),
        key=lambda r: (r.get("folder", ""), r.get("title", "").lower()),
    )


def get_recent_reads(reader_id: str, limit: int = 5) -> List[dict]:
    """Retrieves up to `limit` entries the current reader has most recently opened."""
    rows = csv_read_dicts(PROGRESS_CSV)
    # Filter records targeting only this specific active cookie user
    user_rows = [r for r in rows if r.get("reader_id") == reader_id]
    
    # Sort descending based on unix Epoch timestamp string values
    user_rows.sort(key=lambda r: int(r.get("last_read", 0)), reverse=True)
    
    recent = []
    for row in user_rows:
        pid = row.get("pdf_id")
        if pid in pdfs_by_id:  # Verify file index mapping presence (prevent dead links)
            pdf_data = dict(pdfs_by_id[pid])
            pdf_data["current_page"] = row.get("current_page", "1")
            recent.append(pdf_data)
            if len(recent) >= limit:
                break
    return recent


# ----------------------------
# Startup
# ----------------------------

ensure_dirs()
load_state()
scan_library()


# ----------------------------
# Request hooks
# ----------------------------

@app.after_request
def set_reader_cookie(resp):
    now = int(time.time())
    try:
        last_refresh = int(request.cookies.get(COOKIE_REFRESH_NAME, "0"))
    except ValueError:
        last_refresh = 0

    cookie_missing = not request.cookies.get(COOKIE_NAME)
    refresh_due = last_refresh <= 0 or now - last_refresh >= COOKIE_REFRESH_INTERVAL or last_refresh > now
    if cookie_missing or refresh_due:
        rid = get_reader_id()
        resp.set_cookie(
            COOKIE_NAME,
            rid,
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
        )
        resp.set_cookie(
            COOKIE_REFRESH_NAME,
            str(now),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
        )
    return resp


# ----------------------------
# Routes
# ----------------------------

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23111'/%3E%3Cpath d='M14 16c8-2 14 0 18 5v30c-4-5-10-7-18-5V16zm36 0c-8-2-14 0-18 5v30c4-5 10-7 18-5V16z' fill='%23fff'/%3E%3Cpath d='M32 21v30' stroke='%23d98b00' stroke-width='3'/%3E%3C/svg%3E">
  <title>PDF Library</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #f4f4f4; color: #111; }
    header { padding: 16px; background: #111; color: #fff; position: sticky; top: 0; }
    .wrap { max-width: 900px; margin: 0 auto; padding: 12px; }
    .section-title { font-size: 14px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #666; margin: 24px 0 8px 4px; }
    .card { background: #fff; border-radius: 14px; padding: 14px; margin: 10px 0; box-shadow: 0 1px 4px rgba(0,0,0,.08); border: 1px solid transparent; }
    .card.accent { border-left: 4px solid #0066cc; border-radius: 10px 14px 14px 10px; }
    .card-row { display: flex; align-items: center; gap: 10px; }
    .card-link { flex: 1; min-width: 0; }
    .card-menu { position: relative; flex: none; }
    .card-menu summary { display: flex; align-items: center; justify-content: center; width: 44px; height: 44px; border-radius: 10px; background: #eee; cursor: pointer; font-size: 24px; line-height: 1; list-style: none; user-select: none; }
    .card-menu summary::-webkit-details-marker { display: none; }
    .card-menu-panel { position: absolute; z-index: 2; top: calc(100% + 6px); right: 0; min-width: 170px; padding: 6px; background: #fff; border: 1px solid #ccc; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.18); }
    .card-menu-panel a { padding: 10px 12px; border-radius: 7px; white-space: nowrap; }
    .card-menu-panel a:active { background: #eee; }
    .title { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
    .meta { color: #555; font-size: 14px; display: flex; gap: 8px; align-items: center; }
    .badge { background: #e0f0ff; color: #0066cc; padding: 2px 6px; border-radius: 6px; font-size: 11px; font-weight: bold; }
    a { color: inherit; text-decoration: none; display: block; }
    .btn { display: inline-block; margin-top: 10px; background: #111; color: #fff; padding: 10px 12px; border-radius: 10px; }
    .topbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; justify-content:space-between; }
    .search { width: 100%; max-width: 360px; padding: 10px 12px; border-radius: 10px; border: 1px solid #ccc; font-size: 16px; }
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div><strong>PDF Library</strong> — {{ count }} files</div>
      <form method="get" action="/">
        <input class="search" name="q" placeholder="Search..." value="{{ q|default('') }}">
      </form>
    </div>
  </header>
  <div class="wrap">
  
    {# --- CONTINUE READING SECTION --- #}
    {% if recents and not q %}
      <div class="section-title">Continue Reading</div>
      {% for pdf in recents %}
        <div class="card accent card-row">
          <a class="card-link" href="{{ url_for('read_pdf', pdf_id=pdf['pdf_id']) }}">
            <div class="title">{{ pdf['title'] }}</div>
            <div class="meta">
              <span class="badge">Page {{ pdf['current_page'] }}</span>
              {% if pdf['folder'] %}{{ pdf['folder'] }} / {% endif %}
              {% if pdf['page_count'] %}{{ pdf['page_count'] }} pages{% endif %}
            </div>
          </a>
          {% if pdf['pdf_id'] in bookmarked_pdf_ids %}
            <details class="card-menu">
              <summary aria-label="More options for {{ pdf['title'] }}" title="More options">⋯</summary>
              <div class="card-menu-panel">
                <a href="{{ url_for('pdf_bookmarks', pdf_id=pdf['pdf_id']) }}">View bookmarks</a>
              </div>
            </details>
          {% endif %}
        </div>
      {% endfor %}
    {% endif %}

    {# --- MAIN LIBRARY LIST SECTION --- #}
    <div class="section-title">{% if q %}Search Results{% else %}All Documents{% endif %}</div>
    {% for pdf in pdfs %}
      <div class="card card-row">
        <a class="card-link" href="{{ url_for('read_pdf', pdf_id=pdf['pdf_id']) }}">
            <div class="title">{{ pdf['title'] }}</div>
            <div class="meta">
              {% if pdf['folder'] %}{{ pdf['folder'] }} / {% endif %}
              {% if pdf['page_count'] %}{{ pdf['page_count'] }} pages{% else %}page count unknown{% endif %}
            </div>
          </a>
        {% if pdf['pdf_id'] in bookmarked_pdf_ids %}
          <details class="card-menu">
            <summary aria-label="More options for {{ pdf['title'] }}" title="More options">⋯</summary>
            <div class="card-menu-panel">
              <a href="{{ url_for('pdf_bookmarks', pdf_id=pdf['pdf_id']) }}">View bookmarks</a>
            </div>
          </details>
        {% endif %}
      </div>
    {% else %}
      <p>No PDFs found matching your query.</p>
    {% endfor %}
    
    <div style="margin-top:24px;">
      <a class="btn" href="{{ url_for('rescan') }}">Rescan library</a>
    </div>
  </div>
</body>
</html>
"""

PDF_BOOKMARKS_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23111'/%3E%3Cpath d='M14 16c8-2 14 0 18 5v30c-4-5-10-7-18-5V16zm36 0c-8-2-14 0-18 5v30c4-5 10-7 18-5V16z' fill='%23fff'/%3E%3Cpath d='M32 21v30' stroke='%23d98b00' stroke-width='3'/%3E%3C/svg%3E">
  <title>Bookmarks · {{ pdf['title'] }}</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #f4f4f4; color: #111; }
    header { padding: 16px; background: #111; color: #fff; }
    .wrap { max-width: 900px; margin: 0 auto; padding: 12px; }
    a { color: inherit; text-decoration: none; }
    .back { display: inline-flex; align-items: center; min-height: 44px; padding: 0 14px; border-radius: 10px; background: #2a2a2a; font-weight: 700; }
    .card { display: block; background: #fff; border-left: 4px solid #d98b00; border-radius: 10px 14px 14px 10px; padding: 14px; margin: 10px 0; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .title { font-size: 20px; font-weight: 700; margin: 16px 0; }
    .badge { display: inline-block; background: #fff1d6; color: #9b6200; padding: 4px 8px; border-radius: 6px; font-size: 13px; font-weight: 700; }
  </style>
</head>
<body>
  <header><div class="wrap"><a class="back" href="{{ url_for('index') }}">← Library</a></div></header>
  <main class="wrap">
    <div class="title">Bookmarks · {{ pdf['title'] }}</div>
    {% for page in pages %}
      <a class="card" href="{{ url_for('read_pdf_page', pdf_id=pdf['pdf_id'], page_num=page) }}">
        <span class="badge">Page {{ page }}</span>
      </a>
    {% else %}
      <p>No bookmarks for this PDF.</p>
    {% endfor %}
  </main>
</body>
</html>
"""

READER_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=3">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23111'/%3E%3Cpath d='M14 16c8-2 14 0 18 5v30c-4-5-10-7-18-5V16zm36 0c-8-2-14 0-18 5v30c4-5 10-7 18-5V16z' fill='%23fff'/%3E%3Cpath d='M32 21v30' stroke='%23d98b00' stroke-width='3'/%3E%3C/svg%3E">
  <title>{{ title }}</title>
  <style>
    html, body { margin: 0; padding: 0; height: 100%; background: #111; color: #fff; font-family: system-ui, sans-serif; }
    .top { position: sticky; top: 0; z-index: 5; background: rgba(17,17,17,.95); padding: 10px 12px; display:flex; justify-content:space-between; align-items:center; gap:10px; border-bottom: 1px solid #2a2a2a; }
    .top a { color: #fff; text-decoration: none; }
    .library-btn { display: inline-flex; align-items: center; min-height: 44px; padding: 0 16px; margin-bottom: 4px; background: #2a2a2a; border-radius: 12px; font-size: 18px; font-weight: 700; }
    .meta { font-size: 14px; opacity: .9; }
    .viewer { display: flex; justify-content: center; align-items: flex-start; padding: 8px; min-height: calc(100vh - 150px); }
    img { display: block; background: #222; user-select: none; -webkit-user-drag: none; }
    body.fit-width img { max-width: 100%; height: auto; }
    body.fit-height .viewer { align-items: center; }
    body.fit-height img { width: auto; max-width: none; height: calc(100vh - 154px); max-height: calc(100vh - 154px); }
    .controls { position: fixed; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-between; gap: 8px; padding: 10px; background: rgba(17,17,17,.95); border-top: 1px solid #2a2a2a; }
    .btn { flex: 1; text-align: center; padding: 14px 10px; background: #2a2a2a; border-radius: 12px; color: #fff; text-decoration: none; font-size: 18px; touch-action: manipulation; user-select: none; border: none; cursor: pointer; }
    .btn:active { background: #444; }
    .spacer { height: 76px; }
    .right-actions { display: flex; align-items: center; gap: 12px; }
    .pagejump { display:flex; gap:8px; align-items:center; }
    input[type=number] { width: 90px; font-size: 16px; padding: 8px; border-radius: 10px; border: 1px solid #444; background: #222; color: #fff; }
    
    /* Fullscreen Icon Styling */
    .fullscreen-btn { display: flex; align-items: center; justify-content: center; padding: 10px; background: #2a2a2a; border-radius: 10px; color: #fff; border: none; cursor: pointer; }
    .fullscreen-btn:active { background: #444; }
    .fullscreen-btn svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    .fullscreen-btn.active { color: #ffd37a; }
    .exit-icon { display: none; }
  </style>
</head>
<body class="fit-width">
  <div class="top">
    <div>
      <div><a class="library-btn" href="{{ url_for('index') }}">← Library</a></div>
      <div class="meta">{{ title }} · Page <span id="pageLabel">{{ page }}</span>{% if page_count %} / {{ page_count }}{% endif %}</div>
    </div>
    <div class="right-actions">
      <button id="bookmarkBtn" class="fullscreen-btn{% if bookmarked %} active{% endif %}" onclick="toggleBookmark()" title="Bookmark page" aria-label="Bookmark page" aria-pressed="{{ 'true' if bookmarked else 'false' }}">
        <svg viewBox="0 0 24 24">
          <path d="M19 21l-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
        </svg>
      </button>
      <button id="fitBtn" class="fullscreen-btn" onclick="toggleFitMode()" title="Fit height" aria-label="Toggle fit height">
        <svg viewBox="0 0 24 24">
          <path d="M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4M5 3h14M5 21h14"/>
        </svg>
      </button>
      <button class="fullscreen-btn" onclick="toggleFullscreen()" title="Toggle Fullscreen">
        <svg class="enter-icon" viewBox="0 0 24 24">
          <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>
        </svg>
        <svg class="exit-icon" viewBox="0 0 24 24">
          <path d="M4 14h6v6M20 10h-6V4M14 10l7-7M10 14l-7 7"/>
        </svg>
      </button>
      <div class="pagejump">
        <input id="pageInput" type="number" min="1" max="{{ page_count or 999999 }}" value="{{ page }}">
        <button class="btn" style="flex:none; padding:10px 12px; font-size:14px;" onclick="jumpToPage()">Go</button>
      </div>
    </div>
  </div>

  <div class="viewer">
    <img id="pageImg" src="{{ image_url }}" alt="Page {{ page }}" decoding="async">
  </div>

  <div class="spacer"></div>

  <div class="controls">
    <a id="prevBtn" class="btn" href="{{ prev_url }}">Prev</a>
    <a id="nextBtn" class="btn" href="{{ next_url }}">Next</a>
  </div>

<script>
  const pdfId = "{{ pdf_id }}";
  let currentPage = {{ page }};
  const maxPage = {{ page_count or 999999 }};
  const imageBaseUrl = "{{ image_base_url }}";
  const readerBaseUrl = "{{ base_url }}";
  let bookmarked = {{ 'true' if bookmarked else 'false' }};
  const preloadedPages = new Map();

  function clampPage(page) {
    if (page < 1) return 1;
    if (page > maxPage) return maxPage;
    return page;
  }

  function imageUrl(page) {
    return `${imageBaseUrl}${page}.jpg`;
  }

  function readerUrl(page) {
    return `${readerBaseUrl}${page}`;
  }

  function preloadPage(page) {
    page = clampPage(page);
    if (page === currentPage || preloadedPages.has(page)) return;

    const img = new Image();
    img.decoding = "async";
    img.src = imageUrl(page);
    preloadedPages.set(page, img);
  }

  function preloadNextPage() {
    if (currentPage < maxPage) preloadPage(currentPage + 1);
  }

  function updateNavLinks() {
    document.getElementById("prevBtn").href = readerUrl(clampPage(currentPage - 1));
    document.getElementById("nextBtn").href = readerUrl(clampPage(currentPage + 1));
  }

  function refreshBookmarkForPage(page) {
    fetch(`/pdf/${pdfId}/bookmark?page=${page}`)
      .then((r) => r.json())
      .then((data) => applyBookmarkState(data.bookmarked))
      .catch(() => applyBookmarkState(false));
  }

  function showPage(page, options = {}) {
    page = clampPage(page);
    if (page === currentPage && !options.force) return;

    const pageImg = document.getElementById("pageImg");
    const cachedImg = preloadedPages.get(page);
    pageImg.src = cachedImg ? cachedImg.src : imageUrl(page);
    pageImg.alt = `Page ${page}`;

    currentPage = page;
    document.getElementById("pageLabel").textContent = page;
    document.getElementById("pageInput").value = page;
    updateNavLinks();
    saveProgress(page);
    refreshBookmarkForPage(page);
    preloadNextPage();

    if (!options.replaceHistory && !document.fullscreenElement) {
      history.pushState({page}, "", readerUrl(page));
    }
  }

  function applyFitMode(mode) {
    const fitMode = mode === "height" ? "height" : "width";
    document.body.classList.toggle("fit-height", fitMode === "height");
    document.body.classList.toggle("fit-width", fitMode === "width");

    const fitBtn = document.getElementById("fitBtn");
    fitBtn.classList.toggle("active", fitMode === "height");
    fitBtn.title = fitMode === "height" ? "Fit width" : "Fit height";
    fitBtn.setAttribute("aria-label", fitBtn.title);
    localStorage.setItem("pdfFitMode", fitMode);
  }

  function toggleFitMode() {
    applyFitMode(document.body.classList.contains("fit-height") ? "width" : "height");
  }

  function applyBookmarkState(value) {
    bookmarked = Boolean(value);
    const bookmarkBtn = document.getElementById("bookmarkBtn");
    bookmarkBtn.classList.toggle("active", bookmarked);
    bookmarkBtn.setAttribute("aria-pressed", bookmarked ? "true" : "false");
  }

  function toggleBookmark() {
    fetch(`/pdf/${pdfId}/bookmark`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({page: currentPage, bookmarked: !bookmarked})
    })
      .then((r) => r.json())
      .then((data) => applyBookmarkState(data.bookmarked))
      .catch(() => {});
  }

  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch((err) => {
        console.error(`Error attempting to enable fullscreen: ${err.message}`);
      });
    } else {
      document.exitFullscreen();
    }
  }

  document.addEventListener('fullscreenchange', () => {
    const enterIcon = document.querySelector('.enter-icon');
    const exitIcon = document.querySelector('.exit-icon');
    if (document.fullscreenElement) {
      enterIcon.style.display = 'none';
      exitIcon.style.display = 'block';
    } else {
      enterIcon.style.display = 'block';
      exitIcon.style.display = 'none';
      history.replaceState({page: currentPage}, "", readerUrl(currentPage));
    }
  });

  function saveProgress(page) {
    fetch(`/pdf/${pdfId}/progress`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({page})
    }).catch(() => {});
  }

  function go(page) {
    showPage(page);
  }

  function jumpToPage() {
    const v = parseInt(document.getElementById("pageInput").value, 10);
    if (!isNaN(v)) go(v);
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") go(currentPage - 1);
    if (e.key === "ArrowRight") go(currentPage + 1);
  });

  document.getElementById("prevBtn").addEventListener("click", (e) => {
    e.preventDefault();
    go(currentPage - 1);
  });

  document.getElementById("nextBtn").addEventListener("click", (e) => {
    e.preventDefault();
    go(currentPage + 1);
  });

  window.addEventListener("popstate", (e) => {
    if (e.state && e.state.page) {
      showPage(e.state.page, {replaceHistory: true});
      return;
    }

    const match = window.location.pathname.match(/\/page\/(\d+)$/);
    if (match) showPage(parseInt(match[1], 10), {replaceHistory: true});
  });

  // Save progress when page loads.
  applyFitMode(localStorage.getItem("pdfFitMode") || "width");
  history.replaceState({page: currentPage}, "", readerUrl(currentPage));
  updateNavLinks();
  preloadNextPage();
  saveProgress(currentPage);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    reader_id = get_reader_id()
    q = (request.args.get("q") or "").strip().lower()
    items = sorted_pdfs()
    if q:
        items = [
            p for p in items
            if q in p.get("title", "").lower() or q in p.get("folder", "").lower() or q in p.get("path", "").lower()
        ]
    
    # Fetch recent reads only when not searching to keep layout clean
    recents = get_recent_reads(reader_id, limit=5)
    bookmarked_pdf_ids = set(bookmarks_by_reader.get(reader_id, {}))
    
    return render_template_string(
        INDEX_HTML,
        pdfs=items,
        recents=recents,
        bookmarked_pdf_ids=bookmarked_pdf_ids,
        count=len(items),
        q=q,
        library_dir=str(LIBRARY_DIR),
    )


@app.route("/bookmarks/<pdf_id>")
def pdf_bookmarks(pdf_id: str):
    pdf = get_pdf(pdf_id)
    reader_id = get_reader_id()
    pages = sorted(bookmarks_by_reader.get(reader_id, {}).get(pdf_id, set()))
    return render_template_string(PDF_BOOKMARKS_HTML, pdf=pdf, pages=pages)


@app.route("/rescan")
def rescan():
    scan_library()
    return redirect(url_for("index"))


@app.route("/read/<pdf_id>")
def read_pdf(pdf_id: str):
    pdf = get_pdf(pdf_id)
    count = ensure_page_count(pdf_id)

    reader_id = get_reader_id()
    start_page = get_progress(reader_id, pdf_id)
    if start_page < 1:
        start_page = 1
    if count and start_page > count:
        start_page = count

    base_url = url_for("read_pdf_page", pdf_id=pdf_id, page_num=1)
    base_url = base_url.rsplit("/", 1)[0] + "/"
    image_base_url = url_for("pdf_page", pdf_id=pdf_id, page_num=1)
    image_base_url = image_base_url.rsplit("/", 1)[0] + "/"

    return render_template_string(
        READER_HTML,
        title=pdf["title"],
        pdf_id=pdf_id,
        page=start_page,
        page_count=count,
        image_url=url_for("pdf_page", pdf_id=pdf_id, page_num=start_page),
        image_base_url=image_base_url,
        prev_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=max(1, start_page - 1)),
        next_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=min(count, start_page + 1)) if count else url_for("read_pdf_page", pdf_id=pdf_id, page_num=start_page + 1),
        base_url=base_url,
        bookmarked=is_bookmarked(reader_id, pdf_id, start_page),
    )


@app.route("/read/<pdf_id>/page/<int:page_num>")
def read_pdf_page(pdf_id: str, page_num: int):
    pdf = get_pdf(pdf_id)
    count = ensure_page_count(pdf_id)
    if count:
        page_num = max(1, min(page_num, count))
    else:
        page_num = max(1, page_num)

    reader_id = get_reader_id()
    set_progress(reader_id, pdf_id, page_num)

    base_url = url_for("read_pdf_page", pdf_id=pdf_id, page_num=1)
    base_url = base_url.rsplit("/", 1)[0] + "/"
    image_base_url = url_for("pdf_page", pdf_id=pdf_id, page_num=1)
    image_base_url = image_base_url.rsplit("/", 1)[0] + "/"

    return render_template_string(
        READER_HTML,
        title=pdf["title"],
        pdf_id=pdf_id,
        page=page_num,
        page_count=count,
        image_url=url_for("pdf_page", pdf_id=pdf_id, page_num=page_num),
        image_base_url=image_base_url,
        prev_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=max(1, page_num - 1)),
        next_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=min(count, page_num + 1)) if count else url_for("read_pdf_page", pdf_id=pdf_id, page_num=page_num + 1),
        base_url=base_url,
        bookmarked=is_bookmarked(reader_id, pdf_id, page_num),
    )


@app.route("/pdf/<pdf_id>/page/<int:page_num>.jpg")
def pdf_page(pdf_id: str, page_num: int):
    pdf = get_pdf(pdf_id)
    count = ensure_page_count(pdf_id)
    if page_num < 1 or page_num > count:
        abort(404, "Page out of range")

    img_path = render_page_to_jpg(pdf_id, page_num)
    return send_file(img_path, mimetype="image/jpeg", conditional=True, max_age=60 * 60 * 24 * 365)


@app.route("/pdf/<pdf_id>/progress", methods=["GET", "POST"])
def progress(pdf_id: str):
    get_pdf(pdf_id)
    reader_id = get_reader_id()

    if request.method == "GET":
        return jsonify({
            "pdf_id": pdf_id,
            "reader_id": reader_id,
            "current_page": get_progress(reader_id, pdf_id),
        })

    data = request.get_json(silent=True) or {}
    page = int(data.get("page", 1))
    set_progress(reader_id, pdf_id, page)
    return jsonify({"ok": True, "page": page})


@app.route("/pdf/<pdf_id>/bookmark", methods=["GET", "POST"])
def bookmark(pdf_id: str):
    count = ensure_page_count(pdf_id)
    reader_id = get_reader_id()

    if request.method == "GET":
        page = int(request.args.get("page", get_progress(reader_id, pdf_id)))
        page = max(1, min(page, count))
        return jsonify({
            "pdf_id": pdf_id,
            "page": page,
            "bookmarked": is_bookmarked(reader_id, pdf_id, page),
        })

    data = request.get_json(silent=True) or {}
    page = int(data.get("page", get_progress(reader_id, pdf_id)))
    page = max(1, min(page, count))
    bookmarked = bool(data.get("bookmarked", True))
    bookmarked = set_bookmark(reader_id, pdf_id, page, bookmarked)
    return jsonify({"ok": True, "page": page, "bookmarked": bookmarked})


@app.route("/admin/rebuild-cache/<pdf_id>")
def rebuild_cache(pdf_id: str):
    get_pdf(pdf_id)
    clear_pdf_cache(pdf_id)
    return jsonify({"ok": True, "message": "Cache cleared"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
