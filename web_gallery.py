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
        safe_title = urllib.parse.quote(title) if title else "ungrouped"
        session_key = f"sess_{safe_title}"

        batch_cards = []
        for name, entries in cardlist:
            formatted_time, hsh = format_batch_display(name)
            hsh_badge = f'<span class="hash-tag">{hsh}</span>' if hsh else ""
            del_batch = ""
            if not is_session:
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
                f'<div class="batch-card" data-batch-name="{name}">'
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
'''


def gallery_html(sections):
    cards_content = gallery_cards_html(sections)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capture Gallery</title>
<link rel="stylesheet" href="/static/photoswipe/photoswipe.css">
<style>
{PAGE_CSS}
</style>
</head>
<body>

<header class="app-header">
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
      </div>
      <div class="status-pills-bar" id="statusPills">
        <span class="live-pill" id="primaryPill"><span class="live-dot"></span> Checking Primary...</span>
        <span class="live-pill" id="sessionPill" style="display:none;"><span class="live-dot"></span> <span id="sessionPillText">No Session</span></span>
        <span id="secondariesPills"></span>
      </div>
    </div>

    <div class="controls-bar">
      <div class="actions-group">
        <button class="btn btn-primary" id="btnCapture" onclick="triggerCapture(this)">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="3"></circle></svg>
          <span>Capture All</span>
        </button>
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
</header>

<main class="main-content">
  <div class="gallery-subbar">
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
      <label class="sort-group">Photos
        <select class="select-sort" id="selSortImages" onchange="setSort('images', this.value)">
          <option value="file">Filename</option>
          <option value="device">Device cam ID</option>
        </select>
      </label>
      <button class="btn-text" onclick="toggleAllAccordions(true)">Expand All</button>
      <span style="color:var(--border-strong)">·</span>
      <button class="btn-text" onclick="toggleAllAccordions(false)">Collapse All</button>
      <span style="color:var(--border-strong)">·</span>
      <button class="btn-text" id="btnSelectMode" onclick="toggleSelectMode()">Select</button>
      <button class="btn-text" id="btnSelectAllBtn" onclick="toggleSelectAll()" hidden>Select All</button>
      <button class="btn btn-primary btn-batch-dl" id="btnBatchDl" onclick="downloadSelected()" hidden>Download Selected (0)</button>
    </div>
  </div>

  <div id="galleryContainer">
    {cards_content}
  </div>
</main>

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

<div class="mobile-bar" id="mobileBar">
  <button class="btn btn-primary btn-capture-mobile" id="btnCaptureM" onclick="triggerCapture(this)">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="3"></circle></svg>
    <span>Capture All</span>
  </button>
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
</div>

<div class="toast-container" id="toastContainer"></div>

<script>
let lightbox = null;

function showToast(message, type = 'info', duration = 4000) {{
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {{
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.2s ease';
    setTimeout(() => toast.remove(), 250);
  }}, duration);
}}

function initLightbox() {{
  if (typeof PhotoSwipeLightbox === 'undefined') return;
  if (lightbox) {{
    lightbox.destroy();
  }}
  lightbox = new PhotoSwipeLightbox({{
    gallery: '.grid',
    children: 'a.pswp-link',
    pswpModule: () => import('/static/photoswipe/photoswipe.esm.js')
  }});
  lightbox.init();
}}

function getOpenAccordionKeys() {{
  const openKeys = new Set();
  document.querySelectorAll('.session-accordion[open]').forEach(el => {{
    const key = el.getAttribute('data-session-key');
    if (key) openKeys.add(key);
  }});
  return openKeys;
}}

function restoreAccordionStates(openKeys) {{
  document.querySelectorAll('.session-accordion').forEach(el => {{
    const key = el.getAttribute('data-session-key');
    if (key && openKeys.has(key)) {{
      el.setAttribute('open', '');
      el.classList.add('is-open');
    }} else if (key && openKeys.size > 0) {{
      el.removeAttribute('open');
      el.classList.remove('is-open');
    }}
  }});
}}

function toggleAllAccordions(open) {{
  document.querySelectorAll('.session-accordion').forEach(el => {{
    if (open) {{
      el.setAttribute('open', '');
      el.classList.add('is-open');
    }} else {{
      el.removeAttribute('open');
      el.classList.remove('is-open');
    }}
  }});
}}

const SORT_STORE = 'mcGallerySort';
let sortPrefs = {{ sessions: 'newest', batches: 'newest', images: 'file' }};
try {{
  const saved = JSON.parse(localStorage.getItem(SORT_STORE) || '{{}}');
  if (saved && typeof saved === 'object') Object.assign(sortPrefs, saved);
}} catch (e) {{}}

function naturalKey(s) {{
  return s.toLowerCase().split(/(\\d+)/).map(t => (/^\\d+$/.test(t) ? t.padStart(12, '0') : t));
}}

function cmpNatural(a, b) {{
  const ka = naturalKey(a), kb = naturalKey(b);
  const n = Math.min(ka.length, kb.length);
  for (let i = 0; i < n; i++) {{
    if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
  }}
  return ka.length - kb.length;
}}

function cmpFile(a, b) {{
  if (a === b) return 0;
  return a < b ? -1 : 1;
}}

function applySorts() {{
  const cont = document.getElementById('galleryContainer');
  const sm = sortPrefs.sessions;
  const accs = Array.from(cont.querySelectorAll(':scope > .session-accordion'));
  accs.sort((a, b) => {{
    if (sm === 'name') {{
      const na = decodeURIComponent(a.getAttribute('data-session-name') || '');
      const nb = decodeURIComponent(b.getAttribute('data-session-name') || '');
      return na.localeCompare(nb);
    }}
    const ca = parseFloat(a.getAttribute('data-session-created') || '0');
    const cb = parseFloat(b.getAttribute('data-session-created') || '0');
    if (ca === cb) return 0;
    return sm === 'newest' ? cb - ca : ca - cb;
  }});
  accs.forEach(el => cont.appendChild(el));

  const bm = sortPrefs.batches;
  if (bm !== 'file') {{
    document.querySelectorAll('.session-body').forEach(body => {{
      const cards = Array.from(body.querySelectorAll(':scope > .batch-card'));
      cards.sort((a, b) => {{
        const na = a.getAttribute('data-batch-name') || '';
        const nb = b.getAttribute('data-batch-name') || '';
        return bm === 'newest' ? cmpNatural(nb, na) : cmpNatural(na, nb);
      }});
      cards.forEach(c => body.appendChild(c));
    }});
  }}

  const im = sortPrefs.images;
  document.querySelectorAll('.grid').forEach(grid => {{
    const figs = Array.from(grid.querySelectorAll(':scope > .thumb-card'));
    figs.sort((a, b) => {{
      const na = a.getAttribute('data-img') || '';
      const nb = b.getAttribute('data-img') || '';
      return im === 'device' ? cmpNatural(na, nb) : cmpFile(na, nb);
    }});
    figs.forEach(f => grid.appendChild(f));
  }});
}}

function setSort(kind, value) {{
  sortPrefs[kind] = value;
  try {{
    localStorage.setItem(SORT_STORE, JSON.stringify(sortPrefs));
  }} catch (e) {{}}
  applySorts();
  initLightbox();
}}

function initSortControls() {{
  document.getElementById('selSortSessions').value = sortPrefs.sessions;
  document.getElementById('selSortBatches').value = sortPrefs.batches;
  document.getElementById('selSortImages').value = sortPrefs.images;
}}

function toggleSelectMode() {{
  document.body.classList.toggle('select-mode');
  const on = document.body.classList.contains('select-mode');
  document.getElementById('btnSelectAllBtn').hidden = !on;
  if (!on) {{
    document.querySelectorAll('.sel-sess').forEach(cb => {{ cb.checked = false; }});
  }}
  updateBatchDl();
}}

function toggleSelectAll() {{
  const boxes = document.querySelectorAll('.sel-sess');
  const all = Array.from(boxes).every(cb => cb.checked);
  boxes.forEach(cb => {{ cb.checked = !all; }});
  updateBatchDl();
}}

function updateBatchDl() {{
  const n = document.querySelectorAll('.sel-sess:checked').length;
  const btn = document.getElementById('btnBatchDl');
  btn.hidden = n === 0;
  btn.textContent = 'Download Selected (' + n + ')';
  btn.disabled = n === 0;
}}

function downloadSelected() {{
  const sessions = [];
  const batches = [];
  document.querySelectorAll('.sel-sess:checked').forEach(cb => {{
    const name = decodeURIComponent(cb.getAttribute('data-name'));
    const enc = encodeURIComponent(name);
    (cb.getAttribute('data-is-session') === '1' ? sessions : batches).push(enc);
  }});
  if (!sessions.length && !batches.length) return;
  const url = '/zip/multi?sessions=' + sessions.join(',') + '&batches=' + batches.join(',');
  window.location.assign(url);
}}

document.addEventListener('change', e => {{
  if (e.target && e.target.classList && e.target.classList.contains('sel-sess')) {{
    updateBatchDl();
  }}
}});

function toggleOverflow(e) {{
  if (e) e.stopPropagation();
  const menu = document.getElementById('overflowMenu');
  menu.hidden = !menu.hidden;
}}

document.addEventListener('click', e => {{
  const menu = document.getElementById('overflowMenu');
  if (menu && !menu.hidden && !e.target.closest('.overflow-wrap')) {{
    menu.hidden = true;
  }}
}});

function refreshGallery(showToastMsg = false) {{
  const openKeys = getOpenAccordionKeys();
  return fetch('/?partial=1')
    .then(r => r.text())
    .then(html => {{
      document.getElementById('galleryContainer').innerHTML = html;
      restoreAccordionStates(openKeys);
      applySorts();
      initLightbox();
      initSortControls();
      if (typeof updateBatchDl === 'function') updateBatchDl();
      if (showToastMsg) showToast('Gallery updated', 'info', 2000);
    }})
    .catch(err => {{
      if (showToastMsg) showToast('Failed to refresh gallery: ' + err, 'error');
    }});
}}

function triggerCapture(btn) {{
  btn = btn || document.getElementById('btnCapture');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> <span>Capturing...</span>';
  
  fetch('/capture', {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{
        showToast('Capture complete!', 'success');
        setTimeout(() => refreshGallery(false), {CAPTURE_REFRESH_MS});
      }} else {{
        showToast('Capture failed: ' + (d.error || 'Unknown error'), 'error');
      }}
    }})
    .catch(e => showToast('Capture error: ' + e, 'error'))
    .finally(() => {{
      btn.disabled = false;
      btn.innerHTML = original;
    }});
}}

function triggerSync(btn) {{
  btn = btn || document.getElementById('btnSync');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="border-top-color:var(--text-main)"></span> <span>Syncing...</span>';
  
  fetch('/sync', {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (!d.ok) {{
        showToast('Sync failed: ' + (d.error || ''), 'error');
        return;
      }}
      const parts = Object.entries(d.secondaries || {{}}).map(([k, v]) =>
        v.error ? k + ': ' + v.error : k + ': +' + v.pushed + (v.failed ? ' (' + v.failed + ' failed)' : '')
      );
      showToast(parts.length ? 'Sync: ' + parts.join(' | ') : 'No secondaries registered.', 'success');
      setTimeout(() => refreshGallery(false), 600);
    }})
    .catch(e => showToast('Sync error: ' + e, 'error'))
    .finally(() => {{
      btn.disabled = false;
      btn.innerHTML = original;
    }});
}}

function openPurgeModal() {{
  document.getElementById('purgeModal').classList.add('active');
}}

function closePurgeModal() {{
  document.getElementById('purgeModal').classList.remove('active');
}}

function confirmPurge() {{
  const btn = document.getElementById('btnConfirmPurge');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Deleting...';

  fetch('/purge-secondaries', {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      closePurgeModal();
      if (!d.ok) {{
        showToast('Purge failed: ' + (d.error || ''), 'error');
      }} else {{
        const parts = Object.entries(d.secondaries || {{}}).map(([k, v]) =>
          v.error ? k + ': ' + v.error : k + ': ' + v.deleted + ' deleted'
        );
        showToast(parts.length ? 'Purged: ' + parts.join(' | ') : 'No secondaries registered.', 'success');
      }}
    }})
    .catch(e => {{
      closePurgeModal();
      showToast('Purge error: ' + e, 'error');
    }})
    .finally(() => {{
      btn.disabled = false;
      btn.textContent = 'Delete Captures';
    }});
}}

function startSession() {{
  const input = document.getElementById('sessname');
  const name = input.value.trim();
  if (!name) {{
    showToast('Please enter a session name.', 'info');
    return;
  }}
  fetch('/session/start?name=' + encodeURIComponent(name), {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{
        showToast('Session started: ' + d.session, 'success');
        input.value = '';
        pollStatus();
        setTimeout(() => refreshGallery(false), 500);
      }} else {{
        showToast('Start session failed: ' + (d.error || ''), 'error');
      }}
    }})
    .catch(e => showToast('Session error: ' + e, 'error'));
}}

function continueSession(btn) {{
  const acc = btn.closest('.session-accordion');
  const name = decodeURIComponent(acc.getAttribute('data-session-name'));
  fetch('/session/start?name=' + encodeURIComponent(name), {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{
        showToast('Continuing session: ' + d.session, 'success');
        pollStatus();
      }} else {{
        showToast('Continue failed: ' + (d.error || ''), 'error');
      }}
    }})
    .catch(e => showToast('Session error: ' + e, 'error'));
}}

function stopSession() {{
  fetch('/session/stop', {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{
        showToast('Session stopped', 'info');
        pollStatus();
        setTimeout(() => refreshGallery(false), 500);
      }} else {{
        showToast('Stop session failed: ' + (d.error || ''), 'error');
      }}
    }})
    .catch(e => showToast('Session error: ' + e, 'error'));
}}

function showDelConfirm(btn) {{
  const wrap = btn.parentElement;
  btn.hidden = true;
  wrap.querySelector('.del-confirm').hidden = false;
}}

function hideDelConfirm(btn) {{
  const wrap = btn.closest('.del-wrap');
  wrap.querySelector('.btn-del').hidden = false;
  wrap.querySelector('.del-confirm').hidden = true;
}}

function deleteSession(btn, files) {{
  const acc = btn.closest('.session-accordion');
  const name = decodeURIComponent(acc.getAttribute('data-session-name'));
  fetch('/session/delete?name=' + encodeURIComponent(name) + (files ? '&files=1' : ''), {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (!d.ok) {{
        showToast('Delete failed: ' + (d.error || ''), 'error');
        return;
      }}
      showToast(files ? 'Session deleted, files removed.' : 'Session removed, captures kept.', 'success');
      pollStatus();
      setTimeout(() => refreshGallery(false), 400);
    }})
    .catch(e => showToast('Delete error: ' + e, 'error'));
}}

function deleteBatch(btn) {{
  const card = btn.closest('.batch-card');
  const name = card.getAttribute('data-batch-name');
  fetch('/captures/delete?batch=' + encodeURIComponent(name), {{method: 'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (!d.ok) {{
        showToast('Delete failed: ' + (d.error || ''), 'error');
        return;
      }}
      showToast('Batch deleted: ' + name, 'success');
      setTimeout(() => refreshGallery(false), 400);
    }})
    .catch(e => showToast('Delete error: ' + e, 'error'));
}}

function pollStatus() {{
  fetch('/server-status')
    .then(r => r.json())
    .then(d => {{
      const pPill = document.getElementById('primaryPill');
      const sPill = document.getElementById('sessionPill');
      const sText = document.getElementById('sessionPillText');
      const secContainer = document.getElementById('secondariesPills');

      if (!d.ok) {{
        pPill.className = 'live-pill offline';
        pPill.innerHTML = '<span class="live-dot"></span> Primary Down';
        sPill.style.display = 'none';
        secContainer.innerHTML = '';
        return;
      }}

      const p = d.primary || {{}};
      pPill.className = 'live-pill online';
      pPill.innerHTML = '<span class="live-dot"></span> Primary (' + (p.cameras || 0) + ' cam' + (p.cameras === 1 ? '' : 's') + ')';

      if (p.session) {{
        sPill.style.display = 'inline-flex';
        sPill.className = 'live-pill active-session';
        sText.textContent = 'Active: ' + p.session;
      }} else {{
        sPill.style.display = 'none';
      }}

      const secs = Object.entries(d.secondaries || {{}});
      if (secs.length) {{
        secContainer.innerHTML = secs.map(([k, v]) => {{
          if (v.up) {{
            return '<span class="live-pill online" title="' + v.addr + '"><span class="live-dot"></span> ' + k + ' (' + (v.cameras || 0) + ' cam)</span>';
          }} else {{
            return '<span class="live-pill offline" title="' + v.addr + (v.error ? ' — ' + v.error : '') + '"><span class="live-dot"></span> ' + k + ' Down</span>';
          }}
        }}).join(' ');
      }} else {{
        secContainer.innerHTML = '<span class="live-pill" style="opacity:0.7">0 secondaries</span>';
      }}
    }})
    .catch(() => {{
      const pPill = document.getElementById('primaryPill');
      pPill.className = 'live-pill offline';
      pPill.innerHTML = '<span class="live-dot"></span> Connection Error';
    }});
}}

pollStatus();
setInterval(pollStatus, 5000);
initSortControls();
applySorts();
</script>

<script type="module">
import PhotoSwipeLightbox from '/static/photoswipe/photoswipe-lightbox.esm.js';
window.PhotoSwipeLightbox = PhotoSwipeLightbox;
initLightbox();
</script>
</body>
</html>"""


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
            self._serve_gallery(partial=partial)
        elif path == "/server-status":
            self._send_json(200, self._server_status())
        elif path.startswith("/static/"):
            self._serve_static()
        elif path.startswith("/zip/"):
            self._serve_zip(urllib.parse.unquote(path[len("/zip/"):]), query)
        else:
            super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
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
        elif path in ("/session/start", "/session/stop", "/session/delete", "/captures/delete"):
            body = self._proxy(path + (f"?{qs}" if qs else ""), timeout=10)
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
                entry["cameras"] = data.get("cameras")
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
        if by_name:
            ung_eps = [batch_epoch(n) for n in by_name]
            ung_eps = [e for e in ung_eps if e is not None]
            sections.append(("Ungrouped", [(n, e) for n, e in by_name.items()], False, max(ung_eps) if ung_eps else 0.0))
        if partial:
            body = gallery_cards_html(sections).encode("utf-8")
        else:
            body = gallery_html(sections).encode("utf-8")
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
                        if fname.endswith(".jpg") and not fname.endswith("_thumb.jpg"):
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
