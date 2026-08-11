import os
import json
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


def gallery_html(folders):
    cards = []
    for name, imgs in folders:
        thumbs = "".join(
            f'<figure><img src="{name}/{img}" loading="lazy"><figcaption>{img}</figcaption></figure>'
            for img in imgs
        )
        cards.append(f'<div class="card"><h2>{name}</h2><div class="grid">{thumbs}</div></div>')
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Capture Gallery</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:.75rem}}
figure{{margin:0}}
img{{width:100%;height:auto;display:block;border-radius:6px}}
.card{{margin-bottom:2rem;border-top:2px solid #ddd;padding-top:1rem}}
button{{font-size:1.1rem;padding:.6rem 1.4rem;cursor:pointer}}
#status{{margin-left:1rem;color:#666}}
</style></head>
<body>
<h1>Capture Gallery</h1>
<button onclick="capture()">Capture All</button><span id="status"></span>
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
</script>
</body></html>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=CAPTURES_DIR, **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path in ("/", ""):
            self._serve_gallery()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/capture":
            url = f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/remote-capture"
            try:
                req = urllib.request.Request(url, data=b"", method="POST")
                resp = urllib.request.urlopen(req, timeout=10)
                body = json.loads(resp.read().decode())
                self._send_json(200, {"ok": True, "primary": body})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_gallery(self):
        folders = []
        if os.path.isdir(CAPTURES_DIR):
            for name in sorted(os.listdir(CAPTURES_DIR), reverse=True):
                path = os.path.join(CAPTURES_DIR, name)
                if os.path.isdir(path):
                    imgs = sorted(f for f in os.listdir(path) if f.endswith(".jpg"))
                    folders.append((name, imgs))
        body = gallery_html(folders).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("", PORT), Handler)
    print(f"Gallery on http://0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
