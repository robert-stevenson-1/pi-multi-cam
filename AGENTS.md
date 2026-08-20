# AGENTS.md

Multi-Pi synchronized capture on Raspberry Pi 5. Two scripts, all stdlib (no new Python deps):

- `cam_server.py` — capture daemon, one instance per Pi. Role set by `MODE` (`"p"`/`"s"`).
- `web_gallery.py` — HTTP gallery + remote controls, run on the primary only. Serves vendored static assets from `static/` (PhotoSwipe, committed to the repo — keep them vendored and offline, no CDNs, since the primary will be a hotspot).

## Hardware-only, no dev loop

- Pi 5 only: `picamera2`, `libcamera`, `gpiod` are Pi-only. Nothing can be imported, linted, or tested on a normal dev machine — do not try to run it or add CI/tests.
- Validate by inspection only; `python3 -m py_compile cam_server.py web_gallery.py` works for syntax.
- `gpiod` import is guarded (`try/except ImportError`) and degrades gracefully — keep it that way.

## Architecture

Shared **active-low** button wired to every Pi's `GPIO_TRIGGER_PIN` (pull-up set in software, no external resistor). A press makes all Pis capture simultaneously via hardware — the network is only used *after* capture for secondaries to push frames to the primary.

- **Primary** (`MODE="p"`, one Pi): HTTP server on `PRIMARY_PORT` — `GET /status` (pi_id, camera count, active session, registered secondaries), `POST /register` (secondaries announce themselves), `POST /upload` (frames routed by `batch` param or current batch folder), `POST /remote-capture` (software trigger, passes its batch name to secondaries), `POST /sync` (fans out to secondaries, backfills missing frames), `POST /purge-secondaries` (remote wipe of secondaries' local captures), `POST /session/start|stop` (active capture session; batches register into `sessions.json`). Keyboard `c`/`q` on stdin.
- **Secondary** (`MODE="s"`, up to `MAX_SECONDARIES`=3): registers with `PRIMARY_HOST` at startup, HTTP server on `SECONDARY_PORT` — `GET /capture` (web-trigger path, optional `?batch=`), `GET /status`, `GET /sync` (pushes every local capture without a `.ok` marker), `POST /purge` (deletes local captures, returns count). Headless. **Store-and-forward:** every capture is saved locally under the same batch naming scheme, uploaded, then marked `<file>.ok` on success — so offline periods are recovered by a later sync.

Two trigger paths: **GPIO** (all Pis fire at once) and **web gallery "Capture All"** → primary `/remote-capture` → `GET /capture?batch=<name>` on every registered secondary. The primary batches uploads into `captures/<YYYYMMDD_HHMMSS>_<hash>/<pi_id>_cam<idx>.jpg`; uploads carry a `batch` name (secondary-local, computed from the same scheme) or fall back to the current batch.

Folder hash = first 4 hex chars of `sha256("<YYYYMMDD_HHMMSS>_<floor(time*5)>")` — the 200ms time bucket makes the name unique within a second (0.5s debounce guarantees different buckets) while staying identical across Pis (all detect the press within one bucket). Keep `batch_stamp()` (in `cam_server.py`) as the single source of batch naming; `new_batch()` wraps it for the primary and registers the batch into the active session.

## Run

```bash
python3 cam_server.py        # copy .env.example to .env and edit per Pi first
python3 web_gallery.py       # primary only; http://<primary-ip>:8088
```

No display needed on any Pi. Foreground process — use tmux/systemd to background it. Alternatively `./launch_primary.sh` (cam_server + web_gallery) and `./launch_secondary.sh` (cam_server) — start/stop/status/restart, toggle on bare invocation; PID files + logs in `.run/` (gitignored).

## Config (`.env`, gitignored)

Config is read from `.env` (loaded by both scripts at startup, fallback to in-code defaults; real env vars take precedence). Copy `.env.example` to `.env` on each Pi and edit per device — this avoids merge conflicts from per-Pi edits.

- `MODE`: `"p"` or `"s"` (`"primary"`/`"secondary"` also accepted).
- `PI_ID`: unique per Pi; embedded in every filename.
- `PRIMARY_HOST`: IP of primary. On the primary set it to its own IP/127.0.0.1 — it also drives web_gallery's proxy target; on secondaries it's the primary's IP.
- `PRIMARY_PORT` / `SECONDARY_PORT`: HTTP ports (primary / secondary servers). `PORT`: web_gallery port.
- `MAX_SECONDARIES`: cap on `POST /register`, default 3.
- `AF_MODE`: `"continuous"` vs `"interval"` (manual AF trigger every 5s via private `_last_af_trigger` attr).
- `GPIO_TRIGGER_PIN`: BCM pin, `-1` disables; active-low, 0.5s software debounce.
- `CAPTURES_DIR`: output directory.

`web_gallery.py` knobs: `PORT`, `CAPTURES_DIR`, `PRIMARY_HOST`/`PRIMARY_PORT` (proxy target for the capture button; defaults to `127.0.0.1:8080`). `cam_preview.py` uses its own `PREVIEW_PORT` (default 9090) so it never collides with `web_gallery`'s `PORT`. The gallery header shows a live status line (`GET /server-status`: primary `/status` proxy + direct pings to each registered secondary's `:SECONDARY_PORT/status`) — red `cam_server DOWN` when the primary backend is unreachable.

## Conventions

- Concurrency is `threading` + `http.server` (`ThreadingHTTPServer`). Camera access is serialized by module-level `capture_lock` — keep new endpoints capturing via `grab_frames()` under that lock, never concurrently.
- Camera count is `range(2)`; cameras that fail to open are skipped. GPIO, registration, and network failures only warn. Preserve all defensive degradation.
- Frame pipeline: `capture_array()` → flip → `cv2.cvtColor(..., RGB2BGR)` → save / `imencode` to JPEG. Every full-res frame the primary writes gets a `_thumb.jpg` (~480px) beside it (`write_frame()` for local, imdecode in `handle_upload` for uploaded); the gallery never shows `_thumb.jpg` as a real image.
- Sessions live in `captures/sessions.json` (primary only) and never move files — batches are appended by name, the layout stays flat. Session registration happens in `new_batch()` only; `handle_upload`'s explicit-`batch` path does not register (so sync backfills stay ungrouped). Gallery groups by the manifest and downloads batch/session ZIPs via `/zip/<batch>` and `/zip/session/<name>`.
- Secondaries store captures locally and write `<file>.ok` after a successful upload; `GET /sync` pushes everything missing a marker. Sync markers are plain files next to the image.
- `web_gallery.py` is pure stdlib (runs on any machine); `cam_server.py` imports the Pi-only camera stack. Keep `web_gallery.py` importable/testable off-device — the gallery was verified with a local run + browser.
- No comments in code, no tests, no external instruction files.
