from picamera2 import Picamera2
import cv2
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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

PORT = int(os.getenv("PREVIEW_PORT", "9090"))
SIZE = tuple(map(int, os.getenv("SIZE", "640x480").split("x", 1)))

cameras = []
for i in range(2):
    try:
        cam = Picamera2(i)
        cam.configure(cam.create_still_configuration(main={"size": SIZE, "format": "RGB888"}))
        cam.start()
        time.sleep(1)
        cameras.append(cam)
        print(f"Camera {i} connected. Streaming {SIZE}")
    except Exception as e:
        print(f"Camera {i} not connected: {e}")

if not cameras:
    print("No cameras found. Exiting.")
    sys.exit(1)

capture_lock = threading.Lock()


def jpeg_bytes(cam):
    with capture_lock:
        raw = cam.capture_array()
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(cv2.flip(raw, -1), cv2.COLOR_RGB2BGR))
    return buf.tobytes() if ok else b""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            imgs = "".join(
                f'<img src="/stream/{i}" width="480" alt="cam {i}">' for i in range(len(cameras))
            )
            body = (
                "<html><head><title>Camera preview</title></head>"
                f'<body style="background:#111;margin:0">{imgs}</body></html>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/stream/"):
            idx = path[len("/stream/"):]
            if not (idx.isdigit() and int(idx) < len(cameras)):
                self.send_error(404)
                return
            cam = cameras[int(idx)]
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    data = jpeg_bytes(cam)
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    time.sleep(1 / 12)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self.send_error(404)


def main():
    server = ThreadingHTTPServer(("", PORT), Handler)
    print(f"Camera preview on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for cam in cameras:
            cam.stop()
        server.server_close()


if __name__ == "__main__":
    main()
