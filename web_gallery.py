import os
import re
import json
import tempfile
import zipfile
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def load_env(path=".env"):
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
    cards = []
    for title, cardlist, is_session in sections:
        if title:
            dl = f' <a class="dl" href="/zip/session/{urllib.parse.quote(title)}">download</a>' if is_session else ""
            cards.append(f'<h2 class="sess">{title}{dl}</h2>')
        for name, entries in cardlist:
            thumbs = "".join(
                f'<figure><a href="{name}/{img}" data-pswp-width="{w}" data-pswp-height="{h}">'
                f'<img src="{name}/{thumb or img}" loading="lazy"></a>'
                f'<figcaption>{img} <a class="dl" href="{name}/{img}" download>save</a></figcaption></figure>'
                for img, thumb, w, h in entries
            )
            cards.append(
                f'<div class="card"><h3>{name} <a class="dl" href="/zip/{name}">download batch</a></h3>'
                f'<div class="grid">{thumbs}</div></div>'
            )
    return "".join(cards)


def gallery_html(sections):
    cards = gallery_cards_html(sections)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Capture Gallery</title>
<link rel="stylesheet" href="/static/photoswipe/photoswipe.css">
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:.75rem}}
figure{{margin:0}}
img{{width:100%;height:auto;display:block;border-radius:6px}}
.card{{margin-bottom:2rem;border-top:2px solid #ddd;padding-top:1rem}}
.sess{{color:#555;text-transform:uppercase;font-size:.9rem;letter-spacing:.05em;margin:1.5rem 0 .25rem}}
.dl{{font-size:.85rem;font-weight:normal;color:#888;margin-left:.5rem}}
button{{font-size:1.1rem;padding:.6rem 1.4rem;cursor:pointer}}
input{{font-size:1.1rem;padding:.6rem .8rem}}
#status{{margin-left:1rem;color:#666}}
#syncstatus{{margin-left:1rem;color:#666}}
#sessstatus{{margin-left:1rem;color:#666}}
#srvstatus{{margin:.5rem 0;color:#666}}
</style></head>
<body>
<h1>Capture Gallery</h1>
<div id="srvstatus"></div>
<button onclick="capture()">Capture All</button><span id="status"></span>
<button onclick="syncNow()">Sync now</button><span id="syncstatus"></span>
<button id="purgebtn" onclick="purge()">Purge secondaries</button>
<div id="sesspanel">
  <input id="sessname" placeholder="session name">
  <button onclick="startSession()">Start session</button>
  <button onclick="stopSession()">Stop session</button>
  <span id="sessstatus"></span>
</div>
{''.join(cards)}
<script>
function capture() {{
  const s = document.getElementById('status');
  s.textContent = 'Triggering capture...';
  fetch('/capture', {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{ s.textContent = d.ok ? 'Done. Refresh to view.' : 'Failed: ' + (d.error||''); setTimeout(()=>location.reload(), 800); }})
    .catch(e => {{ s.textContent = 'Error: ' + e; }});
}}
function syncNow() {{
  const s = document.getElementById('syncstatus');
  s.textContent = 'Syncing secondaries...';
  fetch('/sync', {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (!d.ok) {{ s.textContent = 'Failed: ' + (d.error||''); return; }}
      const parts = Object.entries(d.secondaries).map(([k,v]) =>
        v.error ? k+': error '+v.error : k+': pushed '+v.pushed+', failed '+v.failed);
      s.textContent = parts.length ? 'Synced — ' + parts.join(' | ') : 'No secondaries registered.';
      setTimeout(()=>location.reload(), 1200);
    }})
    .catch(e => {{ s.textContent = 'Error: ' + e; }});
}}
let purgeArmed = false;
function purge() {{
  const b = document.getElementById('purgebtn');
  const s = document.getElementById('syncstatus');
  if (!purgeArmed) {{
    purgeArmed = true;
    b.textContent = 'Really delete? Click again';
    setTimeout(() => {{ purgeArmed = false; b.textContent = 'Purge secondaries'; }}, 5000);
    return;
  }}
  b.textContent = 'Purging...';
  fetch('/purge-secondaries', {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{
      if (!d.ok) {{ s.textContent = 'Failed: ' + (d.error||''); }}
      else {{
        const parts = Object.entries(d.secondaries).map(([k,v]) =>
          v.error ? k+': error '+v.error : k+': deleted '+v.deleted);
        s.textContent = parts.length ? 'Purged — ' + parts.join(' | ') : 'No secondaries registered.';
      }}
      b.textContent = 'Purge secondaries';
      purgeArmed = false;
    }})
    .catch(e => {{ s.textContent = 'Error: ' + e; b.textContent = 'Purge secondaries'; purgeArmed = false; }});
}}
function startSession() {{
  const s = document.getElementById('sessstatus');
  const name = document.getElementById('sessname').value.trim();
  if (!name) {{ s.textContent = 'Enter a session name.'; return; }}
  s.textContent = 'Starting...';
  fetch('/session/start?name=' + encodeURIComponent(name), {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{ s.textContent = d.ok ? 'Active: ' + d.session : 'Failed: ' + (d.error||''); setTimeout(()=>location.reload(), 800); }})
    .catch(e => {{ s.textContent = 'Error: ' + e; }});
}}
function stopSession() {{
  const s = document.getElementById('sessstatus');
  s.textContent = 'Stopping...';
  fetch('/session/stop', {{method:'POST'}})
    .then(r => r.json())
    .then(d => {{ s.textContent = d.ok ? 'Stopped.' : 'Failed: ' + (d.error||''); setTimeout(()=>location.reload(), 800); }})
    .catch(e => {{ s.textContent = 'Error: ' + e; }});
}}
function pollStatus() {{
  fetch('/server-status')
    .then(r => r.json())
    .then(d => {{
      const el = document.getElementById('srvstatus');
      if (!d.ok) {{
        el.textContent = 'cam_server DOWN — start with ./launch_primary.sh start';
        el.style.color = '#c00';
        return;
      }}
      const p = d.primary;
      let parts = ['cam_server up · ' + (p.cameras || 0) + ' cams' + (p.session ? ' · session: ' + p.session : '')];
      const secs = Object.entries(d.secondaries);
      if (secs.length) {{
        parts.push(secs.map(([k, v]) => k + ' @ ' + v.addr + ': ' + (v.up ? 'up · ' + (v.cameras || 0) + ' cam' : 'DOWN')).join(' | '));
      }} else {{
        parts.push('no secondaries registered');
      }}
      el.textContent = parts.join(' — ');
      el.style.color = '#666';
    }})
    .catch(e => {{
      const el = document.getElementById('srvstatus');
      el.textContent = 'status error: ' + e;
      el.style.color = '#c00';
    }});
}}
pollStatus();
setInterval(pollStatus, 5000);
</script>
<script type="module">
import PhotoSwipeLightbox from '/static/photoswipe/photoswipe-lightbox.esm.js';
const lightbox = new PhotoSwipeLightbox({{
  gallery: '.grid',
  children: 'a',
  pswpModule: () => import('/static/photoswipe/photoswipe.esm.js')
}});
lightbox.init();
</script>
</body></html>"""


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
            self._serve_zip(urllib.parse.unquote(path[len("/zip/"):]))
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
        elif path in ("/session/start", "/session/stop"):
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

    def _serve_gallery(self):
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
        for s in sessions:
            cards = [(b, by_name.pop(b, [])) for b in s.get("batches", []) if b in by_name]
            if cards:
                sections.append((s["name"], cards, True))
        if by_name:
            sections.append(("Ungrouped", [(n, e) for n, e in by_name.items()], False))
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

    def _serve_zip(self, target):
        if target.startswith("session/"):
            names = self._session_batches(target[len("session/"):])
            dlname = target.replace("/", "_") + ".zip"
        else:
            names = [target]
            dlname = target + ".zip"
        folders = []
        for n in names:
            if not all(c.isalnum() or c == "_" for c in n):
                self.send_error(400)
                return
            full = os.path.join(CAPTURES_DIR, n)
            if os.path.isdir(full):
                folders.append(full)
        if not folders:
            self.send_error(404)
            return
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
                for folder in folders:
                    for fname in sorted(os.listdir(folder)):
                        if fname.endswith(".jpg") and not fname.endswith("_thumb.jpg"):
                            zf.write(
                                os.path.join(folder, fname),
                                arcname=os.path.join(os.path.basename(folder), fname),
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
