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

JPEG_QUALITY = 80
RENDER_ZOOM = 1.8  # Higher = sharper, larger files
COOKIE_NAME = "reader_id"

app = Flask(__name__)

csv_lock = threading.Lock()
render_locks: Dict[str, threading.Lock] = {}

# In-memory indexes
pdfs_by_id: Dict[str, dict] = {}
progress_by_reader: Dict[str, Dict[str, int]] = {}


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
    global pdfs_by_id, progress_by_reader

    pdfs_by_id = {}
    for row in csv_read_dicts(PDFS_CSV):
        pdfs_by_id[row["pdf_id"]] = row

    progress_by_reader = {}
    for row in csv_read_dicts(PROGRESS_CSV):
        rid = row["reader_id"]
        pid = row["pdf_id"]
        page = int(row["current_page"])
        progress_by_reader.setdefault(rid, {})[pid] = page


def save_pdfs_state() -> None:
    rows = list(pdfs_by_id.values())
    rows.sort(key=lambda r: (r.get("folder", ""), r.get("title", "")))
    csv_write_dicts_atomic(
        PDFS_CSV,
        ["pdf_id", "path", "title", "folder", "mtime", "page_count"],
        rows,
    )


def save_progress_state() -> None:
    rows = []
    for rid, pdf_map in progress_by_reader.items():
        for pid, page in pdf_map.items():
            rows.append(
                {
                    "reader_id": rid,
                    "pdf_id": pid,
                    "current_page": str(page),
                    "last_read": str(int(time.time())),
                }
            )
    csv_write_dicts_atomic(
        PROGRESS_CSV,
        ["reader_id", "pdf_id", "current_page", "last_read"],
        rows,
    )


def get_reader_id() -> str:
    rid = request.cookies.get(COOKIE_NAME)
    if not rid:
        rid = uuid.uuid4().hex
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


def sorted_pdfs() -> List[dict]:
    return sorted(
        pdfs_by_id.values(),
        key=lambda r: (r.get("folder", ""), r.get("title", "").lower()),
    )


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
    rid = request.cookies.get(COOKIE_NAME)
    if not rid:
        resp.set_cookie(
            COOKIE_NAME,
            get_reader_id(),
            max_age=60 * 60 * 24 * 365 * 5,
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
  <title>PDF Library</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #f4f4f4; color: #111; }
    header { padding: 16px; background: #111; color: #fff; position: sticky; top: 0; }
    .wrap { max-width: 900px; margin: 0 auto; padding: 12px; }
    .card { background: #fff; border-radius: 14px; padding: 14px; margin: 10px 0; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .title { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
    .meta { color: #555; font-size: 14px; }
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
    {% for pdf in pdfs %}
      <div class="card">
        <a href="{{ url_for('read_pdf', pdf_id=pdf['pdf_id']) }}">
          <div class="title">{{ pdf['title'] }}</div>
          <div class="meta">
            {% if pdf['folder'] %}{{ pdf['folder'] }} / {% endif %}
            {% if pdf['page_count'] %}{{ pdf['page_count'] }} pages{% else %}page count unknown{% endif %}
          </div>
        </a>
      </div>
    {% else %}
      <p>No PDFs found in <code>{{ library_dir }}</code>.</p>
    {% endfor %}
    <div style="margin-top:16px;">
      <a class="btn" href="{{ url_for('rescan') }}">Rescan library</a>
    </div>
  </div>
</body>
</html>
"""

READER_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=3">
  <title>{{ title }}</title>
  <style>
    html, body { margin: 0; padding: 0; height: 100%; background: #111; color: #fff; font-family: system-ui, sans-serif; }
    .top { position: sticky; top: 0; z-index: 5; background: rgba(17,17,17,.95); padding: 10px 12px; display:flex; justify-content:space-between; align-items:center; gap:10px; border-bottom: 1px solid #2a2a2a; }
    .top a { color: #fff; text-decoration: none; }
    .meta { font-size: 14px; opacity: .9; }
    .viewer { display: flex; justify-content: center; align-items: flex-start; padding: 8px; }
    img { max-width: 100%; height: auto; display: block; background: #222; }
    .controls { position: fixed; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-between; gap: 8px; padding: 10px; background: rgba(17,17,17,.95); border-top: 1px solid #2a2a2a; }
    .btn { flex: 1; text-align: center; padding: 14px 10px; background: #2a2a2a; border-radius: 12px; color: #fff; text-decoration: none; font-size: 18px; touch-action: manipulation; user-select: none; }
    .btn:active { background: #444; }
    .spacer { height: 76px; }
    .pagejump { display:flex; gap:8px; align-items:center; }
    input[type=number] { width: 90px; font-size: 16px; padding: 8px; border-radius: 10px; border: 1px solid #444; background: #222; color: #fff; }
  </style>
</head>
<body>
  <div class="top">
    <div>
      <div><a href="{{ url_for('index') }}">← Library</a></div>
      <div class="meta">{{ title }} · Page <span id="pageLabel">{{ page }}</span>{% if page_count %} / {{ page_count }}{% endif %}</div>
    </div>
    <div class="pagejump">
      <input id="pageInput" type="number" min="1" max="{{ page_count or 999999 }}" value="{{ page }}">
      <button class="btn" style="flex:none; padding:10px 12px; font-size:14px;" onclick="jumpToPage()">Go</button>
    </div>
  </div>

  <div class="viewer">
    <img id="pageImg" src="{{ image_url }}" alt="Page {{ page }}">
  </div>

  <div class="spacer"></div>

  <div class="controls">
    <a class="btn" href="{{ prev_url }}">Prev</a>
    <a class="btn" href="{{ next_url }}">Next</a>
  </div>

<script>
  const pdfId = "{{ pdf_id }}";
  let currentPage = {{ page }};
  const maxPage = {{ page_count or 999999 }};

  function saveProgress(page) {
    fetch(`/pdf/${pdfId}/progress`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({page})
    }).catch(() => {});
  }

  function go(page) {
    if (page < 1) page = 1;
    if (page > maxPage) page = maxPage;
    window.location.href = `{{ base_url }}` + page;
  }

  function jumpToPage() {
    const v = parseInt(document.getElementById("pageInput").value, 10);
    if (!isNaN(v)) go(v);
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") go(currentPage - 1);
    if (e.key === "ArrowRight") go(currentPage + 1);
  });

  let startX = null;
  document.addEventListener("touchstart", (e) => {
    if (e.touches && e.touches.length === 1) startX = e.touches[0].clientX;
  }, {passive:true});
  document.addEventListener("touchend", (e) => {
    if (startX === null) return;
    const endX = e.changedTouches[0].clientX;
    const dx = endX - startX;
    if (Math.abs(dx) > 50) {
      if (dx < 0) go(currentPage + 1);
      else go(currentPage - 1);
    }
    startX = null;
  }, {passive:true});

  // Save progress when page loads.
  saveProgress(currentPage);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    q = (request.args.get("q") or "").strip().lower()
    items = sorted_pdfs()
    if q:
        items = [
            p for p in items
            if q in p.get("title", "").lower() or q in p.get("folder", "").lower() or q in p.get("path", "").lower()
        ]
    return render_template_string(
        INDEX_HTML,
        pdfs=items,
        count=len(items),
        q=q,
        library_dir=str(LIBRARY_DIR),
    )


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

    base_url = url_for("pdf_page", pdf_id=pdf_id, page_num=1)
    # We'll replace trailing 1 with the chosen page in JS/CSS-safe way
    base_url = base_url.rsplit("/", 1)[0] + "/"

    return render_template_string(
        READER_HTML,
        title=pdf["title"],
        pdf_id=pdf_id,
        page=start_page,
        page_count=count,
        image_url=url_for("pdf_page", pdf_id=pdf_id, page_num=start_page),
        prev_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=max(1, start_page - 1)),
        next_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=min(count, start_page + 1)) if count else url_for("read_pdf_page", pdf_id=pdf_id, page_num=start_page + 1),
        base_url=base_url,
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

    return render_template_string(
        READER_HTML,
        title=pdf["title"],
        pdf_id=pdf_id,
        page=page_num,
        page_count=count,
        image_url=url_for("pdf_page", pdf_id=pdf_id, page_num=page_num),
        prev_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=max(1, page_num - 1)),
        next_url=url_for("read_pdf_page", pdf_id=pdf_id, page_num=min(count, page_num + 1)) if count else url_for("read_pdf_page", pdf_id=pdf_id, page_num=page_num + 1),
        base_url=base_url,
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
    get_pdf(pdf_id)  # validate exists
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


@app.route("/admin/rebuild-cache/<pdf_id>")
def rebuild_cache(pdf_id: str):
    get_pdf(pdf_id)
    clear_pdf_cache(pdf_id)
    return jsonify({"ok": True, "message": "Cache cleared"})


if __name__ == "__main__":
    # On a tablet-friendly LAN setup, use 0.0.0.0 so other devices can reach it.
    app.run(host="0.0.0.0", port=8000, debug=True)