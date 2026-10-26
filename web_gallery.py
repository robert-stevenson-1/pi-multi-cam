import os
import re
import json
import datetime
import tempfile
import zipfile
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def load_env(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env()

PORT = int(os.getenv("PORT", "8088"))
CAPTURE_REFRESH_MS = int(os.getenv("CAPTURE_REFRESH_MS", "1000"))
CAPTURES_DIR = os.getenv("CAPTURES_DIR", "captures")
PRIMARY_HOST = os.getenv("PRIMARY_HOST", "127.0.0.1")
PRIMARY_PORT = int(os.getenv("PRIMARY_PORT", "8080"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def jpeg_size(path):
    with open(path, "rb") as f:
        data = f.read(65536)
    if len(data) < 12 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 8 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return ((data[i + 7] << 8) | data[i + 8], (data[i + 5] << 8) | data[i + 6])
        i += 2 + ((data[i + 2] << 8) | data[i + 3])
    return None


def batch_epoch(name):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_", name)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            m.group(1) + m.group(2) + m.group(3) + "_" + m.group(4) + m.group(5) + m.group(6),
            "%Y%m%d_%H%M%S",
        ).timestamp()
    except ValueError:
        return None


def session_created(s):
    created = s.get("created")
    if created:
        return created
    eps = [batch_epoch(b) for b in s.get("batches", [])]
    eps = [e for e in eps if e is not None]
    return min(eps) if eps else 0.0


def format_batch_display(name):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_([a-f0-9]+)$", name)
    if m:
        year, month, day, hour, minute, sec, hsh = m.groups()
        return f"{year}-{month}-{day} {hour}:{minute}:{sec}", hsh
    return name, None


def parse_device_badge(img_name):
    base, _ = os.path.splitext(img_name)
    parts = base.split("_")
    if len(parts) >= 2:
        return f"{parts[0]} · {parts[1]}"
    return base


def gallery_cards_html(sections):
    if not sections:
        return """
        <div class="empty-state">
            <div class="empty-icon">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path>
                    <circle cx="12" cy="13" r="4"></circle>
                </svg>
            </div>
            <h2>No captures yet</h2>
            <p>Click <strong>Capture All</strong> in the toolbar above or trigger the GPIO button to take multi-camera captures.</p>
        </div>
        """

    sections_html = []
    for title, cardlist, is_session, created in sections:
        total_photos = sum(len(entries) for _, entries in cardlist)
        total_batches = len(cardlist)
        safe_title = urllib.parse.quote(title, safe="") if title else "ungrouped"
        session_key = f"sess_{safe_title}"
        session_batch_attr = (
            f' data-session-name="{urllib.parse.quote(title, safe="")}"'
            if is_session and title else ""
        )

        batch_cards = []
        for name, entries in cardlist:
            formatted_time, hsh = format_batch_display(name)
            hsh_badge = f'<span class="hash-tag">{hsh}</span>' if hsh else ""
            del_batch = (
                f'<span class="del-wrap">'
                f'<button class="btn-zip btn-del" onclick="showDelConfirm(this)">Delete</button>'
                f'<span class="del-confirm" hidden>'
                f'<button class="btn-zip btn-del-files" onclick="deleteBatch(this)">Delete batch</button>'
                f'<button class="btn-zip" onclick="hideDelConfirm(this)">&#10005;</button>'
                f'</span>'
                f'</span>'
            )
            thumbs = "".join(
                f'<figure class="thumb-card" data-img="{img}">'
                f'<div class="thumb-media">'
                f'<span class="dev-badge">{parse_device_badge(img)}</span>'
                f'<a href="{name}/{img}" data-pswp-width="{w}" data-pswp-height="{h}" class="pswp-link">'
                f'<img src="{name}/{thumb or img}" loading="lazy" alt="{img}">'
                f'<div class="thumb-overlay"><span class="view-pill">View Full</span></div>'
                f'</a>'
                f'</div>'
                f'<figcaption><span class="fname" title="{img}">{img}</span>'
                f'<a class="btn-save" href="{name}/{img}" download title="Download file">'
                f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> <span class="save-label">Save</span></a>'
                f'</figcaption></figure>'
                for img, thumb, w, h in entries
            )

            batch_cards.append(
                f'<div class="batch-card" data-batch-name="{name}"{session_batch_attr}>'
                f'<div class="batch-header">'
                f'<div class="batch-title-group">'
                f'<span class="batch-time">{formatted_time}</span>{hsh_badge}'
                f'<span class="count-pill">{len(entries)} photo{"s" if len(entries) != 1 else ""}</span>'
                f'</div>'
                f'<a class="btn-zip" href="/zip/{name}">'
                f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Batch ZIP</a>'
                f'{del_batch}'
                f'</div>'
                f'<div class="grid">{thumbs}</div>'
                f'</div>'
            )

        dl_session = ""
        del_btn = ""
        cont_btn = ""
        if is_session and title:
            cont_btn = (
                f'<button class="btn-zip btn-session-zip" onclick="event.stopPropagation();continueSession(this)">'
                f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> Continue</button>'
            )
            dl_session = (
                f'<a class="btn-zip btn-session-zip" href="/zip/session/{urllib.parse.quote(title)}" onclick="event.stopPropagation()">'
                f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Session ZIP</a>'
            )
            del_btn = (
                f'<span class="del-wrap" onclick="event.stopPropagation()">'
                f'<button class="btn-zip btn-del" onclick="showDelConfirm(this)">Delete</button>'
                f'<span class="del-confirm" hidden>'
                f'<button class="btn-zip" onclick="deleteSession(this)">Keep files</button>'
                f'<button class="btn-zip btn-del-files" onclick="deleteSession(this, true)">Delete files</button>'
                f'<button class="btn-zip" onclick="hideDelConfirm(this)">&#10005;</button>'
                f'</span>'
                f'</span>'
            )

        sections_html.append(
            f'<details class="session-accordion" data-session-key="{session_key}" data-session-name="{safe_title}" data-session-created="{created}" data-is-session="{1 if is_session and title else 0}">'
            f'<summary class="session-header">'
            f'<div class="session-header-left">'
            f'<label class="sel-check"><input type="checkbox" class="sel-sess" data-name="{safe_title}" onclick="event.stopPropagation()"></label>'
            f'<span class="chevron"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg></span>'
            f'<span class="session-title">{title if title else "Ungrouped Captures"}</span>'
            f'<div class="session-meta-pills">'
            f'<span class="meta-pill">{total_batches} batch{"es" if total_batches != 1 else ""}</span>'
            f'<span class="meta-pill">{total_photos} photo{"s" if total_photos != 1 else ""}</span>'
            f'</div>'
            f'</div>'
            f'<div class="session-header-right">{cont_btn}{dl_session}{del_btn}</div>'
            f'</summary>'
            f'<div class="session-body">'
            f'{"".join(batch_cards)}'
            f'</div>'
            f'</details>'
        )

    return "".join(sections_html)


PAGE_CSS = '''
:root {
  --bg-page: #f8fafc;
  --bg-card: #ffffff;
  --bg-elevated: #f1f5f9;
  --text-main: #0f172a;
  --text-muted: #64748b;
  --text-light: #94a3b8;
  --border-subtle: #e2e8f0;
  --border-strong: #cbd5e1;
  --accent-primary: #2563eb;
  --accent-primary-hover: #1d4ed8;
  --accent-success: #10b981;
  --accent-danger: #ef4444;
  --accent-danger-hover: #dc2626;
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.07), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.04);
}

* { box-sizing: border-box; margin: 0; padding: 0; }
[hidden] { display: none !important; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg-page);
  color: var(--text-main);
  line-height: 1.5;
  padding-bottom: 5rem;
}

.app-header {
  position: sticky;
  top: 0;
  z-index: 50;
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-sm);
}

.header-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0.85rem 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.header-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}

.brand-section {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.app-title {
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--text-main);
  letter-spacing: -0.02em;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.status-pills-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.live-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.25rem 0.65rem;
  border-radius: 9999px;
  background: #f1f5f9;
  color: #475569;
  border: 1px solid var(--border-subtle);
}

.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #94a3b8;
}

.live-pill.online { background: #ecfdf5; color: #065f46; border-color: #a7f3d0; }
.live-pill.online .live-dot { background: #10b981; box-shadow: 0 0 6px #10b981; }
.live-pill.offline { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
.live-pill.offline .live-dot { background: #ef4444; }
.live-pill.active-session { background: #eff6ff; color: #1e40af; border-color: #bfdbfe; }
.live-pill.warn { background: #fffbeb; color: #92400e; border-color: #fde68a; }
.live-pill.warn .live-dot { background: #f59e0b; }

.controls-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}

.actions-group {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.session-form-group {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.input-text {
  font-size: 0.85rem;
  padding: 0.45rem 0.75rem;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: #ffffff;
  color: var(--text-main);
  outline: none;
  min-width: 140px;
  transition: border-color 0.15s ease;
}
.input-text:focus {
  border-color: var(--accent-primary);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  font-size: 0.85rem;
  font-weight: 600;
  padding: 0.45rem 0.85rem;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-card);
  color: var(--text-main);
  cursor: pointer;
  text-decoration: none;
  transition: all 0.15s ease;
  user-select: none;
}

.btn:hover {
  background: var(--bg-elevated);
  border-color: var(--border-strong);
}

.btn-primary {
  background: var(--accent-primary);
  color: #ffffff;
  border-color: var(--accent-primary);
}
.btn-primary:hover {
  background: var(--accent-primary-hover);
  border-color: var(--accent-primary-hover);
}

.btn-danger {
  color: var(--accent-danger);
}
.btn-danger:hover {
  background: #fef2f2;
  border-color: #fecaca;
}

.btn-icon-only {
  padding: 0.45rem 0.55rem;
}

.main-content {
  max-width: 1200px;
  margin: 1.5rem auto;
  padding: 0 1.25rem;
}

.gallery-subbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 1rem;
}

.view-controls {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.sort-group {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-muted);
}

.select-sort {
  font-size: 0.78rem;
  padding: 0.3rem 0.45rem;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: #ffffff;
  color: var(--text-main);
  outline: none;
  cursor: pointer;
}
.select-sort:focus {
  border-color: var(--accent-primary);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}

.sel-check {
  display: none;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-muted);
  cursor: pointer;
  touch-action: manipulation;
}
.sel-check input[type="checkbox"] {
  display: block;
  width: 1.05rem;
  height: 1.05rem;
  accent-color: var(--accent-primary);
  cursor: pointer;
}
body.select-mode .sel-check {
  display: inline-flex;
}
body.select-mode .session-accordion {
  border-color: var(--border-strong);
}
.btn-batch-dl {
  font-size: 0.8rem;
  padding: 0.35rem 0.75rem;
}
.btn-batch-dl:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-text {
  background: none;
  border: none;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0.25rem 0.5rem;
  border-radius: var(--radius-sm);
}
.btn-text:hover {
  color: var(--text-main);
  background: var(--bg-elevated);
}

.session-accordion {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  margin-bottom: 1.25rem;
  box-shadow: var(--shadow-sm);
  overflow: hidden;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}

.session-accordion[open] {
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}

.session-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.9rem 1.25rem;
  background: #ffffff;
  cursor: pointer;
  user-select: none;
  list-style: none;
  transition: background 0.15s ease;
}
.session-header::-webkit-details-marker { display: none; }

.session-header:hover {
  background: var(--bg-elevated);
}

.session-header-left {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.chevron {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

.session-accordion[open] .chevron {
  transform: rotate(90deg);
}

.session-title {
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--text-main);
  letter-spacing: -0.01em;
}

.session-meta-pills {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}

.meta-pill {
  font-size: 0.75rem;
  font-weight: 500;
  padding: 0.15rem 0.5rem;
  border-radius: 9999px;
  background: var(--bg-elevated);
  color: var(--text-muted);
}

.session-body {
  padding: 1rem 1.25rem;
  border-top: 1px solid var(--border-subtle);
  background: #fafbfc;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.batch-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 1rem;
  box-shadow: var(--shadow-sm);
}

.batch-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.85rem;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.batch-title-group {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.batch-time {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-main);
}

.hash-tag {
  font-family: ui-monospace, monospace;
  font-size: 0.75rem;
  background: var(--bg-elevated);
  color: var(--text-muted);
  padding: 0.15rem 0.4rem;
  border-radius: 4px;
  border: 1px solid var(--border-subtle);
}

.count-pill {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.btn-zip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-muted);
  text-decoration: none;
  padding: 0.25rem 0.6rem;
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  transition: all 0.15s ease;
}
.btn-zip:hover {
  color: var(--accent-primary);
  border-color: var(--accent-primary);
  background: #ffffff;
}

.btn-session-zip {
  font-size: 0.8rem;
  padding: 0.35rem 0.75rem;
}

.btn-del {
  color: #b91c1c;
  border-color: #fecaca;
}
.btn-del:hover {
  color: #ffffff;
  border-color: #dc2626;
  background: #dc2626;
}
.btn-del-files {
  color: #ffffff;
  background: #dc2626;
  border-color: #dc2626;
}
.del-wrap {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
}
.del-confirm {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
}
.del-confirm[hidden], .btn-del[hidden] { display: none; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 0.85rem;
}

.thumb-card {
  position: relative;
  background: #ffffff;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.thumb-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}

.thumb-media {
  position: relative;
  width: 100%;
  aspect-ratio: 4/3;
  background: #e2e8f0;
  overflow: hidden;
}

.thumb-media img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.dev-badge {
  position: absolute;
  top: 0.45rem;
  left: 0.45rem;
  z-index: 2;
  background: rgba(15, 23, 42, 0.75);
  backdrop-filter: blur(4px);
  color: #ffffff;
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.2rem 0.5rem;
  border-radius: 4px;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.thumb-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.25);
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.thumb-card:hover .thumb-overlay {
  opacity: 1;
}

.view-pill {
  background: rgba(255, 255, 255, 0.95);
  color: var(--text-main);
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.35rem 0.75rem;
  border-radius: 9999px;
  box-shadow: var(--shadow-sm);
}

figcaption {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.5rem 0.65rem;
  font-size: 0.75rem;
  background: #ffffff;
  border-top: 1px solid var(--border-subtle);
}

.fname {
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 150px;
}

.btn-save {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  color: var(--accent-primary);
  text-decoration: none;
  font-weight: 600;
}
.btn-save:hover {
  text-decoration: underline;
}

.empty-state {
  text-align: center;
  padding: 4rem 1.5rem;
  background: var(--bg-card);
  border: 2px dashed var(--border-strong);
  border-radius: var(--radius-lg);
  margin: 2rem 0;
}
.empty-icon {
  color: var(--text-light);
  margin-bottom: 1rem;
}
.empty-state h2 {
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--text-main);
  margin-bottom: 0.5rem;
}
.empty-state p {
  color: var(--text-muted);
  font-size: 0.9rem;
  max-width: 480px;
  margin: 0 auto;
}

.toast-container {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  z-index: 100;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  pointer-events: none;
}

.toast {
  pointer-events: auto;
  min-width: 260px;
  max-width: 400px;
  padding: 0.75rem 1rem;
  background: #ffffff;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-lg);
  font-size: 0.85rem;
  color: var(--text-main);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  animation: toastIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}
.toast.success { border-left: 4px solid var(--accent-success); }
.toast.error { border-left: 4px solid var(--accent-danger); }
.toast.info { border-left: 4px solid var(--accent-primary); }

@keyframes toastIn {
  from { transform: translateY(16px); opacity: 0; }
  to { transform: translateY(0); opacity: 1; }
}

.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  backdrop-filter: blur(4px);
  z-index: 90;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 1.25rem;
}
.modal-backdrop.active {
  display: flex;
}

.modal-box {
  background: #ffffff;
  border-radius: var(--radius-md);
  max-width: 460px;
  width: 100%;
  padding: 1.5rem;
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--border-subtle);
  animation: modalIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes modalIn {
  from { transform: scale(0.96); opacity: 0; }
  to { transform: scale(1); opacity: 1; }
}

.mobile-bar {
  display: none;
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 60;
  align-items: center;
  gap: 0.6rem;
  padding: 0.6rem 0.9rem calc(0.6rem + env(safe-area-inset-bottom));
  background: rgba(255, 255, 255, 0.97);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-top: 1px solid var(--border-subtle);
  box-shadow: 0 -4px 12px rgba(0, 0, 0, 0.08);
}
.btn-capture-mobile {
  flex: 1;
  padding: 0.95rem 1rem;
  font-size: 1rem;
  border-radius: var(--radius-md);
}
.overflow-wrap {
  position: relative;
}
.overflow-menu {
  position: absolute;
  bottom: calc(100% + 0.5rem);
  right: 0;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 0.35rem;
  background: #ffffff;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 0.5rem;
  box-shadow: var(--shadow-lg);
  min-width: 200px;
}
.overflow-menu .btn {
  justify-content: flex-start;
}

.modal-box h3 {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--text-main);
  margin-bottom: 0.5rem;
}
.modal-box p {
  font-size: 0.9rem;
  color: var(--text-muted);
  line-height: 1.45;
  margin-bottom: 1.25rem;
}
.modal-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.6rem;
}

.spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-radius: 50%;
  border-top-color: #ffffff;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 640px) {
  body {
    padding-bottom: 5.75rem;
    -webkit-tap-highlight-color: transparent;
  }
  .btn, .btn-zip, .btn-save, .summary, summary, .select-sort, .btn-text {
    touch-action: manipulation;
  }
  .app-header {
    background: rgba(255, 255, 255, 0.97);
  }
  .header-container {
    padding: 0.6rem 0.75rem;
    gap: 0.5rem;
  }
  .app-title {
    font-size: 1.05rem;
  }
  .status-pills-bar {
    gap: 0.3rem;
  }
  .live-pill {
    font-size: 0.68rem;
    padding: 0.2rem 0.5rem;
  }
  .controls-bar {
    flex-direction: column;
    align-items: stretch;
    gap: 0.5rem;
  }
  .actions-group {
    display: none;
  }
  .session-form-group {
    justify-content: space-between;
  }
  .input-text {
    font-size: 16px;
    min-width: 0;
    flex: 1;
  }
  .btn {
    min-height: 44px;
  }
  .main-content {
    margin-top: 1rem;
    padding: 0 0.6rem;
  }
  .gallery-subbar {
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .view-controls {
    gap: 0.4rem;
  }
  .grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.55rem;
  }
  .dev-badge {
    font-size: 0.6rem;
    padding: 0.15rem 0.35rem;
  }
  .thumb-overlay {
    display: none;
  }
  figcaption {
    padding: 0.35rem 0.45rem;
    gap: 0.3rem;
  }
  .fname {
    max-width: none;
    flex: 1;
    min-width: 0;
    font-size: 0.62rem;
  }
  .btn-save .save-label {
    display: none;
  }
  .btn-save {
    padding: 0.3rem;
  }
  .batch-card {
    padding: 0.6rem;
  }
  .batch-header {
    margin-bottom: 0.6rem;
  }
  .session-body {
    padding: 0.75rem;
    gap: 0.75rem;
  }
  .session-header {
    padding: 0.7rem 0.75rem;
  }
  .session-header-right {
    flex-wrap: wrap;
    gap: 0.3rem;
  }
  .toast-container {
    bottom: 5.75rem;
    left: 0.75rem;
    right: 0.75rem;
  }
  .toast {
    min-width: 0;
    max-width: none;
  }
  .modal-backdrop {
    padding: 0.75rem;
  }
  .modal-box {
    padding: 1.1rem;
  }
  .mobile-bar {
    display: flex;
  }
}

.mode-tabs {
  display: inline-flex;
  align-items: center;
  gap: 0.15rem;
  border: 1px solid var(--border-subtle);
  border-radius: 9999px;
  background: var(--bg-elevated);
  padding: 0.2rem;
}
.mode-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  font-weight: 600;
  padding: 0.35rem 0.9rem;
  border-radius: 9999px;
  color: var(--text-muted);
  text-decoration: none;
  transition: all 0.15s ease;
}
.mode-tab:hover {
  background: #ffffff;
  color: var(--text-main);
}
.mode-tab.current {
  background: var(--accent-primary);
  color: #ffffff;
}
.mode-tab.locked {
  color: var(--text-light);
  cursor: not-allowed;
}

.rec-pill {
  background: #fef2f2;
  color: #991b1b;
  border-color: #fecaca;
}
.rec-pill .live-dot {
  background: #ef4444;
  box-shadow: 0 0 6px #ef4444;
  animation: recBlink 1s infinite;
}
@keyframes recBlink {
  50% { opacity: 0.3; }
}

.settings-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 1.1rem 1.25rem;
  box-shadow: var(--shadow-sm);
  margin-bottom: 1.25rem;
}
.record-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
}
.settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 0.75rem;
  margin: 0.9rem 0 0.75rem;
}
.set-field label {
  display: block;
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 0.3rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.set-field select,
.set-field input {
  width: 100%;
  font-size: 0.85rem;
  padding: 0.45rem 0.6rem;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: #ffffff;
  color: var(--text-main);
  outline: none;
}
.set-field select:focus,
.set-field input:focus {
  border-color: var(--accent-primary);
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}
.settings-note {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin: 0;
}
.highres-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  margin-top: 0.75rem;
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-main);
  cursor: pointer;
}
.highres-toggle input {
  width: 1rem;
  height: 1rem;
  accent-color: var(--accent-primary);
}
.highres-warning {
  margin-top: 0.35rem;
  color: #92400e;
  font-size: 0.76rem;
}
.upload-refresh-option {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  margin-top: 0.65rem;
  color: var(--text-muted);
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
}
.upload-refresh-option input {
  width: 1rem;
  height: 1rem;
  accent-color: var(--accent-primary);
}
.upload-refresh-status {
  color: var(--text-muted);
  font-size: 0.78rem;
  margin-left: 0.5rem;
}
.btn-record {
  padding: 0.7rem 1.4rem;
  font-size: 0.95rem;
  border-radius: 9999px;
}
.btn-stop {
  background: var(--accent-danger);
  border-color: var(--accent-danger);
  color: #ffffff;
}
.btn-stop:hover {
  background: var(--accent-danger-hover);
  border-color: var(--accent-danger-hover);
}
.vid-media {
  display: flex;
  align-items: center;
  justify-content: center;
}
.vid-media a {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-light);
}
.vid-media a:hover {
  color: var(--accent-primary);
}
.vid-media svg {
  width: 38px;
  height: 38px;
}

@media (max-width: 640px) {
  .settings-grid {
    grid-template-columns: 1fr 1fr;
  }
  .btn-record {
    flex: 1;
    min-height: 44px;
  }
}
'''


VIDEO_EXTENSIONS = (".mp4", ".h264")


def format_size(n):
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def mode_tabs(current):
    photo_cur = " current" if current == "photo" else ""
    video_cur = " current" if current == "video" else ""
    return f"""<nav class="mode-tabs" aria-label="Capture mode">
          <a class="mode-tab{photo_cur}" href="/" title="Photo capture mode">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>
            Photos
          </a>
          <a class="mode-tab{video_cur}" href="/video" title="Video capture mode">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>
            Video
          </a>
        </nav>"""


def video_settings_fields(ss):
    ss = ss or {}
    res = str(ss.get("res", "1080"))
    res = {"1080": "1920x1080", "720": "1280x720"}.get(res, res)
    fps = str(ss.get("fps", "30"))
    qual = str(ss.get("quality", "medium"))
    dur = str(ss.get("duration", 0))
    prefill = "1" if ss else "0"
    res_options = [
        ("1920x1080", "1080p · 1920 × 1080"),
        ("1280x720", "720p · 1280 × 720"),
    ]
    if res not in {value for value, _ in res_options}:
        size_label = res.replace("x", " × ")
        res_options.append((res, size_label))
    rendered_res_options = "".join(
        f'<option value="{value}" {"selected" if res == value else ""}>{label}</option>'
        for value, label in res_options
    )
    extra_fps = "" if fps in ("15", "30") else f'<option value="{fps}" selected>{fps} fps</option>'
    highres = bool(ss.get("highres", False))
    return f"""<div class="settings-grid" id="videoSettings" data-prefilled="{prefill}">
        <div class="set-field"><label for="vidRes">Resolution</label>
          <select id="vidRes">{rendered_res_options}</select></div>
        <div class="set-field"><label for="vidFps">Frame rate</label>
          <select id="vidFps">
            <option value="30" {'selected' if fps == '30' else ''}>30 fps</option>
            <option value="15" {'selected' if fps == '15' else ''}>15 fps</option>
            {extra_fps}
          </select></div>
        <div class="set-field"><label for="vidQual">Quality</label>
          <select id="vidQual">
            <option value="low" {'selected' if qual == 'low' else ''}>Low · 4 Mbps</option>
            <option value="medium" {'selected' if qual == 'medium' else ''}>Medium · 10 Mbps</option>
            <option value="high" {'selected' if qual == 'high' else ''}>High · 20 Mbps</option>
          </select></div>
        <div class="set-field"><label for="vidDur">Max duration (s)</label>
          <input id="vidDur" type="number" min="0" step="1" value="{dur}">
        </div>
      </div>
      <label class="highres-toggle"><input id="vidHighRes" type="checkbox"{' checked' if highres else ''}> Enable higher-resolution video modes</label>
      <p class="highres-warning">Warning: these opt-in modes can overload the Pi 5’s CPU, memory, and cooling. Software encoding may drop frames, fail, or throttle; files are much larger. Module 3’s 4608 × 2592 mode is limited to about 14 fps.</p>"""


def video_cards_html(sections):
    if not sections:
        return """
        <div class="empty-state">
            <div class="empty-icon">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="23 7 16 12 23 17 23 7"></polygon>
                    <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
                </svg>
            </div>
            <h2>No video recordings yet</h2>
            <p>Press <strong>Start Recording</strong> above to capture video from every camera at once, then <strong>Stop Recording</strong> when you are done. Recordings appear here after upload.</p>
        </div>
        """
    sections_html = []
    for title, cardlist, is_session, created in sections:
        total_videos = sum(len(entries) for _, entries in cardlist)
        total_batches = len(cardlist)
        safe_title = urllib.parse.quote(title, safe="") if title else "ungrouped"
        session_key = f"sess_{safe_title}"
        session_batch_attr = (
            f' data-session-name="{urllib.parse.quote(title, safe="")}"'
            if is_session and title else ""
        )
        batch_cards = []
        for name, entries in cardlist:
            formatted_time, hsh = format_batch_display(name)
            hsh_badge = f'<span class="hash-tag">{hsh}</span>' if hsh else ""
            del_batch = (
                f'<span class="del-wrap">'
                f'<button class="btn-zip btn-del" onclick="showDelConfirm(this)">Delete</button>'
                f'<span class="del-confirm" hidden>'
                f'<button class="btn-zip btn-del-files" onclick="deleteBatch(this)">Delete batch</button>'
                f'<button class="btn-zip" onclick="hideDelConfirm(this)">&#10005;</button>'
                f'</span>'
                f'</span>'
            )
            figures = "".join(
                f'<figure class="thumb-card" data-img="{img}">'
                f'<div class="thumb-media vid-media">'
                f'<span class="dev-badge">{parse_device_badge(img)}</span>'
                f'<a href="{name}/{img}" download title="{img}" aria-label="Download {img}">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>'
                f'</a>'
                f'</div>'
                f'<figcaption><span class="fname" title="{img}">{img}</span>'
                f'<span class="count-pill">{format_size(size)}</span>'
                f'<a class="btn-save" href="{name}/{img}" download title="Download file">'
                f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> <span class="save-label">Save</span></a>'
                f'</figcaption></figure>'
                for img, size in entries
            )
            batch_cards.append(
                f'<div class="batch-card" data-batch-name="{name}"{session_batch_attr}>'
                f'<div class="batch-header">'
                f'<div class="batch-title-group">'
                f'<span class="batch-time">{formatted_time}</span>{hsh_badge}'
                f'<span class="count-pill">{len(entries)} video{"s" if len(entries) != 1 else ""}</span>'
                f'</div>'
                f'<a class="btn-zip" href="/zip/{name}">'
                f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Batch ZIP</a>'
                f'{del_batch}'
                f'</div>'
                f'<div class="grid">{figures}</div>'
                f'</div>'
            )
        dl_session = ""
        del_btn = ""
        cont_btn = ""
        if is_session and title:
            cont_btn = (
                f'<button class="btn-zip btn-session-zip" onclick="event.stopPropagation();continueSession(this)">'
                f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> Continue</button>'
            )
            dl_session = (
                f'<a class="btn-zip btn-session-zip" href="/zip/session/{urllib.parse.quote(title)}" onclick="event.stopPropagation()">'
                f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Session ZIP</a>'
            )
            del_btn = (
                f'<span class="del-wrap" onclick="event.stopPropagation()">'
                f'<button class="btn-zip btn-del" onclick="showDelConfirm(this)">Delete</button>'
                f'<span class="del-confirm" hidden>'
                f'<button class="btn-zip" onclick="deleteSession(this)">Keep files</button>'
                f'<button class="btn-zip btn-del-files" onclick="deleteSession(this, true)">Delete files</button>'
                f'<button class="btn-zip" onclick="hideDelConfirm(this)">&#10005;</button>'
                f'</span>'
                f'</span>'
            )
        sections_html.append(
            f'<details class="session-accordion" data-session-key="{session_key}" data-session-name="{safe_title}" data-session-created="{created}" data-is-session="{1 if is_session and title else 0}">'
            f'<summary class="session-header">'
            f'<div class="session-header-left">'
            f'<label class="sel-check"><input type="checkbox" class="sel-sess" data-name="{safe_title}" onclick="event.stopPropagation()"></label>'
            f'<span class="chevron"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg></span>'
            f'<span class="session-title">{title if title else "Ungrouped Recordings"}</span>'
            f'<div class="session-meta-pills">'
            f'<span class="meta-pill">{total_batches} batch{"es" if total_batches != 1 else ""}</span>'
            f'<span class="meta-pill">{total_videos} video{"s" if total_videos != 1 else ""}</span>'
            f'</div>'
            f'</div>'
            f'<div class="session-header-right">{cont_btn}{dl_session}{del_btn}</div>'
            f'</summary>'
            f'<div class="session-body">'
            f'{"".join(batch_cards)}'
            f'</div>'
            f'</details>'
        )
    return "".join(sections_html)


def header_html(mode):
    capture_btn = ""
    if mode == "photo":
        capture_btn = """
        <button class="btn btn-primary" id="btnCapture" onclick="triggerCapture(this)">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="3"></circle></svg>
          <span>Capture All</span>
        </button>"""
    return f"""<header class="app-header">
  <div class="header-container">
    <div class="header-top">
      <div class="brand-section">
        <h1 class="app-title">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
            <circle cx="8.5" cy="8.5" r="1.5"></circle>
            <polyline points="21 15 16 10 5 21"></polyline>
          </svg>
          Multi-Cam Gallery
        </h1>
        {mode_tabs(mode)}
      </div>
      <div class="status-pills-bar" id="statusPills">
        <span class="live-pill" id="primaryPill"><span class="live-dot"></span> Checking Primary...</span>
        <span class="live-pill" id="sessionPill" style="display:none;"><span class="live-dot"></span> <span id="sessionPillText">No Session</span></span>
        <span id="secondariesPills"></span>
      </div>
    </div>

    <div class="controls-bar">
      <div class="actions-group">
        {capture_btn}
        <button class="btn" id="btnSync" onclick="triggerSync(this)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
          <span>Sync Now</span>
        </button>
        <button class="btn btn-danger" id="btnPurge" onclick="openPurgeModal()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
          <span>Purge Secondaries</span>
        </button>
      </div>

      <div class="session-form-group">
        <input class="input-text" id="sessname" placeholder="Session name" autocomplete="off">
        <button class="btn" onclick="startSession()">Start</button>
        <button class="btn" onclick="stopSession()">Stop</button>
        <button class="btn btn-icon-only" onclick="refreshGallery(true)" title="Refresh Gallery">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
        </button>
      </div>
    </div>
  </div>
</header>"""


def subbar_html(mode, ungrouped_count=0):
    del_ung = ""
    if ungrouped_count > 0:
        label = "batch" if ungrouped_count == 1 else "batches"
        del_ung = f"""<span class="del-wrap">
        <button class="btn-zip btn-del" data-kind="{mode}" onclick="showDelConfirm(this)">Delete Ungrouped ({ungrouped_count})</button>
        <span class="del-confirm" hidden>
          <button class="btn-zip btn-del-files" onclick="deleteUngrouped(this)">Delete {ungrouped_count} {label}</button>
          <button class="btn-zip" onclick="hideDelConfirm(this)">&#10005;</button>
        </span>
      </span>"""
    still_res = ""
    if mode == "photo":
        still_res = """<label class="sort-group">Photo resolution
        <select class="select-sort" id="stillRes">
          <option value="full">Full sensor</option>
          <option value="1920x1080">1920 × 1080</option>
          <option value="1280x720">1280 × 720</option>
          <option value="960x540">960 × 540</option>
          <option value="640x480">640 × 480</option>
        </select>
      </label>
      <span style="color:var(--border-strong)">·</span>"""
    return f"""<div class="gallery-subbar">
    <div class="view-controls">
      <label class="sort-group">Sessions
        <select class="select-sort" id="selSortSessions" onchange="setSort('sessions', this.value)">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="name">Name A–Z</option>
        </select>
      </label>
      <label class="sort-group">Captures
        <select class="select-sort" id="selSortBatches" onchange="setSort('batches', this.value)">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
      </label>
      <label class="sort-group">Files
        <select class="select-sort" id="selSortImages" onchange="setSort('images', this.value)">
          <option value="file">Filename</option>
          <option value="device">Device cam ID</option>
        </select>
      </label>
      {still_res}
      <button class="btn-text" onclick="toggleAllAccordions(true)">Expand All</button>
      <span style="color:var(--border-strong)">·</span>
      <button class="btn-text" onclick="toggleAllAccordions(false)">Collapse All</button>
      <span style="color:var(--border-strong)">·</span>
      <button class="btn-text" id="btnSelectMode" onclick="toggleSelectMode()">Select</button>
      <button class="btn-text" id="btnSelectAllBtn" onclick="toggleSelectAll()" hidden>Select All</button>
      <button class="btn btn-primary btn-batch-dl" id="btnBatchDl" onclick="downloadSelected()" hidden>Download Selected (0)</button>
      {del_ung}
      <span class="live-pill warn" id="capsPill" hidden title="No camera capability data">Defaults — no camera data</span>
    </div>
  </div>"""


def main_html(mode, cards_content, session_settings=None, ungrouped_count=0):
    if mode == "photo":
        return f"""<main class="main-content">
  <div class="gallery-subbar">
    {subbar_html("photo", ungrouped_count)}
  </div>

  <div id="galleryContainer">
    {cards_content}
  </div>
</main>"""
    settings = video_settings_fields(session_settings)
    return f"""<main class="main-content">
  <div class="settings-card">
    <div class="record-row">
      <button class="btn btn-primary btn-record" id="btnRecord" onclick="toggleRecord(this)">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"></circle></svg>
        <span id="btnRecordText">Start Recording</span>
      </button>
      <span class="live-pill rec-pill" id="recPill" hidden><span class="live-dot"></span> <span id="recPillText">REC</span></span>
    </div>
    {settings}
    <p class="settings-note">Settings apply to every Pi in the next recording and are saved with the active video session. Each camera records its own file; no live preview is shown.</p>
    <label class="upload-refresh-option"><input type="checkbox" id="autoRefreshUploads" checked> Auto-refresh while Pi videos upload</label>
    <span class="upload-refresh-status" id="uploadRefreshStatus" aria-live="polite"></span>
  </div>
  <div class="gallery-subbar">
    {subbar_html("video", ungrouped_count)}
  </div>

  <div id="galleryContainer">
    {cards_content}
  </div>
</main>"""


def mobile_bar_html(mode):
    primary_btn = ""
    if mode == "photo":
        primary_btn = """<button class="btn btn-primary btn-capture-mobile" id="btnCaptureM" onclick="triggerCapture(this)">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="3"></circle></svg>
    <span>Capture All</span>
  </button>"""
    else:
        primary_btn = """<button class="btn btn-primary btn-capture-mobile" id="btnRecordM" onclick="toggleRecord(this)">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"></circle></svg>
    <span id="btnRecordTextM">Start Recording</span>
  </button>"""
    return f"""<div class="mobile-bar" id="mobileBar">
  {primary_btn}
  <div class="overflow-wrap">
    <button class="btn btn-icon-only" onclick="toggleOverflow(event)">&nbsp;&#8942;&nbsp;</button>
    <div class="overflow-menu" id="overflowMenu" hidden>
      <button class="btn" onclick="triggerSync(this); toggleOverflow(event)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
        Sync Now
      </button>
      <button class="btn btn-danger" onclick="openPurgeModal()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        Purge Secondaries
      </button>
    </div>
  </div>
</div>"""


PAGE_JS = """let lightbox = null;
const PAGE_MODE = '__PAGE_MODE__';
let videoRecordingActive = false;
const VIDEO_AUTO_REFRESH_KEY = 'mcAutoRefreshVideoUploads';
const VIDEO_PENDING_UPLOAD_KEY = 'mcPendingVideoUpload';
const VIDEO_UPLOAD_REFRESH_MS = 5000;
const VIDEO_UPLOAD_REFRESH_MAX_MS = 15 * 60 * 1000;
let autoRefreshVideoUploads = true;
let videoUploadRefreshTimer = null;
let videoUploadRefreshInFlight = false;
let pendingVideoUploadBatch = null;
let pendingVideoUploadStarted = 0;
let observedVideoRecording = false;
let observedVideoBatch = null;
try {
  const savedAutoRefresh = localStorage.getItem(VIDEO_AUTO_REFRESH_KEY);
  if (savedAutoRefresh !== null) autoRefreshVideoUploads = savedAutoRefresh === 'true';
} catch (e) {}

function modeSync() {
  fetch('/mode?mode=' + PAGE_MODE, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (!d.ok) showToast(d.error || 'Capture mode change was refused.', 'error');
    })
    .catch(e => showToast('Capture mode error: ' + e, 'error'));
}

function updateRecordUI() {}

let uiSizes = null;
let uiHighResSizes = [];
let uiFull = null;
let capsSig = "";

function intersectSizes(a, b) {
  const bySize = new Map(b.map(e => [e.size[0] + 'x' + e.size[1], e]));
  return a.flatMap(e => {
    const other = bySize.get(e.size[0] + 'x' + e.size[1]);
    return other ? [{size: e.size, fps: Math.min(e.fps, other.fps)}] : [];
  });
}

function applyCaps(p, secs) {
  const caps = (p || {}).caps;
  if (!caps || !Array.isArray(caps.sizes) || !caps.sizes.length) return;
  let sizes = caps.sizes;
  let highResSizes = Array.isArray(caps.high_res_sizes) ? caps.high_res_sizes : [];
  Object.values(secs || {}).forEach(v => {
    const s = v && v.up && v.caps && v.caps.sizes;
    if (Array.isArray(s) && s.length) sizes = intersectSizes(sizes, s);
    if (v && v.up) {
      const high = v.caps && v.caps.high_res_sizes;
      highResSizes = Array.isArray(high) ? intersectSizes(highResSizes, high) : [];
    }
  });
  if (!sizes.length) return;
  uiSizes = sizes;
  uiHighResSizes = highResSizes;
  uiFull = caps.full || null;
  const sig = JSON.stringify(sizes) + '|' + JSON.stringify(highResSizes) + '|' + JSON.stringify(uiFull);
  if (sig === capsSig) return;
  capsSig = sig;
  rebuildVideoSizes();
  rebuildStillSizes();
}

function rebuildVideoSizes() {
  const resSel = document.getElementById('vidRes');
  if (!resSel) return;
  const highResToggle = document.getElementById('vidHighRes');
  const sizes = (uiSizes || []).concat(highResToggle && highResToggle.checked ? uiHighResSizes : []);
  if (!sizes.length) return;
  const legacy = {'1080': '1920x1080', '720': '1280x720'};
  let cur = resSel.value;
  if (legacy[cur]) cur = legacy[cur];
  resSel.innerHTML = sizes.map(e =>
    '<option value="' + e.size[0] + 'x' + e.size[1] + '">' + e.size[0] + ' × ' + e.size[1] + '</option>'
  ).join('');
  const has = Array.from(resSel.options).some(o => o.value === cur);
  resSel.value = has ? cur : resSel.options[0].value;
  rebuildVideoFps();
}

function rebuildVideoFps() {
  const resSel = document.getElementById('vidRes');
  const fpsSel = document.getElementById('vidFps');
  if (!fpsSel || !uiSizes) return;
  const parts = (resSel.value || '').split('x');
  const e = uiSizes.find(s => String(s.size[0]) === parts[0] && String(s.size[1]) === parts[1]);
  const highResToggle = document.getElementById('vidHighRes');
  const highResEntry = highResToggle && highResToggle.checked
    ? uiHighResSizes.find(s => String(s.size[0]) === parts[0] && String(s.size[1]) === parts[1])
    : null;
  const selected = highResEntry || e;
  const cap = selected ? selected.fps : 0;
  const availableSteps = highResEntry ? [10, 14, 15, 30, 60, 120] : [15, 30, 60, 120];
  const steps = availableSteps.filter(f => f <= cap);
  if (!steps.length) return;
  const cur = parseInt(fpsSel.value, 10) || 30;
  fpsSel.innerHTML = steps.map(f => '<option value="' + f + '">' + f + ' fps</option>').join('');
  fpsSel.value = steps.indexOf(cur) >= 0 ? String(cur) : String(steps[steps.length - 1]);
}

function rebuildStillSizes() {
  const sel = document.getElementById('stillRes');
  if (!sel) return;
  const cur = sel.value;
  let html = '<option value="full">Full sensor' + (uiFull ? ' (' + uiFull[0] + ' × ' + uiFull[1] + ')' : '') + '</option>';
  html += uiSizes.map(e =>
    '<option value="' + e.size[0] + 'x' + e.size[1] + '">' + e.size[0] + ' × ' + e.size[1] + '</option>'
  ).join('');
  sel.innerHTML = html;
  const has = Array.from(sel.options).some(o => o.value === cur);
  sel.value = has ? cur : 'full';
}

function deleteUngrouped(btn) {
  const kind = btn.closest('.del-wrap').querySelector('.btn-del').getAttribute('data-kind') || 'image';
  fetch('/captures/delete-all?kind=' + kind, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        showToast('Delete failed: ' + (d.error || ''), 'error');
        return;
      }
      showToast('Deleted ' + d.removed + ' ungrouped batch(es) (' + Math.round((d.freed || 0) / 1048576) + ' MB freed)', 'success');
      setTimeout(() => refreshGallery(false), 400);
    })
    .catch(e => showToast('Delete error: ' + e, 'error'));
}

function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.2s ease';
    setTimeout(() => toast.remove(), 250);
  }, duration);
}

function initLightbox() {
  if (typeof PhotoSwipeLightbox === 'undefined') return;
  if (lightbox) {
    lightbox.destroy();
  }
  lightbox = new PhotoSwipeLightbox({
    gallery: '.grid',
    children: 'a.pswp-link',
    pswpModule: () => import('/static/photoswipe/photoswipe.esm.js')
  });
  lightbox.init();
}

function getOpenAccordionKeys() {
  const openKeys = new Set();
  document.querySelectorAll('.session-accordion[open]').forEach(el => {
    const key = el.getAttribute('data-session-key');
    if (key) openKeys.add(key);
  });
  return openKeys;
}

function restoreAccordionStates(openKeys) {
  document.querySelectorAll('.session-accordion').forEach(el => {
    const key = el.getAttribute('data-session-key');
    if (key && openKeys.has(key)) {
      el.setAttribute('open', '');
      el.classList.add('is-open');
    } else if (key && openKeys.size > 0) {
      el.removeAttribute('open');
      el.classList.remove('is-open');
    }
  });
}

function toggleAllAccordions(open) {
  document.querySelectorAll('.session-accordion').forEach(el => {
    if (open) {
      el.setAttribute('open', '');
      el.classList.add('is-open');
    } else {
      el.removeAttribute('open');
      el.classList.remove('is-open');
    }
  });
}

const SORT_STORE = 'mcGallerySort';
let sortPrefs = { sessions: 'newest', batches: 'newest', images: 'file' };
try {
  const saved = JSON.parse(localStorage.getItem(SORT_STORE) || '{}');
  if (saved && typeof saved === 'object') Object.assign(sortPrefs, saved);
} catch (e) {}

function naturalKey(s) {
  return s.toLowerCase().split(/(\\d+)/).map(t => (/^\\d+$/.test(t) ? t.padStart(12, '0') : t));
}

function cmpNatural(a, b) {
  const ka = naturalKey(a), kb = naturalKey(b);
  const n = Math.min(ka.length, kb.length);
  for (let i = 0; i < n; i++) {
    if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
  }
  return ka.length - kb.length;
}

function cmpFile(a, b) {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

function applySorts() {
  const cont = document.getElementById('galleryContainer');
  const sm = sortPrefs.sessions;
  const accs = Array.from(cont.querySelectorAll(':scope > .session-accordion'));
  accs.sort((a, b) => {
    if (sm === 'name') {
      const na = decodeURIComponent(a.getAttribute('data-session-name') || '');
      const nb = decodeURIComponent(b.getAttribute('data-session-name') || '');
      return na.localeCompare(nb);
    }
    const ca = parseFloat(a.getAttribute('data-session-created') || '0');
    const cb = parseFloat(b.getAttribute('data-session-created') || '0');
    if (ca === cb) return 0;
    return sm === 'newest' ? cb - ca : ca - cb;
  });
  accs.forEach(el => cont.appendChild(el));

  const bm = sortPrefs.batches;
  if (bm !== 'file') {
    document.querySelectorAll('.session-body').forEach(body => {
      const cards = Array.from(body.querySelectorAll(':scope > .batch-card'));
      cards.sort((a, b) => {
        const na = a.getAttribute('data-batch-name') || '';
        const nb = b.getAttribute('data-batch-name') || '';
        return bm === 'newest' ? cmpNatural(nb, na) : cmpNatural(na, nb);
      });
      cards.forEach(c => body.appendChild(c));
    });
  }

  const im = sortPrefs.images;
  document.querySelectorAll('.grid').forEach(grid => {
    const figs = Array.from(grid.querySelectorAll(':scope > .thumb-card'));
    figs.sort((a, b) => {
      const na = a.getAttribute('data-img') || '';
      const nb = b.getAttribute('data-img') || '';
      return im === 'device' ? cmpNatural(na, nb) : cmpFile(na, nb);
    });
    figs.forEach(f => grid.appendChild(f));
  });
}

function setSort(kind, value) {
  sortPrefs[kind] = value;
  try {
    localStorage.setItem(SORT_STORE, JSON.stringify(sortPrefs));
  } catch (e) {}
  applySorts();
  initLightbox();
}

function initSortControls() {
  document.getElementById('selSortSessions').value = sortPrefs.sessions;
  document.getElementById('selSortBatches').value = sortPrefs.batches;
  document.getElementById('selSortImages').value = sortPrefs.images;
}

function toggleSelectMode() {
  document.body.classList.toggle('select-mode');
  const on = document.body.classList.contains('select-mode');
  document.getElementById('btnSelectAllBtn').hidden = !on;
  if (!on) {
    document.querySelectorAll('.sel-sess').forEach(cb => { cb.checked = false; });
  }
  updateBatchDl();
}

function toggleSelectAll() {
  const boxes = document.querySelectorAll('.sel-sess');
  const all = Array.from(boxes).every(cb => cb.checked);
  boxes.forEach(cb => { cb.checked = !all; });
  updateBatchDl();
}

function updateBatchDl() {
  const n = document.querySelectorAll('.sel-sess:checked').length;
  const btn = document.getElementById('btnBatchDl');
  btn.hidden = n === 0;
  btn.textContent = 'Download Selected (' + n + ')';
  btn.disabled = n === 0;
}

function downloadSelected() {
  const sessions = [];
  const batches = [];
  document.querySelectorAll('.sel-sess:checked').forEach(cb => {
    const name = decodeURIComponent(cb.getAttribute('data-name'));
    const enc = encodeURIComponent(name);
    (cb.getAttribute('data-is-session') === '1' ? sessions : batches).push(enc);
  });
  if (!sessions.length && !batches.length) return;
  const url = '/zip/multi?sessions=' + sessions.join(',') + '&batches=' + batches.join(',');
  window.location.assign(url);
}

document.addEventListener('change', e => {
  if (e.target && e.target.classList && e.target.classList.contains('sel-sess')) {
    updateBatchDl();
  }
});

function toggleOverflow(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById('overflowMenu');
  menu.hidden = !menu.hidden;
}

document.addEventListener('click', e => {
  const menu = document.getElementById('overflowMenu');
  if (menu && !menu.hidden && !e.target.closest('.overflow-wrap')) {
    menu.hidden = true;
  }
});

function refreshGallery(showToastMsg = false, url) {
  url = url || (PAGE_MODE === 'video' ? '/video?partial=1' : '/?partial=1');
  const openKeys = getOpenAccordionKeys();
  return fetch(url)
    .then(r => r.text())
    .then(html => {
      document.getElementById('galleryContainer').innerHTML = html;
      restoreAccordionStates(openKeys);
      applySorts();
      initLightbox();
      initSortControls();
      if (typeof updateBatchDl === 'function') updateBatchDl();
      if (showToastMsg) showToast('Gallery updated', 'info', 2000);
    })
    .catch(err => {
      if (showToastMsg) showToast('Failed to refresh gallery: ' + err, 'error');
    });
}

function triggerCapture(btn) {
  btn = btn || document.getElementById('btnCapture');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> <span>Capturing...</span>';
  
  fetch('/capture', {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        showToast('Capture complete!', 'success');
        setTimeout(() => refreshGallery(false), __REFRESH_MS__);
      } else {
        showToast('Capture failed: ' + (d.error || 'Unknown error'), 'error');
      }
    })
    .catch(e => showToast('Capture error: ' + e, 'error'))
    .finally(() => {
      btn.disabled = false;
      btn.innerHTML = original;
    });
}

function triggerSync(btn) {
  btn = btn || document.getElementById('btnSync');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="border-top-color:var(--text-main)"></span> <span>Syncing...</span>';
  
  fetch('/sync', {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        showToast('Sync failed: ' + (d.error || ''), 'error');
        return;
      }
      const parts = Object.entries(d.secondaries || {}).map(([k, v]) =>
        v.error ? k + ': ' + v.error : k + ': +' + v.pushed + (v.failed ? ' (' + v.failed + ' failed)' : '')
      );
      showToast(parts.length ? 'Sync: ' + parts.join(' | ') : 'No secondaries registered.', 'success');
      setTimeout(() => refreshGallery(false), 600);
    })
    .catch(e => showToast('Sync error: ' + e, 'error'))
    .finally(() => {
      btn.disabled = false;
      btn.innerHTML = original;
    });
}

function openPurgeModal() {
  document.getElementById('purgeModal').classList.add('active');
}

function closePurgeModal() {
  document.getElementById('purgeModal').classList.remove('active');
}

function confirmPurge() {
  const btn = document.getElementById('btnConfirmPurge');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Deleting...';

  fetch('/purge-secondaries', {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      closePurgeModal();
      if (!d.ok) {
        showToast('Purge failed: ' + (d.error || ''), 'error');
      } else {
        const parts = Object.entries(d.secondaries || {}).map(([k, v]) =>
          v.error ? k + ': ' + v.error : k + ': ' + v.deleted + ' deleted'
        );
        showToast(parts.length ? 'Purged: ' + parts.join(' | ') : 'No secondaries registered.', 'success');
      }
    })
    .catch(e => {
      closePurgeModal();
      showToast('Purge error: ' + e, 'error');
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = 'Delete Captures';
    });
}

function startSession() {
  const input = document.getElementById('sessname');
  const name = input.value.trim();
  if (!name) {
    showToast('Please enter a session name.', 'info');
    return;
  }
  fetch('/session/start?name=' + encodeURIComponent(name) + '&kind=' + PAGE_MODE, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        showToast('Session started: ' + d.session, 'success');
        input.value = '';
        pollStatus();
        setTimeout(() => refreshGallery(false), 500);
      } else {
        showToast('Start session failed: ' + (d.error || ''), 'error');
      }
    })
    .catch(e => showToast('Session error: ' + e, 'error'));
}

function continueSession(btn) {
  const acc = btn.closest('.session-accordion');
  const name = decodeURIComponent(acc.getAttribute('data-session-name'));
  fetch('/session/start?name=' + encodeURIComponent(name) + '&kind=' + PAGE_MODE, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        showToast('Continuing session: ' + d.session, 'success');
        pollStatus();
      } else {
        showToast('Continue failed: ' + (d.error || ''), 'error');
      }
    })
    .catch(e => showToast('Session error: ' + e, 'error'));
}

function stopSession() {
  fetch('/session/stop', {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        showToast('Session stopped', 'info');
        pollStatus();
        setTimeout(() => refreshGallery(false), 500);
      } else {
        showToast('Stop session failed: ' + (d.error || ''), 'error');
      }
    })
    .catch(e => showToast('Session error: ' + e, 'error'));
}

function showDelConfirm(btn) {
  const wrap = btn.parentElement;
  btn.hidden = true;
  wrap.querySelector('.del-confirm').hidden = false;
}

function hideDelConfirm(btn) {
  const wrap = btn.closest('.del-wrap');
  wrap.querySelector('.btn-del').hidden = false;
  wrap.querySelector('.del-confirm').hidden = true;
}

function deleteSession(btn, files) {
  const acc = btn.closest('.session-accordion');
  const name = decodeURIComponent(acc.getAttribute('data-session-name'));
  fetch('/session/delete?name=' + encodeURIComponent(name) + (files ? '&files=1' : ''), {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        showToast('Delete failed: ' + (d.error || ''), 'error');
        return;
      }
      showToast(files ? 'Session deleted, files removed.' : 'Session removed, captures kept.', 'success');
      pollStatus();
      setTimeout(() => refreshGallery(false), 400);
    })
    .catch(e => showToast('Delete error: ' + e, 'error'));
}

function deleteBatch(btn) {
  const card = btn.closest('.batch-card');
  const name = card.getAttribute('data-batch-name');
  const sessionName = card.getAttribute('data-session-name');
  const url = sessionName
    ? '/session/batch/delete?session=' + sessionName + '&batch=' + encodeURIComponent(name)
    : '/captures/delete?batch=' + encodeURIComponent(name);
  fetch(url, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        showToast('Delete failed: ' + (d.error || ''), 'error');
        return;
      }
      showToast('Batch deleted: ' + name, 'success');
      setTimeout(() => refreshGallery(false), 400);
    })
    .catch(e => showToast('Delete error: ' + e, 'error'));
}

let cameraAlertKeys = new Set();

function cameraErrorsOf(device) {
  return Array.isArray(device && device.camera_errors) ? device.camera_errors : [];
}

function cameraErrorSummary(errors) {
  return errors.map(e => 'Cam ' + e.camera + ' failed').join(', ');
}

function renderDevicePill(el, label, errors, offline = false, fallbackTitle = '') {
  const dot = document.createElement('span');
  dot.className = 'live-dot';
  el.className = 'live-pill ' + (offline ? 'offline' : (errors.length ? 'warn' : 'online'));
  const summary = cameraErrorSummary(errors);
  el.replaceChildren(dot, document.createTextNode(label + (summary ? ' · ' + summary : '')));
  el.title = errors.length
    ? errors.map(e => 'Camera ' + e.camera + ': ' + e.error).join(' | ')
    : fallbackTitle;
}

function notifyCameraErrors(devices) {
  const current = new Set();
  devices.forEach(([piId, errors]) => {
    errors.forEach(e => {
      const key = piId + ':' + e.camera + ':' + e.error;
      current.add(key);
      if (!cameraAlertKeys.has(key)) {
        showToast(piId + ': Camera ' + e.camera + ' failed — ' + e.error, 'error', 9000);
      }
    });
  });
  cameraAlertKeys = current;
}

function setVideoUploadStatus(message) {
  const status = document.getElementById('uploadRefreshStatus');
  if (status) status.textContent = message;
}

function expectedVideoFiles(status) {
  const devices = [];
  if (status && status.primary) devices.push(status.primary);
  Object.values((status && status.secondaries) || {}).forEach(device => {
    if (device.up) devices.push(device);
  });
  const expected = new Set();
  devices.forEach(device => {
    if (!device.pi_id) return;
    const indices = Array.isArray(device.available_cameras)
      ? device.available_cameras
      : Array.from({length: Number(device.cameras) || 0}, (_, i) => i);
    const failed = new Set(cameraErrorsOf(device).map(error => String(error.camera)));
    indices.forEach(index => {
      if (!failed.has(String(index))) expected.add(device.pi_id + '_cam' + index);
    });
  });
  return expected;
}

function videoFilesInBatch(batch) {
  const card = Array.from(document.querySelectorAll('.batch-card'))
    .find(el => el.getAttribute('data-batch-name') === batch);
  if (!card) return new Set();
  return new Set(Array.from(card.querySelectorAll('.thumb-card')).map(el => {
    const name = el.getAttribute('data-img') || '';
    const dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(0, dot) : name;
  }));
}

function clearVideoUploadRefresh(batch, message, toastType) {
  if (videoUploadRefreshTimer) clearTimeout(videoUploadRefreshTimer);
  videoUploadRefreshTimer = null;
  if (batch === pendingVideoUploadBatch) {
    pendingVideoUploadBatch = null;
    pendingVideoUploadStarted = 0;
    try { localStorage.removeItem(VIDEO_PENDING_UPLOAD_KEY); } catch (e) {}
  }
  setVideoUploadStatus(message);
  if (message && toastType) showToast(message, toastType, 6000);
}

function scheduleVideoUploadRefresh(batch, started) {
  if (!autoRefreshVideoUploads || pendingVideoUploadBatch !== batch) return;
  if (videoUploadRefreshTimer) clearTimeout(videoUploadRefreshTimer);
  videoUploadRefreshTimer = setTimeout(() => pollVideoUploadRefresh(batch, started), VIDEO_UPLOAD_REFRESH_MS);
}

function pollVideoUploadRefresh(batch, started) {
  if (!autoRefreshVideoUploads || pendingVideoUploadBatch !== batch) return;
  if (videoUploadRefreshInFlight) {
    scheduleVideoUploadRefresh(batch, started);
    return;
  }
  videoUploadRefreshTimer = null;
  videoUploadRefreshInFlight = true;
  refreshGallery(false, '/video?partial=1')
    .then(() => fetch('/server-status'))
    .then(r => r.json())
    .then(status => {
      if (pendingVideoUploadBatch !== batch) return;
      if (!autoRefreshVideoUploads) {
        setVideoUploadStatus('Auto-refresh paused.');
        return;
      }
      if (!status.ok) throw new Error(status.error || 'status unavailable');
      const expected = expectedVideoFiles(status);
      const present = videoFilesInBatch(batch);
      const missing = Array.from(expected).filter(name => !present.has(name));
      if (expected.size && missing.length === 0) {
        clearVideoUploadRefresh(batch, 'All available camera videos arrived.', 'success');
        return;
      }
      if (Date.now() - started >= VIDEO_UPLOAD_REFRESH_MAX_MS) {
        clearVideoUploadRefresh(batch, 'Auto-refresh timed out; refresh later to check remaining videos.', 'info');
        return;
      }
      setVideoUploadStatus(missing.length
        ? 'Waiting for ' + missing.length + ' camera video(s) from other Pis…'
        : 'Checking camera upload status…');
      scheduleVideoUploadRefresh(batch, started);
    })
    .catch(() => {
      if (pendingVideoUploadBatch === batch && autoRefreshVideoUploads) {
        if (Date.now() - started >= VIDEO_UPLOAD_REFRESH_MAX_MS) {
          clearVideoUploadRefresh(batch, 'Auto-refresh timed out; refresh later to check remaining videos.', 'info');
        } else {
          setVideoUploadStatus('Checking for camera uploads…');
          scheduleVideoUploadRefresh(batch, started);
        }
      }
    })
    .finally(() => { videoUploadRefreshInFlight = false; });
}

function beginVideoUploadRefresh(batch) {
  if (!batch || PAGE_MODE !== 'video') return;
  if (pendingVideoUploadBatch === batch && pendingVideoUploadStarted) {
    if (autoRefreshVideoUploads && !videoUploadRefreshTimer && !videoUploadRefreshInFlight) {
      pollVideoUploadRefresh(batch, pendingVideoUploadStarted);
    }
    return;
  }
  if (videoUploadRefreshTimer) clearTimeout(videoUploadRefreshTimer);
  pendingVideoUploadBatch = batch;
  pendingVideoUploadStarted = Date.now();
  try {
    localStorage.setItem(VIDEO_PENDING_UPLOAD_KEY, JSON.stringify({
      batch: batch, started: pendingVideoUploadStarted
    }));
  } catch (e) {}
  setVideoUploadStatus(autoRefreshVideoUploads
    ? 'Waiting for secondary video uploads…'
    : 'Auto-refresh is off.');
  if (autoRefreshVideoUploads) pollVideoUploadRefresh(batch, pendingVideoUploadStarted);
  else refreshGallery(false, '/video?partial=1');
}

function initVideoUploadRefreshToggle() {
  const toggle = document.getElementById('autoRefreshUploads');
  if (!toggle) return;
  toggle.checked = autoRefreshVideoUploads;
  toggle.addEventListener('change', () => {
    autoRefreshVideoUploads = toggle.checked;
    try { localStorage.setItem(VIDEO_AUTO_REFRESH_KEY, String(autoRefreshVideoUploads)); } catch (e) {}
    if (!autoRefreshVideoUploads) {
      if (videoUploadRefreshTimer) clearTimeout(videoUploadRefreshTimer);
      videoUploadRefreshTimer = null;
      setVideoUploadStatus(pendingVideoUploadBatch ? 'Auto-refresh paused.' : 'Auto-refresh is off.');
    } else if (pendingVideoUploadBatch) {
      setVideoUploadStatus('Resuming upload refresh…');
      pollVideoUploadRefresh(pendingVideoUploadBatch, pendingVideoUploadStarted);
    } else {
      setVideoUploadStatus('Auto-refresh is on.');
    }
  });
  try {
    const pending = JSON.parse(localStorage.getItem(VIDEO_PENDING_UPLOAD_KEY) || 'null');
    if (pending && pending.batch && Date.now() - pending.started < VIDEO_UPLOAD_REFRESH_MAX_MS) {
      pendingVideoUploadBatch = pending.batch;
      pendingVideoUploadStarted = pending.started;
      if (autoRefreshVideoUploads) pollVideoUploadRefresh(pending.batch, pending.started);
      else setVideoUploadStatus('Auto-refresh paused.');
    } else if (pending) {
      localStorage.removeItem(VIDEO_PENDING_UPLOAD_KEY);
    }
  } catch (e) {}
}

function pollStatus() {
  fetch('/server-status')
    .then(r => r.json())
    .then(d => {
      const pPill = document.getElementById('primaryPill');
      const sPill = document.getElementById('sessionPill');
      const sText = document.getElementById('sessionPillText');
      const secContainer = document.getElementById('secondariesPills');

      if (!d.ok) {
        pPill.className = 'live-pill offline';
        pPill.innerHTML = '<span class="live-dot"></span> Primary Down';
        sPill.style.display = 'none';
        secContainer.innerHTML = '';
        return;
      }

      const p = d.primary || {};
      videoRecordingActive = !!(p.video && p.video.recording);
      const photoTab = document.querySelector('.mode-tab[href="/"]');
      if (photoTab) {
        photoTab.classList.toggle('locked', videoRecordingActive);
        photoTab.setAttribute('aria-disabled', videoRecordingActive ? 'true' : 'false');
        photoTab.title = videoRecordingActive
          ? 'Stop the active video recording before switching to Photos'
          : 'Photo capture mode';
      }
      if (videoRecordingActive && PAGE_MODE === 'photo') {
        showToast('Video recording is active. Photo mode is locked until it stops.', 'info', 6000);
        window.location.replace('/video');
        return;
      }
      const currentVideo = p.video || {};
      if (PAGE_MODE === 'video') {
        if (currentVideo.recording && currentVideo.batch) {
          observedVideoBatch = currentVideo.batch;
        } else if (observedVideoRecording && observedVideoBatch) {
          beginVideoUploadRefresh(observedVideoBatch);
          observedVideoBatch = null;
        }
        observedVideoRecording = !!currentVideo.recording;
      }
      const primaryErrors = cameraErrorsOf(p);
      renderDevicePill(pPill,
        'Primary (' + (p.cameras || 0) + ' cam' + (p.cameras === 1 ? '' : 's') + ')',
        primaryErrors);
      videoState = p.video || null;
      applyCaps(p, d.secondaries || {});
      const capsPill = document.getElementById('capsPill');
      if (capsPill) {
        const capsData = p.caps;
        const capsOk = Array.isArray(capsData && capsData.sizes) && capsData.sizes.length;
        capsPill.hidden = !!capsOk;
        if (!capsOk) {
          capsPill.setAttribute('title', (capsData && capsData.error) ? capsData.error : 'no capability data from primary');
          if (!window.capsWarned) {
            window.capsWarned = true;
            showToast(capsData && capsData.error
              ? 'Camera settings unavailable: ' + capsData.error + ' — using default presets.'
              : 'No camera data from primary (restart cam_server to enable real settings) — using default presets.', 'info', 6000);
          }
        } else {
          window.capsWarned = false;
        }
      }
      updateRecordUI();

      if (p.session) {
        sPill.style.display = 'inline-flex';
        sPill.className = 'live-pill active-session';
        sText.textContent = 'Active: ' + p.session + (p.session_kind === 'video' ? ' (Video)' : '');
      } else {
        sPill.style.display = 'none';
      }

      const secs = Object.entries(d.secondaries || {});
      const cameraAlerts = [['Primary', primaryErrors]];
      if (secs.length) {
        secContainer.replaceChildren(...secs.map(([k, v]) => {
          const pill = document.createElement('span');
          if (v.up) {
            const errors = cameraErrorsOf(v);
            cameraAlerts.push([k, errors]);
            renderDevicePill(pill,
              k + ' (' + (v.cameras || 0) + ' cam' + (v.cameras === 1 ? '' : 's') + ')' + (v.recording ? ' · REC' : ''),
              errors, false, v.addr || '');
          } else {
            renderDevicePill(pill, k + ' Down', [], true,
              (v.addr || '') + (v.error ? ' — ' + v.error : ''));
          }
          return pill;
        }));
      } else {
        secContainer.innerHTML = '<span class="live-pill" style="opacity:0.7">0 secondaries</span>';
      }
      notifyCameraErrors(cameraAlerts);
    })
    .catch(() => {
      const pPill = document.getElementById('primaryPill');
      pPill.className = 'live-pill offline';
      pPill.innerHTML = '<span class="live-dot"></span> Connection Error';
    });
}

document.querySelectorAll('.mode-tab').forEach(tab => {
  tab.addEventListener('click', event => {
    if (tab.getAttribute('href') === '/' && videoRecordingActive) {
      event.preventDefault();
      showToast('Stop the video recording before switching to Photos.', 'info');
    }
  });
});

const stillSel = document.getElementById('stillRes');
if (stillSel) stillSel.addEventListener('change', () => {
  fetch('/still-size?size=' + encodeURIComponent(stillSel.value), {method: 'POST'}).catch(() => {});
});
const vidResSel = document.getElementById('vidRes');
if (vidResSel) vidResSel.addEventListener('change', rebuildVideoFps);
initVideoUploadRefreshToggle();
pollStatus();
setInterval(pollStatus, 5000);
initSortControls();
applySorts();
modeSync();"""


def page_js(mode):
    js = PAGE_JS.replace("__REFRESH_MS__", str(CAPTURE_REFRESH_MS)).replace("__PAGE_MODE__", mode)
    if mode == "video":
        js += "\n" + VIDEO_JS.replace("__REFRESH_MS__", str(CAPTURE_REFRESH_MS))
    return js


def base_page(mode, cards_content, session_settings=None, ungrouped_count=0):
    title = "Capture Gallery" if mode == "photo" else "Video Capture"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="/static/photoswipe/photoswipe.css">
<style>
{PAGE_CSS}
</style>
</head>
<body>

{header_html(mode)}

{main_html(mode, cards_content, session_settings, ungrouped_count)}

<div class="modal-backdrop" id="purgeModal">
  <div class="modal-box">
    <h3>Purge Secondary Captures?</h3>
    <p>This will permanently delete local capture files stored on registered secondary Pis to free up disk space. Captures already synced to the primary will not be affected.</p>
    <div class="modal-actions">
      <button class="btn" onclick="closePurgeModal()">Cancel</button>
      <button class="btn btn-primary" style="background:var(--accent-danger);border-color:var(--accent-danger);" id="btnConfirmPurge" onclick="confirmPurge()">Delete Captures</button>
    </div>
  </div>
</div>

{mobile_bar_html(mode)}

<div class="toast-container" id="toastContainer"></div>

<script>
{page_js(mode)}
</script>

<script type="module">
import PhotoSwipeLightbox from '/static/photoswipe/photoswipe-lightbox.esm.js';
window.PhotoSwipeLightbox = PhotoSwipeLightbox;
initLightbox();
</script>
</body>
</html>"""


def gallery_html(sections, ungrouped_count=0):
    return base_page("photo", gallery_cards_html(sections), ungrouped_count=ungrouped_count)


def video_html(sections, session_settings=None, ungrouped_count=0):
    return base_page("video", video_cards_html(sections), session_settings, ungrouped_count)


VIDEO_JS = '''
let videoState = null;
let recTimer = null;

function fmtElapsed(total) {
  const s = Math.max(0, Math.floor(total));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = (m < 10 ? '0' : '') + m;
  const ss = (sec < 10 ? '0' : '') + sec;
  return h > 0 ? h + ':' + mm + ':' + ss : mm + ':' + ss;
}

function videoSettingsQuery() {
  const res = document.getElementById('vidRes').value;
  const fps = document.getElementById('vidFps').value;
  const qual = document.getElementById('vidQual').value;
  const highres = document.getElementById('vidHighRes').checked ? '1' : '0';
  const durRaw = parseFloat(document.getElementById('vidDur').value);
  const dur = (isNaN(durRaw) || durRaw < 0) ? 0 : durRaw;
  return 'res=' + res + '&fps=' + fps + '&quality=' + qual + '&dur=' + dur + '&highres=' + highres;
}

function saveVideoSettings() {
  try {
    localStorage.setItem('mcVideoSettings', JSON.stringify({
      res: document.getElementById('vidRes').value,
      fps: document.getElementById('vidFps').value,
      quality: document.getElementById('vidQual').value,
      duration: document.getElementById('vidDur').value,
      highres: document.getElementById('vidHighRes').checked
    }));
  } catch (e) {}
}

function toggleRecord(btn) {
  if (videoState && videoState.recording) stopRecord(btn);
  else startRecord(btn);
}

function startRecord(btn) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> <span>Starting...</span>';
  fetch('/video/start?' + videoSettingsQuery(), {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        saveVideoSettings();
        showToast('Recording started: ' + (d.batch || ''), 'info', 6000);
        pollStatus();
      } else {
        showToast('Record failed: ' + (d.error || 'Unknown error'), 'error');
      }
    })
    .catch(e => showToast('Record error: ' + e, 'error'))
    .finally(() => {
      btn.disabled = false;
      btn.innerHTML = original;
    });
}

function stopRecord(btn) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> <span>Stopping...</span>';
  fetch('/video/stop', {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        showToast('Recording saved (' + Math.round(d.duration || 0) + 's): ' + (d.batch || ''), 'success');
        pollStatus();
        beginVideoUploadRefresh(d.batch);
      } else {
        showToast('Stop failed: ' + (d.error || ''), 'error');
      }
    })
    .catch(e => showToast('Stop error: ' + e, 'error'))
    .finally(() => {
      btn.disabled = false;
      btn.innerHTML = original;
    });
}

function updateRecordUI() {
  const pill = document.getElementById('recPill');
  if (!pill) return;
  const btns = Array.from(document.querySelectorAll('#btnRecord, #btnRecordM'));
  const text = document.getElementById('recPillText');
  if (videoState && videoState.recording) {
    const el = fmtElapsed(Date.now() / 1000 - (videoState.started || 0));
    pill.hidden = false;
    if (text) text.textContent = 'REC ' + el + ' · ' + (videoState.batch || '');
    btns.forEach(b => {
      if (b.disabled) return;
      b.classList.add('btn-stop');
      const t = b.querySelector('span:last-child');
      if (t) t.textContent = 'Stop Recording';
    });
    if (!recTimer) recTimer = setInterval(updateRecordUI, 1000);
  } else {
    pill.hidden = true;
    btns.forEach(b => {
      b.classList.remove('btn-stop');
      const t = b.querySelector('span:last-child');
      if (t) t.textContent = 'Start Recording';
    });
    if (recTimer) {
      clearInterval(recTimer);
      recTimer = null;
    }
  }
}

function initVideoSettings() {
  const grid = document.getElementById('videoSettings');
  if (!grid) return;
  const prefilled = grid.getAttribute('data-prefilled') === '1';
  let vs = {};
  try {
    if (!prefilled) vs = JSON.parse(localStorage.getItem('mcVideoSettings') || '{}');
    if (!prefilled && vs && typeof vs === 'object') {
      if (vs.res) document.getElementById('vidRes').value = vs.res;
      if (vs.fps) document.getElementById('vidFps').value = vs.fps;
      if (vs.quality) document.getElementById('vidQual').value = vs.quality;
      if (typeof vs.duration !== 'undefined') document.getElementById('vidDur').value = vs.duration;
    }
  } catch (e) {}
  const highresToggle = document.getElementById('vidHighRes');
  if (!prefilled && typeof vs.highres === 'boolean') highresToggle.checked = vs.highres;
  if (highresToggle) highresToggle.addEventListener('change', () => {
    rebuildVideoSizes();
    rebuildVideoFps();
    saveVideoSettings();
  });
}

initVideoSettings();
'''




class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=CAPTURES_DIR, **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", ""):
            partial = "partial" in query and query["partial"][0] in ("1", "true")
            if not partial:
                try:
                    resp = urllib.request.urlopen(
                        f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/status", timeout=2)
                    primary_status = json.loads(resp.read().decode())
                    if primary_status.get("video", {}).get("recording"):
                        self.send_response(303)
                        self.send_header("Location", "/video")
                        self.end_headers()
                        return
                except Exception:
                    pass
            self._serve_gallery(partial=partial)
        elif path == "/video":
            partial = "partial" in query and query["partial"][0] in ("1", "true")
            self._serve_video(partial=partial)
        elif path == "/server-status":
            self._send_json(200, self._server_status())
        elif path.startswith("/static/"):
            self._serve_static()
        elif path.startswith("/zip/"):
            self._serve_zip(urllib.parse.unquote(path[len("/zip/"):]), query)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.urlparse(self.path).query
        if path == "/capture":
            body = self._proxy("/remote-capture", timeout=10)
            self._send_json(200, {"ok": body.get("ok", False), "primary": body})
        elif path == "/sync":
            body = self._proxy("/sync", timeout=90)
            self._send_json(200, {"ok": body.get("ok", False), "secondaries": body.get("secondaries", {})})
        elif path == "/purge-secondaries":
            body = self._proxy("/purge-secondaries", timeout=60)
            self._send_json(200, {"ok": body.get("ok", False), "secondaries": body.get("secondaries", {})})
        elif path in ("/session/start", "/session/stop", "/session/delete", "/session/batch/delete", "/captures/delete", "/captures/delete-all", "/still-size"):
            body = self._proxy(path + (f"?{qs}" if qs else ""), timeout=10)
            self._send_json(200, body)
        elif path == "/video/start":
            body = self._proxy("/video/start" + (f"?{qs}" if qs else ""), timeout=30)
            self._send_json(200, body)
        elif path == "/video/stop":
            body = self._proxy("/video/stop", timeout=30)
            self._send_json(200, body)
        elif path == "/mode":
            body = self._proxy("/mode" + (f"?{qs}" if qs else ""), timeout=5)
            self._send_json(200, body)
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def _proxy(self, path, timeout=10):
        url = f"http://{PRIMARY_HOST}:{PRIMARY_PORT}{path}"
        try:
            req = urllib.request.Request(url, data=b"", method="POST")
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read().decode())
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _server_status(self):
        result = {"ok": False, "primary": None, "secondaries": {}}
        try:
            resp = urllib.request.urlopen(f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/status", timeout=2)
            result["primary"] = json.loads(resp.read().decode())
            result["ok"] = True
        except Exception as e:
            result["error"] = str(e)
            return result
        for pi_id, addr in result["primary"].get("secondaries", {}).items():
            ip, _, port = addr.partition(":")
            entry = {"addr": addr, "up": False, "cameras": None}
            try:
                resp = urllib.request.urlopen(f"http://{ip}:{port}/status", timeout=2)
                data = json.loads(resp.read().decode())
                entry["up"] = True
                entry["pi_id"] = data.get("pi_id", pi_id)
                entry["cameras"] = data.get("cameras")
                entry["available_cameras"] = data.get("available_cameras")
                entry["recording"] = bool(data.get("recording"))
                entry["caps"] = data.get("caps")
                entry["camera_errors"] = data.get("camera_errors", [])
            except Exception as e:
                entry["error"] = str(e)
            result["secondaries"][pi_id] = entry
        return result

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_gallery(self, partial=False):
        by_name = {}
        if os.path.isdir(CAPTURES_DIR):
            for name in sorted(os.listdir(CAPTURES_DIR), reverse=True):
                path = os.path.join(CAPTURES_DIR, name)
                if os.path.isdir(path):
                    entries = []
                    for f in sorted(os.listdir(path)):
                        if not f.endswith(".jpg") or f.endswith("_thumb.jpg"):
                            continue
                        thumb = f[:-4] + "_thumb.jpg"
                        if not os.path.exists(os.path.join(path, thumb)):
                            thumb = None
                        size = jpeg_size(os.path.join(path, f))
                        w, h = size if size else (1920, 1080)
                        entries.append((f, thumb, w, h))
                    if entries:
                        by_name[name] = entries
        sessions = []
        try:
            with open(os.path.join(CAPTURES_DIR, "sessions.json")) as f:
                sessions = json.load(f)
        except Exception:
            sessions = []
        sections = []
        for s in reversed(sessions):
            cards = [(b, by_name.pop(b, [])) for b in s.get("batches", []) if b in by_name]
            if cards:
                sections.append((s["name"], cards, True, session_created(s)))
        une = len(by_name)
        if by_name:
            ung_eps = [batch_epoch(n) for n in by_name]
            ung_eps = [e for e in ung_eps if e is not None]
            sections.append(("Ungrouped", [(n, e) for n, e in by_name.items()], False, max(ung_eps) if ung_eps else 0.0))
        if partial:
            body = gallery_cards_html(sections).encode("utf-8")
        else:
            body = gallery_html(sections, une).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self):
        rel = urllib.parse.urlparse(self.path).path[len("/static/"):]
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = "application/javascript" if full.endswith(".js") else "text/css"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _session_batches(self, sname):
        try:
            with open(os.path.join(CAPTURES_DIR, "sessions.json")) as f:
                sessions = json.load(f)
        except Exception:
            return []
        for s in sessions:
            if s.get("name") == sname:
                return s.get("batches", [])
        return []

    def _load_sessions(self):
        try:
            with open(os.path.join(CAPTURES_DIR, "sessions.json")) as f:
                return json.load(f)
        except Exception:
            return []

    def _video_sections(self):
        by_name = {}
        if os.path.isdir(CAPTURES_DIR):
            for name in sorted(os.listdir(CAPTURES_DIR), reverse=True):
                path = os.path.join(CAPTURES_DIR, name)
                if os.path.isdir(path):
                    entries = []
                    for f in sorted(os.listdir(path)):
                        if f.endswith(VIDEO_EXTENSIONS):
                            try:
                                size = os.path.getsize(os.path.join(path, f))
                            except OSError:
                                size = 0
                            entries.append((f, size))
                    if entries:
                        by_name[name] = entries
        sessions = self._load_sessions()
        sections = []
        active_settings = None
        for s in reversed(sessions):
            if s.get("kind", "image") != "video":
                continue
            if s.get("active") and s.get("video_settings"):
                active_settings = s["video_settings"]
            cards = [(b, by_name.pop(b, [])) for b in s.get("batches", []) if b in by_name]
            if cards:
                sections.append((s["name"], cards, True, session_created(s)))
        if by_name:
            ung_eps = [batch_epoch(n) for n in by_name]
            ung_eps = [e for e in ung_eps if e is not None]
            sections.append(("Ungrouped", [(n, e) for n, e in by_name.items()], False, max(ung_eps) if ung_eps else 0.0))
        return sections, active_settings, len(by_name)

    def _serve_video(self, partial=False):
        sections, active_settings, ungrouped_count = self._video_sections()
        if partial:
            body = video_cards_html(sections).encode("utf-8")
        else:
            body = video_html(sections, active_settings, ungrouped_count).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_zip(self, target, query=None):
        groups = []
        if target == "multi":
            raw_s = (query or {}).get("sessions", [""])[0]
            raw_b = (query or {}).get("batches", [""])[0]
            for sname in [urllib.parse.unquote(n) for n in raw_s.split(",") if n]:
                groups.extend((b, "sessions/" + sname) for b in self._session_batches(sname))
            groups.extend((urllib.parse.unquote(n), "batches") for n in raw_b.split(",") if n)
            dlname = "selected-captures.zip"
        elif target.startswith("session/"):
            names = self._session_batches(target[len("session/"):])
            dlname = target.replace("/", "_") + ".zip"
            groups.extend((n, "sessions/" + target[len("session/"):]) for n in names)
        else:
            groups.append((target, ""))
            dlname = target + ".zip"
        folders = []
        for n, prefix in groups:
            if not all(c.isalnum() or c == "_" for c in n):
                self.send_error(400)
                return
            full = os.path.join(CAPTURES_DIR, n)
            if os.path.isdir(full):
                folders.append((full, prefix))
        if not folders:
            self.send_error(404)
            return
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
                for folder, prefix in folders:
                    for fname in sorted(os.listdir(folder)):
                        if fname.endswith("_thumb.jpg"):
                            continue
                        if fname.endswith(".jpg") or fname.endswith(VIDEO_EXTENSIONS):
                            zf.write(
                                os.path.join(folder, fname),
                                arcname=os.path.join(prefix, os.path.basename(folder), fname),
                            )
            size = os.path.getsize(tmp)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{dlname}"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(tmp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            os.remove(tmp)


def main():
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("", PORT), Handler)
    print(f"Gallery on http://0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
