# AGENTS.md

Multi-Pi synchronized capture on Raspberry Pi 5. Two scripts, all stdlib (no new deps):

- `cam_server.py` — capture daemon, one instance per Pi. Role set by `MODE` (`"p"`/`"s"`).
- `web_gallery.py` — HTTP gallery + remote "Capture All" trigger. Run on the primary only.

## Hardware-only, no dev loop

- Pi 5 only: `picamera2`, `libcamera`, `gpiod` are Pi-only. Nothing can be imported, linted, or tested on a normal dev machine — do not try to run it or add CI/tests.
- Validate by inspection only; `python3 -m py_compile cam_server.py web_gallery.py` works for syntax.
- `gpiod` import is guarded (`try/except ImportError`) and degrades gracefully — keep it that way.

## Architecture

Shared **active-low** button wired to every Pi's `GPIO_TRIGGER_PIN` (pull-up set in software, no external resistor). A press makes all Pis capture simultaneously via hardware — the network is only used *after* capture for secondaries to push frames to the primary.

- **Primary** (`MODE="p"`, one Pi): HTTP server on `PRIMARY_PORT` — `POST /register` (secondaries announce themselves), `POST /upload` (frames routed to current batch folder), `POST /remote-capture` (software trigger, also fires all registered secondaries). Keyboard `c`/`q` on stdin.
- **Secondary** (`MODE="s"`, up to `MAX_SECONDARIES`=3): registers with `PRIMARY_HOST` at startup, HTTP server on `SECONDARY_PORT` — `GET /capture` (web-trigger path), `GET /status`. Headless.

Two trigger paths: **GPIO** (all Pis fire at once) and **web gallery "Capture All"** → primary `/remote-capture` → `GET /capture` on every registered secondary. The primary batches uploads into `captures/<YYYYMMDD_HHMMSS>_<hash>/<pi_id>_cam<idx>.jpg`; an upload arriving with no open batch auto-creates one.

Folder hash = first 4 hex chars of `sha256("<YYYYMMDD_HHMMSS>_<floor(time*5)>")` — the 200ms time bucket makes the name unique within a second (0.5s debounce guarantees different buckets) while staying identical across Pis (all detect the press within one bucket). Keep `new_batch()` as the single source of folder naming.

## Run

```bash
python3 cam_server.py        # edit MODE / PI_ID / PRIMARY_HOST per Pi first
python3 web_gallery.py       # primary only; http://<primary-ip>:8088
```

No display needed on any Pi. Foreground process — use tmux/systemd to background it. Alternatively `./launch_primary.sh` (cam_server + web_gallery) and `./launch_secondary.sh` (cam_server) — start/stop/status/restart, toggle on bare invocation; PID files + logs in `.run/` (gitignored).

## Config knobs (`cam_server.py`)

- `MODE`: `"p"` or `"s"` (`"primary"`/`"secondary"` also accepted).
- `PI_ID`: unique per Pi; embedded in every filename.
- `PRIMARY_HOST`: IP of primary (secondary only).
- `PRIMARY_PORT` / `SECONDARY_PORT`: HTTP ports (primary / secondary servers).
- `MAX_SECONDARIES`: cap on `POST /register`, default 3.
- `AF_MODE`: `"continuous"` vs `"interval"` (manual AF trigger every 5s via private `_last_af_trigger` attr).
- `GPIO_TRIGGER_PIN`: BCM pin, `-1` disables; active-low, 0.5s software debounce.

`web_gallery.py` knobs: `PORT`, `CAPTURES_DIR`, `PRIMARY_HOST`/`PRIMARY_PORT` (proxy target for the capture button; defaults to `127.0.0.1:8080`).

## Conventions

- Concurrency is `threading` + `http.server` (`ThreadingHTTPServer`). Camera access is serialized by module-level `capture_lock` — keep new endpoints capturing via `grab_frames()` under that lock, never concurrently.
- Camera count is `range(2)`; cameras that fail to open are skipped. GPIO, registration, and network failures only warn. Preserve all defensive degradation.
- Frame pipeline: `capture_array()` → flip → `cv2.cvtColor(..., RGB2BGR)` → save / `imencode` to JPEG.
- No comments in code, no tests, no external instruction files.
