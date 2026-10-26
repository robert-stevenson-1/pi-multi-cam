from picamera2 import Picamera2
from libcamera import controls, Transform
import cv2
import numpy as np
import time
import os
import hashlib
import json
import select
import sys
import glob
import shutil
import threading
import urllib.parse
import urllib.request
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import gpiod
    GPIO_ACTIVE = gpiod.line.Value.ACTIVE
except Exception as e:
    gpiod = None
    GPIO_ACTIVE = 0
    print(f"gpiod failed to import: {e}")


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

MODE = os.getenv("MODE", "p")
PI_ID = os.getenv("PI_ID", "pi1")
PRIMARY_HOST = os.getenv("PRIMARY_HOST", "192.168.1.100")
PRIMARY_PORT = int(os.getenv("PRIMARY_PORT", "8080"))
SECONDARY_PORT = int(os.getenv("SECONDARY_PORT", "8081"))
AF_MODE = os.getenv("AF_MODE", "continuous")
GPIO_TRIGGER_PIN = int(os.getenv("GPIO_TRIGGER_PIN", "19"))
MAX_SECONDARIES = int(os.getenv("MAX_SECONDARIES", "3"))
CAPTURES_DIR = os.getenv("CAPTURES_DIR", "captures")
VID_RES = os.getenv("VID_RES", "1080")
VID_FPS = int(os.getenv("VID_FPS", "30"))
VID_QUALITY = os.getenv("VID_QUALITY", "medium")
VID_DURATION = float(os.getenv("VID_DURATION", "0"))
VID_MAX_RES = os.getenv("VID_MAX_RES", "1080")

MODE = MODE.strip().lower()
if MODE in ("p", "primary"):
    PRIMARY = True
elif MODE in ("s", "secondary"):
    PRIMARY = False
else:
    print(f"Invalid MODE: {MODE}")
    sys.exit(1)

try:
    camera_count = len(Picamera2.global_camera_info())
except Exception as e:
    print(f"Camera enumeration failed, probing 0-1: {e}")
    camera_count = 2

def _read_sensor_caps(cam):
    try:
        modes = cam.sensor_modes
    except Exception as e:
        print(f"WARNING: reading sensor modes failed: {e}")
        return []
    best = {}
    for m in modes:
        size = m.get("size")
        if not size:
            continue
        try:
            w, h = int(size[0]), int(size[1])
            fps = float(m.get("fps") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0 or fps < 5:
            continue
        cur = best.get((w, h))
        if cur is None or fps > cur:
            best[(w, h)] = fps
    return [{"size": [w, h], "fps": round(f, 2)} for (w, h), f in sorted(best.items())]


cameras = []
per_cam_caps = []
for i in range(camera_count):
    try:
        cam = Picamera2(i)
        caps = _read_sensor_caps(cam)
        config = cam.create_still_configuration()
        cam.configure(config)
        if AF_MODE == "continuous":
            cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        else:
            cam.set_controls({"AfMode": controls.AfModeEnum.Auto})
        cam.start()
        cameras.append(cam)
        per_cam_caps.append(caps)
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
            chip_path = None
            for path in sorted(glob.glob("/dev/gpiochip*")):
                try:
                    with gpiod.Chip(path) as chip:
                        chip.line_offset_from_id(f"GPIO{GPIO_TRIGGER_PIN}")
                    chip_path = path
                    break
                except Exception:
                    continue
            settings = gpiod.LineSettings(
                direction=gpiod.line.Direction.INPUT,
                bias=gpiod.line.Bias.PULL_UP,
                active_low=True
            )
            gpio_request = gpiod.request_lines(
                chip_path or "/dev/gpiochip0",
                consumer="cam-server-trigger",
                config={GPIO_TRIGGER_PIN: settings},
            )
            print(f"GPIO trigger enabled on pin {GPIO_TRIGGER_PIN}")
        except Exception as e:
            print(f"WARNING: GPIO trigger failed: {e}")
            gpio_request = None

capture_lock = threading.Lock()
video_lock = threading.Lock()
video_state = {"recording": False, "batch": None, "started": 0.0, "recs": [], "timer": None, "settings": None}

VID_SIZES = {"1080": (1920, 1080), "720": (1280, 720)}
VID_BITRATES = {"low": 4000000, "medium": 10000000, "high": 20000000}
VIDEO_EXTENSIONS = (".mp4", ".h264")
VID_MAX_STREAM = VID_SIZES.get(VID_MAX_RES, VID_SIZES["1080"])
CAP_STREAM_LADDER = [(1920, 1080), (1280, 720), (960, 540), (640, 360)]
CAP_FPS_STEPS = (15, 30, 60, 120)

def _intersect_caps(a, b):
    if not a or not b:
        return a or b
    sizes = {(e["size"][0], e["size"][1]) for e in a} & {(e["size"][0], e["size"][1]) for e in b}
    return [e for e in a if (e["size"][0], e["size"][1]) in sizes]


SENSOR_CAPS = []
if per_cam_caps:
    _caps = per_cam_caps[0]
    for c in per_cam_caps[1:]:
        _caps = _intersect_caps(_caps, c)
    SENSOR_CAPS = _caps
    print(f"Sensor modes (intersection over {len(per_cam_caps)} camera(s)): {SENSOR_CAPS}")


def max_fps_for_size(w, h):
    fps = 0
    for e in SENSOR_CAPS:
        mw, mh = e["size"]
        if mw >= w and mh >= h:
            fps = max(fps, e["fps"])
    return fps


def full_sensor_size():
    if not SENSOR_CAPS:
        return None
    return max(SENSOR_CAPS, key=lambda e: e["size"][0] * e["size"][1])["size"]


def stream_caps():
    capw, caph = VID_MAX_STREAM
    out = []
    for w, h in CAP_STREAM_LADDER:
        if w > capw or h > caph:
            continue
        fps = max_fps_for_size(w, h)
        if fps >= 10:
            out.append({"size": [w, h], "fps": round(fps, 2)})
    return out


def caps_payload():
    full = full_sensor_size()
    payload = {"sizes": stream_caps(), "full": list(full) if full else None}
    if not SENSOR_CAPS:
        payload["error"] = "sensor modes unavailable"
    return payload


def parse_size_token(tok):
    m = re.match(r"^(\d{2,5})x(\d{2,5})$", str(tok or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def resolve_stream_size(res):
    token = str(res or "")
    size = parse_size_token(token)
    if size is None and token in VID_SIZES:
        size = VID_SIZES[token]
    if size is None and VID_RES in VID_SIZES:
        size = VID_SIZES[VID_RES]
    if size is None:
        size = VID_SIZES["1080"]
    entries = stream_caps()
    if not entries:
        return size
    for e in entries:
        if tuple(e["size"]) == size:
            return size
    return tuple(entries[0]["size"])


def normalise_video_settings(st):
    size = resolve_stream_size(st.get("res"))
    entries = stream_caps()
    if entries:
        steps = fps_steps_for_size(size[0], size[1])
    else:
        steps = [15, 30]
    try:
        fps = int(st.get("fps") or VID_FPS)
    except (TypeError, ValueError):
        fps = VID_FPS
    fps = min(steps, key=lambda f: (abs(f - fps), f))
    quality = str(st.get("quality") or VID_QUALITY)
    if quality not in VID_BITRATES:
        quality = "medium"
    try:
        duration = float(st.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"res": f"{size[0]}x{size[1]}", "fps": fps, "quality": quality, "duration": max(0.0, duration)}


def fps_steps_for_size(w, h):
    cap = max_fps_for_size(w, h)
    steps = [f for f in CAP_FPS_STEPS if f <= cap]
    return steps or [5]


def normalise_still_size(token):
    t = str(token or "").strip().lower()
    if t in ("", "full", "sensor", "none", "0"):
        return None
    size = parse_size_token(t)
    if size is None:
        return None
    full = full_sensor_size()
    capw, caph = VID_MAX_STREAM
    limits = []
    if full:
        limits.append(full)
    limits.append((capw, caph))
    for lw, lh in limits:
        if size[0] > lw or size[1] > lh:
            size = (min(size[0], lw), min(size[1], lh))
    return size


still_state = {"size": None}
applied_still_size = {"size": None}


def ensure_still_config():
    desired = still_state["size"]
    if applied_still_size["size"] == desired:
        return
    for cam in cameras:
        try:
            cam.stop()
        except Exception:
            pass
        try:
            cfg = cam.create_still_configuration(
                main={} if desired is None else {"size": list(desired)})
            cam.configure(cfg)
            if AF_MODE == "continuous":
                cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
            else:
                cam.set_controls({"AfMode": controls.AfModeEnum.Auto})
            cam.start()
        except Exception as e:
            print(f"WARNING: still reconfiguration failed: {e}")
    applied_still_size["size"] = desired


def video_settings_from_qs(qs):
    return normalise_video_settings({
        "res": qs.get("res", [None])[0],
        "fps": qs.get("fps", [None])[0],
        "quality": qs.get("quality", [None])[0],
        "duration": qs.get("dur", [None])[0],
    })


def video_encoder_factories():
    factories = []
    try:
        from picamera2.encoders import H264Encoder
        factories.append(lambda bitrate: H264Encoder(bitrate=bitrate))
    except Exception as e:
        print(f"H264Encoder unavailable: {e}")
    try:
        from picamera2.encoders import LibavH264Encoder
        factories.append(lambda bitrate: LibavH264Encoder(bitrate=bitrate))
    except Exception as e:
        print(f"LibavH264Encoder unavailable: {e}")
    return factories


def video_output_factories(path_base):
    from picamera2.outputs import FileOutput
    factories = []
    try:
        from picamera2.outputs import FfmpegOutput
        factories.append(("ffmpeg", path_base + ".mp4", lambda: FfmpegOutput(path_base + ".mp4")))
    except Exception as e:
        print(f"FfmpegOutput unavailable: {e}")
    try:
        from picamera2.outputs import PyavOutput
        factories.append(("pyav", path_base + ".mp4", lambda: PyavOutput(path_base + ".mp4")))
    except Exception as e:
        print(f"PyavOutput unavailable: {e}")
    factories.append(("file", path_base + ".h264", lambda: FileOutput(path_base + ".h264")))
    return factories


def start_cam_recording(cam, path_base, st):
    size = parse_size_token(st["res"]) or VID_MAX_STREAM
    bitrate = VID_BITRATES[st["quality"]]
    vid_ctrl = {"FrameRate": st["fps"]}
    if AF_MODE == "continuous":
        vid_ctrl["AfMode"] = controls.AfModeEnum.Continuous
    cfg = cam.create_video_configuration(
        main={"size": size},
        buffer_count=4,
        transform=Transform(hflip=1, vflip=1),
        controls=vid_ctrl,
    )
    cam.stop()
    cam.configure(cfg)
    cam.start()
    last_error = None
    for enc_factory in video_encoder_factories():
        try:
            enc = enc_factory(bitrate)
        except Exception as e:
            print(f"WARNING: encoder construction failed: {e}")
            continue
        enc_name = enc.__class__.__name__
        prio = ("pyav", "ffmpeg", "file") if "Libav" in enc_name else ("ffmpeg", "pyav", "file")
        for kind, path, out_factory in sorted(video_output_factories(path_base),
                                              key=lambda o: prio.index(o[0])):
            try:
                cam.start_encoder(enc, out_factory())
            except Exception as e:
                last_error = e
                print(f"WARNING: {enc_name} + {kind} start failed: {e}")
                continue
            time.sleep(0.5)
            if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                try:
                    cam.stop_encoder()
                except Exception:
                    pass
                last_error = "encoder output produced no data"
                print(f"WARNING: {enc_name} + {kind} produced no data; trying next")
                continue
            print(f"Recording {path} ({enc_name} + {kind})")
            return cam, enc, path
    raise RuntimeError(f"no working video encoder/output combination: {last_error}")


def restore_still_config(cam):
    try:
        cam.stop()
        cam.configure(cam.create_still_configuration())
        if AF_MODE == "continuous":
            cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        else:
            cam.set_controls({"AfMode": controls.AfModeEnum.Auto})
        cam.start()
        applied_still_size["size"] = None
    except Exception as e:
        print(f"WARNING: camera restore to stills failed: {e}")


def video_recording_start(batch_dir, st, auto_stop=None):
    st = normalise_video_settings(st)
    os.makedirs(batch_dir, exist_ok=True)
    with capture_lock:
        with video_lock:
            if video_state["recording"]:
                return {"ok": False, "error": "already recording"}
            recs = []
            try:
                for idx, cam in enumerate(cameras):
                    recs.append(start_cam_recording(
                        cam, os.path.join(batch_dir, f"{PI_ID}_cam{idx}"), st))
            except Exception as e:
                for cam, enc, path in recs:
                    try:
                        cam.stop_encoder()
                    except Exception:
                        pass
                for cam, enc, path in recs:
                    restore_still_config(cam)
                print(f"{PI_ID}: video start failed: {e}")
                return {"ok": False, "error": str(e)}
            video_state.update({
                "recording": True,
                "batch": os.path.basename(batch_dir),
                "started": time.time(),
                "recs": recs,
                "settings": st,
            })
            if st["duration"] > 0:
                timer = threading.Timer(st["duration"], auto_stop or video_recording_stop)
                timer.daemon = True
                timer.start()
                video_state["timer"] = timer
            print(f"{PI_ID}: recording {st['res']}p {st['fps']}fps {st['quality']} "
                  f"to {video_state['batch']} ({len(recs)} camera(s))")
            return {"ok": True, "batch": video_state["batch"], "settings": st, "cameras": len(recs)}


def video_recording_stop():
    with capture_lock:
        with video_lock:
            if not video_state["recording"]:
                return {"ok": False, "error": "not recording"}
            timer = video_state.get("timer")
            if timer is not None:
                timer.cancel()
                video_state["timer"] = None
            files = []
            for cam, enc, path in video_state["recs"]:
                try:
                    cam.stop_encoder()
                    files.append(path)
                except Exception as e:
                    print(f"WARNING: stop encoder failed: {e}")
                restore_still_config(cam)
            duration = time.time() - video_state["started"]
            total = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
            batch = video_state["batch"]
            settings = video_state["settings"]
            video_state.update({"recording": False, "batch": None, "started": 0.0,
                                "recs": [], "settings": None})
            print(f"{PI_ID}: recording stopped after {duration:.1f}s "
                  f"({len(files)} file(s), {total / 1048576:.1f} MB)")
            return {"ok": True, "batch": batch, "files": [os.path.basename(f) for f in files],
                    "duration": duration, "settings": settings}


def video_upload_thumb(path):
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
                tw = 480
                th = max(1, int(h * tw / w))
                cv2.imwrite(path.rsplit(".", 1)[0] + "_thumb.jpg", cv2.resize(frame, (tw, th)))
    except Exception as e:
        print(f"WARNING: video thumb failed: {e}")
    finally:
        if cap is not None:
            cap.release()


def valid_upload_name(fname):
    return bool(re.match(r"^[A-Za-z0-9_-]+\.(jpg|mp4|h264)$", fname or ""))


def grab_frames():
    ensure_still_config()
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
        self.current_video_batch = None
        self.capture_mode = "photo"
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

    def session_start(self, name, kind="image"):
        name = name.strip()
        if not name or not all(c.isalnum() or c in "_- " for c in name):
            return {"ok": False, "error": "invalid session name"}
        with self.lock:
            if self.current_session == name:
                return {"ok": True, "session": name}
            if self.current_session:
                return {"ok": False, "error": f"session '{self.current_session}' already active"}
            s = next((s for s in self.sessions if s["name"] == name), None)
            if s is None:
                s = {"name": name, "kind": kind if kind in ("image", "video") else "image",
                     "created": time.time(), "active": True, "batches": []}
                self.sessions.append(s)
            elif s.get("kind", "image") != kind:
                return {"ok": False, "error": f"session '{name}' is a {s.get('kind', 'image')} session"}
            s["active"] = True
            self.current_session = name
            self._save_sessions()
            return {"ok": True, "session": name, "kind": s.get("kind", "image")}

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

    def session_delete(self, name, files=False):
        with self.lock:
            idx = next((i for i, s in enumerate(self.sessions) if s["name"] == name), None)
            if idx is None:
                return {"ok": False, "error": "unknown session"}
            s = self.sessions.pop(idx)
            if self.current_session == name:
                self.current_session = None
            self._save_sessions()
            removed = []
            if files:
                for b in s.get("batches", []):
                    if not all(c.isalnum() or c == "_" for c in b):
                        continue
                    p = os.path.join(CAPTURES_DIR, b)
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                        removed.append(b)
            return {"ok": True, "removed_batches": removed}

    def session_status(self):
        with self.lock:
            return {"ok": True, "active": self.current_session, "sessions": self.sessions}

    def delete_ungrouped(self, name):
        name = name.strip()
        if not name or not all(c.isalnum() or c == "_" for c in name):
            return {"ok": False, "error": "invalid batch name"}
        with self.lock:
            for s in self.sessions:
                if name in s.get("batches", []):
                    return {"ok": False, "error": f"batch belongs to session '{s['name']}'"}
            with video_lock:
                recording = video_state["recording"]
                active_batch = video_state["batch"]
            if recording and name == active_batch:
                return {"ok": False, "error": "batch is being recorded; stop recording first"}
            p = os.path.join(CAPTURES_DIR, name)
            if not os.path.isdir(p):
                return {"ok": False, "error": "unknown batch"}
            shutil.rmtree(p, ignore_errors=True)
            if self.current_batch and os.path.basename(self.current_batch) == name:
                self.current_batch = None
            return {"ok": True, "removed_batch": name}

    def new_batch(self, kind="image"):
        name = batch_stamp()
        batch = os.path.join(CAPTURES_DIR, name)
        os.makedirs(batch, exist_ok=True)
        if self.current_session:
            for s in self.sessions:
                if s["name"] == self.current_session:
                    if s.get("kind", "image") == kind:
                        s["batches"].append(name)
                        self._save_sessions()
                    break
        return batch

    def delete_ungrouped_all(self, kind="image"):
        with self.lock:
            with video_lock:
                recording = video_state["recording"]
                active_batch = video_state["batch"]
            if recording:
                return {"ok": False, "error": "stop recording before deleting captures"}
            owned = {b for s in self.sessions for b in s.get("batches", [])}
            names = sorted(os.listdir(CAPTURES_DIR)) if os.path.isdir(CAPTURES_DIR) else []
            removed = 0
            freed = 0
            for name in names:
                if name == "sessions.json" or name in owned or name == active_batch:
                    continue
                p = os.path.join(CAPTURES_DIR, name)
                if not os.path.isdir(p):
                    continue
                has_video = any(f.endswith(VIDEO_EXTENSIONS) for f in os.listdir(p))
                if (kind == "video") != has_video:
                    continue
                for f in os.listdir(p):
                    try:
                        freed += os.path.getsize(os.path.join(p, f))
                    except OSError:
                        pass
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
            for field in ("current_batch", "current_video_batch"):
                v = getattr(self, field, None)
                if v and not os.path.isdir(v):
                    setattr(self, field, None)
            return {"ok": True, "kind": kind, "removed": removed, "freed": freed}

    def do_capture(self, remote=False):
        with capture_lock:
            with video_lock:
                if video_state["recording"]:
                    print("Photo capture refused: video recording in progress")
                    return {"ok": False, "error": "video recording in progress"}
            with self.lock:
                self.current_batch = self.new_batch()
                batch = self.current_batch
            frames = grab_frames()
            for idx, frame in enumerate(frames):
                write_frame(os.path.join(batch, f"{PI_ID}_cam{idx}.jpg"), frame)
            print(f"Captured {len(frames)} local frame(s) in {os.path.basename(batch)}")
            result = {"ok": True, "batch": os.path.basename(batch)}
        if remote:
            self.fire_secondaries(os.path.basename(batch))
        self._attach_still_settings(os.path.basename(batch), still_state["size"])
        return result

    def fire_secondaries(self, batch):
        threads = []
        for pi_id, (ip, port) in list(self.secondaries.items()):
            t = threading.Thread(target=self._trigger, args=(pi_id, ip, port, batch), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=3)

    def _trigger(self, pi_id, ip, port, batch):
        size = still_state["size"]
        qs = f"batch={batch}" + (f"&size={size[0]}x{size[1]}" if size else "")
        url = f"http://{ip}:{port}/capture?{qs}"
        try:
            urllib.request.urlopen(url, timeout=3)
        except Exception as e:
            print(f"WARNING: secondary {pi_id} trigger failed: {e}")

    def fire_secondaries_video(self, batch, st):
        qs = urllib.parse.urlencode({
            "batch": batch, "res": st["res"], "fps": st["fps"],
            "quality": st["quality"], "dur": st["duration"],
        })
        print(f"{PI_ID}: video start fanout to {len(self.secondaries)} secondaries (batch {batch})")
        threads = []
        for pi_id, (ip, port) in list(self.secondaries.items()):
            url = f"http://{ip}:{port}/video/start?{qs}"
            t = threading.Thread(
                target=self._fanout_get, args=(pi_id, url, 5), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=6)

    def fire_secondaries_video_stop(self):
        threads = []
        for pi_id, (ip, port) in list(self.secondaries.items()):
            url = f"http://{ip}:{port}/video/stop"
            t = threading.Thread(
                target=self._fanout_get, args=(pi_id, url, 5), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=6)

    def _fanout_get(self, pi_id, url, timeout):
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
            code = getattr(resp, "status", 200)
            try:
                body = resp.read().decode()
            except Exception:
                body = ""
            print(f"{PI_ID}: fanout {pi_id} {urllib.parse.urlparse(url).path} -> {code} {body[:120]}")
        except Exception as e:
            print(f"WARNING: secondary {pi_id} video call failed: {e}")

    def _attach_video_settings(self, batch_name, st):
        with self.lock:
            for s in self.sessions:
                if batch_name in s.get("batches", []):
                    s["video_settings"] = st
                    self._save_sessions()
                    break

    def _attach_still_settings(self, batch_name, size):
        with self.lock:
            for s in self.sessions:
                if batch_name in s.get("batches", []):
                    s["still_size"] = list(size) if size else None
                    self._save_sessions()
                    break

    def video_start(self, remote=False, st=None):
        st = normalise_video_settings(st or {})
        with video_lock:
            if video_state["recording"]:
                return {"ok": False, "error": "already recording"}
        batch = self.new_batch("video")
        self.current_video_batch = batch
        res = video_recording_start(
            batch, st, auto_stop=lambda: primary.video_stop(remote=True))
        if not res.get("ok"):
            with self.lock:
                try:
                    if not os.listdir(batch):
                        os.rmdir(batch)
                except OSError:
                    pass
            return res
        self._attach_video_settings(os.path.basename(batch), st)
        if remote:
            self.fire_secondaries_video(os.path.basename(batch), st)
        return res

    def video_stop(self, remote=False):
        res = video_recording_stop()
        if res.get("ok") and remote:
            self.fire_secondaries_video_stop()
        return res

    def handle_upload(self, pi_id, cam_idx, data, batch=None, kind="image", fname=None):
        ext = ".mp4" if kind == "video" else ".jpg"
        with self.lock:
            if batch:
                batch_dir = os.path.join(CAPTURES_DIR, batch)
                os.makedirs(batch_dir, exist_ok=True)
            elif self.current_batch is None:
                self.current_batch = self.new_batch()
                print("No active batch; created from secondary upload")
            if fname and valid_upload_name(fname):
                name = fname
            else:
                name = f"{pi_id}_cam{cam_idx}{ext}"
            path = os.path.join(batch_dir if batch else self.current_batch, name)
        with open(path, "wb") as f:
            f.write(data)
        if kind == "video":
            video_upload_thumb(path)
        else:
            frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                h, w = frame.shape[:2]
                tw = 480
                th = max(1, int(h * tw / w))
                cv2.imwrite(path[:-4] + "_thumb.jpg", cv2.resize(frame, (tw, th)))
        print(f"Saved {name}")

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


def upload_capture_file(local_dir, fname):
    path = os.path.join(local_dir, fname)
    if os.path.exists(path + ".ok"):
        return True
    with open(path, "rb") as f:
        data = f.read()
    stem = fname.rsplit(".", 1)[0]
    pi_id, cam_idx = PI_ID, 0
    if "_cam" in stem:
        pi, _, idx = stem.rpartition("_cam")
        if pi and idx.isdigit():
            pi_id, cam_idx = pi, int(idx)
    kind = "video" if fname.endswith(VIDEO_EXTENSIONS) else "image"
    qs = urllib.parse.urlencode({
        "pi_id": pi_id, "cam_idx": cam_idx, "batch": os.path.basename(local_dir),
        "kind": kind, "fname": fname,
    })
    url = f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/upload?{qs}"
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "video/mp4" if kind == "video" else "image/jpeg"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=300)
        with open(path + ".ok", "w") as f:
            f.write("ok")
        return True
    except Exception as e:
        print(f"WARNING: upload {fname} failed: {e}")
        return False


def secondary_video_start(batch, st):
    def work():
        print(f"{PI_ID}: work thread start batch={batch} st={st}")
        res = video_recording_start(
            os.path.join(CAPTURES_DIR, batch), st, auto_stop=secondary_video_stop)
        if not res.get("ok"):
            print(f"{PI_ID}: video start failed: {res.get('error')}")
    threading.Thread(target=work, daemon=True).start()


def secondary_video_stop():
    def work():
        res = video_recording_stop()
        if not res.get("ok"):
            if res.get("error") != "not recording":
                print(f"{PI_ID}: video stop failed: {res.get('error')}")
            return
        batch = res.get("batch")
        local_dir = os.path.join(CAPTURES_DIR, batch)
        for fname in res.get("files", []):
            if upload_capture_file(local_dir, fname):
                print(f"{PI_ID}: uploaded {batch}/{fname}")
        print(f"{PI_ID}: video {batch} stopped and saved ({res.get('duration', 0):.1f}s)")
    threading.Thread(target=work, daemon=True).start()


def sync_push():
    pushed = 0
    failed = 0
    if os.path.isdir(CAPTURES_DIR):
        for name in sorted(os.listdir(CAPTURES_DIR)):
            batch_dir = os.path.join(CAPTURES_DIR, name)
            if not os.path.isdir(batch_dir):
                continue
            for fname in sorted(os.listdir(batch_dir)):
                if fname.endswith("_thumb.jpg"):
                    continue
                if not fname.endswith(".jpg") and not fname.endswith(VIDEO_EXTENSIONS):
                    continue
                if upload_capture_file(batch_dir, fname):
                    pushed += 1
                else:
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
                    skind = None
                    if primary.current_session:
                        skind = next((s.get("kind", "image") for s in primary.sessions
                                      if s["name"] == primary.current_session), None)
                with video_lock:
                    video = {
                        "recording": video_state["recording"],
                        "batch": video_state["batch"],
                        "started": video_state["started"],
                        "settings": video_state["settings"],
                    }
                self._send_json(200, {
                    "pi_id": PI_ID,
                    "mode": "primary",
                    "capture_mode": primary.capture_mode,
                    "cameras": len(cameras),
                    "caps": caps_payload(),
                    "still_size": list(still_state["size"]) if still_state["size"] else "full",
                    "session": primary.current_session,
                    "session_kind": skind,
                    "video": video,
                    "secondaries": secs,
                })
            else:
                self._send_json(404, {"error": "not found"})
            return
        if path == "/capture":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            batch = qs.get("batch", [None])[0]
            size = normalise_still_size(qs.get("size", [None])[0])
            still_state["size"] = size
            threading.Thread(target=secondary_capture, args=(batch,), daemon=True).start()
            self._send_json(200, {"ok": True})
        elif path == "/video/start":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            batch = (qs.get("batch", [None])[0] or "").strip()
            print(f"{PI_ID}: /video/start received batch={batch} res={qs.get('res')}")
            if not batch or not all(c.isalnum() or c == "_" for c in batch):
                self._send_json(400, {"error": "invalid batch"})
                return
            secondary_video_start(batch, video_settings_from_qs(qs))
            self._send_json(200, {"ok": True})
        elif path == "/video/stop":
            secondary_video_stop()
            self._send_json(200, {"ok": True})
        elif path == "/status":
            with video_lock:
                recording = video_state["recording"]
            self._send_json(200, {
                "pi_id": PI_ID,
                "cameras": len(cameras),
                "caps": caps_payload(),
                "still_size": list(still_state["size"]) if still_state["size"] else "full",
                "recording": recording,
            })
        elif path == "/sync":
            self._send_json(200, sync_push())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if PRIMARY:
            if parsed.path == "/register":
                pi_id = qs.get("pi_id", [""])[0]
                if not pi_id:
                    self._send_json(400, {"error": "missing pi_id"})
                    return
                port = int(qs.get("port", [str(SECONDARY_PORT)])[0])
                ip = self.client_address[0]
                with primary.lock:
                    known = pi_id in primary.secondaries and \
                        primary.secondaries[pi_id] == (ip, port)
                    full = len(primary.secondaries) >= MAX_SECONDARIES and not known
                if full:
                    self._send_json(429, {"error": "too many secondaries"})
                    return
                with primary.lock:
                    primary.secondaries[pi_id] = (ip, port)
                if not known:
                    print(f"Registered secondary {pi_id} at {ip}:{port}")
                self._send_json(200, {"ok": True, "secondaries": len(primary.secondaries)})
            elif parsed.path == "/upload":
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length) if length else b""
                pi_id = qs.get("pi_id", ["?"])[0]
                try:
                    cam_idx = int(qs.get("cam_idx", ["0"])[0])
                except ValueError:
                    cam_idx = 0
                batch = qs.get("batch", [None])[0]
                kind = qs.get("kind", ["image"])[0]
                if kind not in ("image", "video"):
                    kind = "image"
                fname = qs.get("fname", [None])[0]
                if fname and not valid_upload_name(fname):
                    self._send_json(400, {"error": "invalid filename"})
                    return
                if batch and not all(c.isalnum() or c == "_" for c in batch):
                    self._send_json(400, {"error": "invalid batch"})
                    return
                primary.handle_upload(pi_id, cam_idx, data, batch, kind, fname)
                self._send_json(200, {"ok": True})
            elif parsed.path == "/remote-capture":
                body = primary.do_capture(remote=True)
                response = {"ok": body.get("ok", False)}
                if body.get("error"):
                    response["error"] = body["error"]
                if body.get("batch"):
                    response["batch"] = body["batch"]
                self._send_json(200, response)
            elif parsed.path == "/video/start":
                print(f"video/start POST from {self.client_address[0]}")
                st = video_settings_from_qs(qs)
                body = primary.video_start(remote=True, st=st)
                self._send_json(200, body)
            elif parsed.path == "/video/stop":
                self._send_json(200, primary.video_stop(remote=True))
            elif parsed.path == "/mode":
                mode = qs.get("mode", ["photo"])[0].strip().lower()
                if mode not in ("photo", "video"):
                    self._send_json(400, {"error": "invalid mode"})
                    return
                with primary.lock:
                    primary.capture_mode = mode
                self._send_json(200, {"ok": True, "capture_mode": mode})
            elif parsed.path == "/session/start":
                name = qs.get("name", [""])[0]
                kind = qs.get("kind", ["image"])[0]
                if kind not in ("image", "video"):
                    kind = "image"
                self._send_json(200, primary.session_start(name, kind))
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
            elif parsed.path == "/session/delete":
                name = qs.get("name", [""])[0]
                files = qs.get("files", ["0"])[0] in ("1", "true")
                self._send_json(200, primary.session_delete(name, files))
            elif parsed.path == "/captures/delete":
                name = qs.get("batch", [""])[0]
                self._send_json(200, primary.delete_ungrouped(name))
            elif parsed.path == "/captures/delete-all":
                kind = qs.get("kind", ["image"])[0]
                if kind not in ("image", "video"):
                    kind = "image"
                self._send_json(200, primary.delete_ungrouped_all(kind))
            elif parsed.path == "/still-size":
                size = normalise_still_size(qs.get("size", [None])[0])
                still_state["size"] = size
                self._send_json(200, {"ok": True,
                                      "still_size": list(size) if size else "full"})
            else:
                self._send_json(404, {"error": "not found"})
        else:
            if parsed.path == "/purge":
                self._send_json(200, purge_local())
            else:
                self._send_json(404, {"error": "not found"})


def register_loop():
    qs = urllib.parse.urlencode({"pi_id": PI_ID, "port": SECONDARY_PORT})
    url = f"http://{PRIMARY_HOST}:{PRIMARY_PORT}/register?{qs}"
    while True:
        try:
            urllib.request.urlopen(url, data=b"", timeout=5)
            print(f"{PI_ID}: registered with {PRIMARY_HOST}:{PRIMARY_PORT}")
        except Exception as e:
            print(f"WARNING: registration with primary failed: {e}")
        time.sleep(30)


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
        threading.Thread(target=register_loop, daemon=True).start()
        print(f"Secondary mode ({PI_ID}). HTTP on :{SECONDARY_PORT}")

    gpio_debounce_until = 0
    while True:
        if gpio_request is not None and time.time() > gpio_debounce_until:
            if gpio_request.get_value(GPIO_TRIGGER_PIN) == GPIO_ACTIVE:
                gpio_debounce_until = time.time() + 0.5
                print("GPIO trigger pressed")
                if PRIMARY:
                    if primary.capture_mode == "video":
                        with video_lock:
                            recording = video_state["recording"]
                        if recording:
                            primary.video_stop(remote=True)
                        else:
                            primary.video_start(remote=True)
                    else:
                        primary.do_capture(remote=False)
                else:
                    secondary_capture()
        if not PRIMARY:
            time.sleep(0.05)
            continue
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
            elif key == "v":
                with video_lock:
                    recording = video_state["recording"]
                if recording:
                    primary.video_stop(remote=True)
                else:
                    primary.video_start(remote=True)
            elif key == "x":
                primary.video_stop(remote=True)
            elif key == "c":
                primary.do_capture(remote=True)

    if gpio_request is not None:
        gpio_request.release()
    for cam in cameras:
        cam.stop()
    server.server_close()


if __name__ == "__main__":
    main()
