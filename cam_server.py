from picamera2 import Picamera2
from libcamera import controls
import cv2
import numpy as np
import time
import os
import hashlib
import json
import select
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import gpiod
    GPIO_ACTIVE = gpiod.line.Value.ACTIVE
except Exception as e:
    gpiod = None
    GPIO_ACTIVE = 0
    print(f"gpiod failed to import: {e}")


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

MODE = os.getenv("MODE", "p")
PI_ID = os.getenv("PI_ID", "pi1")
PRIMARY_HOST = os.getenv("PRIMARY_HOST", "192.168.1.100")
PRIMARY_PORT = int(os.getenv("PRIMARY_PORT", "8080"))
SECONDARY_PORT = int(os.getenv("SECONDARY_PORT", "8081"))
AF_MODE = os.getenv("AF_MODE", "continuous")
GPIO_TRIGGER_PIN = int(os.getenv("GPIO_TRIGGER_PIN", "19"))
MAX_SECONDARIES = int(os.getenv("MAX_SECONDARIES", "3"))
CAPTURES_DIR = os.getenv("CAPTURES_DIR", "captures")

MODE = MODE.strip().lower()
if MODE in ("p", "primary"):
    PRIMARY = True
elif MODE in ("s", "secondary"):
    PRIMARY = False
else:
    print(f"Invalid MODE: {MODE}")
    sys.exit(1)

cameras = []
for i in range(2):
    try:
        cam = Picamera2(i)
        config = cam.create_still_configuration()
        cam.configure(config)
        if AF_MODE == "continuous":
            cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        else:
            cam.set_controls({"AfMode": controls.AfModeEnum.Auto})
        cam.start()
        cameras.append(cam)
        print(f"Camera {i} connected. Resolution: {cam.stream_configuration()['size']}")
    except Exception as e:
        print(f"Camera {i} not connected: {e}")

if not cameras:
    print("No cameras found. Exiting.")
    sys.exit(1)

gpio_request = None
if GPIO_TRIGGER_PIN >= 0:
    if gpiod is None:
        print("WARNING: gpiod not installed. GPIO trigger disabled.")
    else:
        try:
            settings = gpiod.LineSettings(
                direction=gpiod.line.Direction.INPUT,
                bias=gpiod.line.Bias.PULL_UP,
                active_low=True
            )
            gpio_request = gpiod.request_lines(
                "/dev/gpiochip0",
                consumer="cam-server-trigger",
                config={GPIO_TRIGGER_PIN: settings},
            )
            print(f"GPIO trigger enabled on pin {GPIO_TRIGGER_PIN}")
        except Exception as e:
            print(f"WARNING: GPIO trigger failed: {e}")
            gpio_request = None

capture_lock = threading.Lock()


def grab_frames():
    frames = []
    for cam in cameras:
        if AF_MODE == "interval":
            cam._last_af_trigger = getattr(cam, "_last_af_trigger", 0)
            if time.time() - cam._last_af_trigger >= 5:
                cam.set_controls({"AfTrigger": controls.AfTriggerEnum.Start})
                cam._last_af_trigger = time.time()
        raw = cam.capture_array()
        frames.append(cv2.cvtColor(cv2.flip(raw, -1), cv2.COLOR_RGB2BGR))
    return frames


def jpeg_bytes(frame):
    ok, buf = cv2.imencode(".jpg", frame)
    return buf.tobytes() if ok else b""


def batch_stamp():
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    bucket = int(now * 5)
    h = hashlib.sha256(f"{stamp}_{bucket}".encode()).hexdigest()[:4]
    return f"{stamp}_{h}"


def write_frame(path, frame):
    cv2.imwrite(path, frame)
    h, w = frame.shape[:2]
    tw = 480
    th = max(1, int(h * tw / w))
    cv2.imwrite(path[:-4] + "_thumb.jpg", cv2.resize(frame, (tw, th)))


class Primary:
    def __init__(self):
        self.lock = threading.Lock()
        self.secondaries = {}
        self.current_batch = None
        self.session_path = os.path.join(CAPTURES_DIR, "sessions.json")
        self.sessions = []
        self.current_session = None
        self._load_sessions()

    def _load_sessions(self):
        try:
            with open(self.session_path) as f:
                self.sessions = json.load(f)
        except Exception:
            self.sessions = []
        for s in self.sessions:
            if s.get("active"):
                self.current_session = s["name"]

    def _save_sessions(self):
        with open(self.session_path, "w") as f:
            json.dump(self.sessions, f, indent=1)

    def session_start(self, name):
        name = name.strip()
        if not name or not all(c.isalnum() or c in "_- " for c in name):
            return {"ok": False, "error": "invalid session name"}
        with self.lock:
            if self.current_session:
                return {"ok": False, "error": f"session '{self.current_session}' already active"}
            self.current_session = name
            self.sessions.append({"name": name, "created": time.time(), "active": True, "batches": []})
            self._save_sessions()
            return {"ok": True, "session": name}

    def session_stop(self):
        with self.lock:
            if not self.current_session:
                return {"ok": False, "error": "no active session"}
            for s in self.sessions:
                if s["name"] == self.current_session:
                    s["active"] = False
            self.current_session = None
            self._save_sessions()
            return {"ok": True}

    def session_status(self):
        with self.lock:
            return {"ok": True, "active": self.current_session, "sessions": self.sessions}

    def new_batch(self):
        name = batch_stamp()
        batch = os.path.join(CAPTURES_DIR, name)
        os.makedirs(batch, exist_ok=True)
        if self.current_session:
            for s in self.sessions:
                if s["name"] == self.current_session:
                    s["batches"].append(name)
                    break
            self._save_sessions()
        return batch

    def do_capture(self, remote=False):
        with capture_lock:
            with self.lock:
                self.current_batch = self.new_batch()
                batch = self.current_batch
            frames = grab_frames()
            for idx, frame in enumerate(frames):
                write_frame(os.path.join(batch, f"{PI_ID}_cam{idx}.jpg"), frame)
            print(f"Captured {len(frames)} local frame(s) in {os.path.basename(batch)}")
        if remote:
            self.fire_secondaries(os.path.basename(batch))

    def fire_secondaries(self, batch):
        threads = []
        for pi_id, (ip, port) in list(self.secondaries.items()):
            t = threading.Thread(target=self._trigger, args=(pi_id, ip, port, batch), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=3)

    def _trigger(self, pi_id, ip, port, batch):
        url = f"http://{ip}:{port}/capture?batch={batch}"
        try:
            urllib.request.urlopen(url, timeout=3)
        except Exception as e:
            print(f"WARNING: secondary {pi_id} trigger failed: {e}")

    def handle_upload(self, pi_id, cam_idx, data, batch=None):
        with self.lock:
            if batch:
                batch_dir = os.path.join(CAPTURES_DIR, batch)
                os.makedirs(batch_dir, exist_ok=True)
            elif self.current_batch is None:
                self.current_batch = self.new_batch()
                print("No active batch; created from secondary upload")
            path = os.path.join(batch_dir if batch else self.current_batch, f"{pi_id}_cam{cam_idx}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is not None:
            h, w = frame.shape[:2]
            tw = 480
            th = max(1, int(h * tw / w))
            cv2.imwrite(path[:-4] + "_thumb.jpg", cv2.resize(frame, (tw, th)))
        print(f"Saved {pi_id}_cam{cam_idx}.jpg")

    def sync_secondaries(self):
        results = {}
        def pull(pi_id, ip, port):
            try:
                resp = urllib.request.urlopen(f"http://{ip}:{port}/sync", timeout=60)
                results[pi_id] = json.loads(resp.read().decode())
            except Exception as e:
                results[pi_id] = {"error": str(e)}
        threads = []
        for pi_id, (ip, port) in list(self.secondaries.items()):
            t = threading.Thread(target=pull, args=(pi_id, ip, port), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=65)
        return results

    def purge_secondaries(self):
        results = {}
        def purge(pi_id, ip, port):
            try:
                req = urllib.request.Request(f"http://{ip}:{port}/purge", data=b"", method="POST")
                resp = urllib.request.urlopen(req, timeout=30)
                results[pi_id] = json.loads(resp.read().decode())
            except Exception as e:
                results[pi_id] = {"error": str(e)}
        threads = []
        for pi_id, (ip, port) in list(self.secondaries.items()):
            t = threading.Thread(target=purge, args=(pi_id, ip, port), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=35)
        return results


def secondary_capture(batch=None):
    with capture_lock:
        frames = grab_frames()
        if not frames:
            print(f"{PI_ID}: no frames captured")
            return
        if not batch:
            batch = batch_stamp()
        local_dir = os.path.join(CAPTURES_DIR, batch)
        os.makedirs(local_dir, exist_ok=True)
        for idx, frame in enumerate(frames):
            data = jpeg_bytes(frame)
            if not data:
                continue
            fname = f"{PI_ID}_cam{idx}.jpg"
            with open(os.path.join(local_dir, fname), "wb") as f:
                f.write(data)
            qs = urllib.parse.urlencode({"pi_id": PI_ID, "cam_idx": idx, "batch": batch})
            url = f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/upload?{qs}"
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "image/jpeg"}, method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                with open(os.path.join(local_dir, fname) + ".ok", "w") as f:
                    f.write("ok")
            except Exception as e:
                print(f"WARNING: upload cam{idx} failed: {e}")
        print(f"{PI_ID}: saved {len(frames)} frame(s) to {batch}")


def sync_push():
    pushed = 0
    failed = 0
    if os.path.isdir(CAPTURES_DIR):
        for name in sorted(os.listdir(CAPTURES_DIR)):
            batch_dir = os.path.join(CAPTURES_DIR, name)
            if not os.path.isdir(batch_dir):
                continue
            for fname in sorted(os.listdir(batch_dir)):
                if not fname.endswith(".jpg") or os.path.exists(os.path.join(batch_dir, fname) + ".ok"):
                    continue
                with open(os.path.join(batch_dir, fname), "rb") as f:
                    data = f.read()
                stem = fname[:-4]
                pi_id, cam_idx = PI_ID, 0
                if "_cam" in stem:
                    pi, _, idx = stem.rpartition("_cam")
                    if pi and idx.isdigit():
                        pi_id, cam_idx = pi, int(idx)
                qs = urllib.parse.urlencode({"pi_id": pi_id, "cam_idx": cam_idx, "batch": name})
                url = f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/upload?{qs}"
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "image/jpeg"}, method="POST"
                )
                try:
                    urllib.request.urlopen(req, timeout=10)
                    with open(os.path.join(batch_dir, fname) + ".ok", "w") as f:
                        f.write("ok")
                    pushed += 1
                except Exception as e:
                    print(f"WARNING: sync upload {name}/{fname} failed: {e}")
                    failed += 1
    print(f"{PI_ID}: sync pushed {pushed}, failed {failed}")
    return {"pushed": pushed, "failed": failed}


def purge_local():
    deleted = 0
    if os.path.isdir(CAPTURES_DIR):
        for name in sorted(os.listdir(CAPTURES_DIR)):
            batch_dir = os.path.join(CAPTURES_DIR, name)
            if not os.path.isdir(batch_dir):
                continue
            for fname in sorted(os.listdir(batch_dir)):
                if fname.endswith(".jpg"):
                    deleted += 1
                try:
                    os.remove(os.path.join(batch_dir, fname))
                except OSError:
                    pass
            try:
                os.rmdir(batch_dir)
            except OSError:
                pass
    print(f"{PI_ID}: purged {deleted} local capture(s)")
    return {"deleted": deleted}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if PRIMARY:
            if path == "/status":
                with primary.lock:
                    secs = {k: f"{ip}:{port}" for k, (ip, port) in primary.secondaries.items()}
                self._send_json(200, {
                    "pi_id": PI_ID,
                    "mode": "primary",
                    "cameras": len(cameras),
                    "session": primary.current_session,
                    "secondaries": secs,
                })
            else:
                self._send_json(404, {"error": "not found"})
            return
        if path == "/capture":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            batch = qs.get("batch", [None])[0]
            threading.Thread(target=secondary_capture, args=(batch,), daemon=True).start()
            self._send_json(200, {"ok": True})
        elif path == "/status":
            self._send_json(200, {"pi_id": PI_ID, "cameras": len(cameras)})
        elif path == "/sync":
            self._send_json(200, sync_push())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if PRIMARY:
            if parsed.path == "/register":
                if len(primary.secondaries) >= MAX_SECONDARIES:
                    self._send_json(429, {"error": "too many secondaries"})
                    return
                pi_id = qs.get("pi_id", [""])[0]
                if not pi_id:
                    self._send_json(400, {"error": "missing pi_id"})
                    return
                port = int(qs.get("port", [str(SECONDARY_PORT)])[0])
                ip = self.client_address[0]
                with primary.lock:
                    primary.secondaries[pi_id] = (ip, port)
                print(f"Registered secondary {pi_id} at {ip}:{port}")
                self._send_json(200, {"ok": True, "secondaries": len(primary.secondaries)})
            elif parsed.path == "/upload":
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length) if length else b""
                pi_id = qs.get("pi_id", ["?"])[0]
                cam_idx = int(qs.get("cam_idx", ["0"])[0])
                batch = qs.get("batch", [None])[0]
                if batch and not all(c.isalnum() or c == "_" for c in batch):
                    self._send_json(400, {"error": "invalid batch"})
                    return
                primary.handle_upload(pi_id, cam_idx, data, batch)
                self._send_json(200, {"ok": True})
            elif parsed.path == "/remote-capture":
                primary.do_capture(remote=True)
                self._send_json(200, {"ok": True, "batch": os.path.basename(primary.current_batch)})
            elif parsed.path == "/sync":
                results = primary.sync_secondaries()
                self._send_json(200, {"ok": True, "secondaries": results})
            elif parsed.path == "/purge-secondaries":
                results = primary.purge_secondaries()
                self._send_json(200, {"ok": True, "secondaries": results})
            elif parsed.path == "/session/start":
                name = qs.get("name", [""])[0]
                self._send_json(200, primary.session_start(name))
            elif parsed.path == "/session/stop":
                self._send_json(200, primary.session_stop())
            else:
                self._send_json(404, {"error": "not found"})
        else:
            if parsed.path == "/purge":
                self._send_json(200, purge_local())
            else:
                self._send_json(404, {"error": "not found"})


def main():
    global primary
    primary = None
    if PRIMARY:
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        primary = Primary()
        server = ThreadingHTTPServer(("", PRIMARY_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Primary mode ({PI_ID}). HTTP on :{PRIMARY_PORT}")
    else:
        server = ThreadingHTTPServer(("", SECONDARY_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        qs = urllib.parse.urlencode({"pi_id": PI_ID, "port": SECONDARY_PORT})
        url = f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/register?{qs}"
        try:
            urllib.request.urlopen(url, data=b"", timeout=5)
            print(f"Secondary mode ({PI_ID}). Registered with {PRIMARY_HOST}")
        except Exception as e:
            print(f"WARNING: registration with primary failed: {e}")
        print(f"Secondary HTTP on :{SECONDARY_PORT}")

    gpio_debounce_until = 0
    while True:
        if gpio_request is not None and time.time() > gpio_debounce_until:
            if gpio_request.get_value(GPIO_TRIGGER_PIN) == GPIO_ACTIVE:
                gpio_debounce_until = time.time() + 0.5
                print("GPIO trigger pressed")
                if PRIMARY:
                    primary.do_capture(remote=False)
                else:
                    secondary_capture()
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if ready:
            line = sys.stdin.readline()
            if not line:
                time.sleep(0.1)
                continue
            key = line.strip().lower()
            if key == "q":
                print("Quitting.")
                break
            elif key == "c" and PRIMARY:
                primary.do_capture(remote=True)

    if gpio_request is not None:
        gpio_request.release()
    for cam in cameras:
        cam.stop()
    server.server_close()


if __name__ == "__main__":
    main()
