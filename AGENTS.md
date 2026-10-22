# AGENTS.md

Multi-Pi synchronized capture on Raspberry Pi 5. Core services use the standard library apart from the Raspberry Pi camera stack (`pispot.py` additionally needs Textual, pip-installed):

- `cam_server.py` — capture daemon, one instance per Pi. Role set by `MODE` (`"p"`/`"s"`).
- `web_gallery.py` — HTTP gallery + remote controls, run on the primary only. Serves vendored static assets from `static/` (PhotoSwipe, committed to the repo — keep them vendored and offline, no CDNs, since the primary will be a hotspot).
- `cam_preview.py` — standalone browser camera diagnostic; do not run it alongside `cam_server.py` on the same Pi.
- `pispot.py` — standalone hotspot TUI (NetworkManager config + client monitor), run under `sudo`. Needs current Textual from pip; apt's `python3-textual` (0.1.13) is too old and must not be installed. Every control is duplicated as an on-screen button (restart/toggle/refresh/quit) because terminals swallow keybindings while inputs hold focus. `apply()` pins the Hotspot profile below the uplink (`ipv4.never-default`, route-metric 700, dns-priority 200), re-pins `connection.interface-name`, and writes `/etc/sysctl.d/99-pispot.conf` (ip_forward) so sharing works over wlan0/eth0. The *Start on boot* switch manages a delayed (10 s) systemd unit (`pispot-hotspot.service`) and forces `connection.autoconnect no` so the unit is the only boot path. Requires the Wi-Fi country code to be set on the host.

## Hardware-only, no dev loop

- Pi 5 only: `picamera2`, `libcamera`, `gpiod` are Pi-only. Nothing can be imported, linted, or tested on a normal dev machine — do not try to run it or add CI/tests.
- Validate by inspection only; `python3 -m py_compile cam_server.py cam_preview.py web_gallery.py pispot.py` works for syntax.
- `gpiod` import and setup are guarded and degrade gracefully — keep any GPIO failure from stopping camera service startup.

## Architecture

Shared **active-low** button wired to every Pi's `GPIO_TRIGGER_PIN` (pull-up set in software, no external resistor). A press makes all Pis capture simultaneously via hardware — the network is only used *after* capture for secondaries to push frames to the primary.

- **Primary** (`MODE="p"`, one Pi): HTTP server on `PRIMARY_PORT` — `GET /status` (pi_id, camera count, capture mode, active session + kind, video state, registered secondaries), `POST /register` (secondaries announce themselves), `POST /upload` (frames/videos routed by `batch` param; `kind=video` + `fname` for video files), `POST /remote-capture` (software trigger, passes its batch name to secondaries), `POST /video/start|/video/stop` (synchronized recording, `?res=&fps=&quality=&dur=`), `POST /mode` (photo/video capture mode), `POST /sync` (fans out to secondaries, backfills missing frames), `POST /purge-secondaries` (remote wipe of secondaries' local captures), `POST /session/start|stop` (`kind=image|video` param; active capture session; batches register into `sessions.json`), `POST /captures/delete` (deletes an ungrouped batch folder — refuses batches owned by a session). Keyboard `c`/`q` on stdin, plus `v` (record start/stop when mode is video) and `x` (force stop recording).
- **Secondary** (`MODE="s"`, up to `MAX_SECONDARIES`=3): registers with `PRIMARY_HOST` at startup, HTTP server on `SECONDARY_PORT` — `GET /capture` (web-trigger path, optional `?batch=`), `GET /video/start?batch=…&res=…` / `GET /video/stop` (recording in a background thread; stop fans uploads back to the primary), `GET /status`, `GET /sync` (pushes every local capture without a `.ok` marker, images and videos), `POST /purge` (deletes local captures, returns count). Registration is one-shot, so restart secondaries after restarting the primary. Headless. **Store-and-forward:** every capture is saved locally under the same batch naming scheme, uploaded, then marked `<file>.ok` on success — so offline periods are recovered by a later sync.

Two trigger paths: **GPIO** (all Pis fire at once) and **web gallery "Capture All"** → primary `/remote-capture` → `GET /capture?batch=<name>` on every registered secondary. Video is web-triggered: gallery "Start Recording" → primary `/video/start` (fan-out `GET /video/start?…` to secondaries) and "Stop Recording" → `/video/stop`; a recording's files are pushed at stop time (or by any later sync). The primary batches uploads into `captures/<YYYYMMDD_HHMMSS>_<hash>/<pi_id>_cam<idx>.jpg|mp4`; uploads carry a `batch` name (secondary-local, computed from the same scheme) or fall back to the current batch. Photo capture is refused while a video recording is in progress (cameras can't be reconfigured mid-encode).

Folder hash = first 4 hex chars of `sha256("<YYYYMMDD_HHMMSS>_<floor(time*5)>")` — the 200ms time bucket makes the name unique within a second (0.5s debounce guarantees different buckets) while staying identical across Pis (all detect the press within one bucket). Keep `batch_stamp()` (in `cam_server.py`) as the single source of batch naming; `new_batch()` wraps it for the primary and registers the batch into the active session (only when the session kind matches the batch kind).

## Video mode

Video recording is a separate capture kind from photos: sessions in `sessions.json` carry `kind` (`image` default/`video`), a session refuses to mix kinds, and only a *video* session accumulates video batches (`new_batch("video")` skips kind-mismatched sessions, so mismatching recordings stay ungrouped). Recording settings are per start (resolution 1080/720, fps 15/30, quality low/medium/high → 4/10/20 Mbps, max duration where `0` = stop manually, else auto-stop timers on every device); the gallery stores them on the active video session and prefills the form.

Picamera2 pipeline: on record start each camera is reconfigured to a lean `create_video_configuration` (4 buffers, no lores/raw — Pi 5/2GB budget, no preview anywhere), flipped via `Transform(hflip=1, vflip=1)` to match stills, then encoder/output pairs are tried defensively until one works (`H264Encoder` → `LibavH264Encoder` × `FfmpegOutput` (.mp4) → `PyavOutput` (.mp4) → `FileOutput` (.h264)). On stop, encoders finalize, cameras are restored to still configuration, and secondaries push their `<PI_ID>_cam<idx>.mp4` files. `video_recording_start/stop()` serialize through `capture_lock`; `video_lock` + `video_state` hold the shared recording state. The cam_server generates a `_thumb.jpg` from frame 0 of each uploaded/local video. GPIO on the primary in video mode toggles record start/stop (with gallery defaults).

## Run

```bash
python3 cam_server.py        # copy .env.example to .env and edit per Pi first
python3 web_gallery.py       # primary only; http://<primary-ip>:8088
```

No display needed on any Pi. Foreground process — use tmux/systemd to background it. Alternatively `./launch_primary.sh` (cam_server + web_gallery) and `./launch_secondary.sh` (cam_server) — start/stop/status/restart, toggle on bare invocation; process state and logs are in `.run/` (gitignored). `status` shows the PID and last three log lines; `tail -f .run/*.log` follows the live output.

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
- `VID_RES` / `VID_FPS` / `VID_QUALITY` / `VID_DURATION`: video recording defaults used when no settings arrive (gallery sends them per start).

`web_gallery.py` knobs: `PORT`, `CAPTURES_DIR`, `PRIMARY_HOST`/`PRIMARY_PORT` (proxy target for controls; defaults to `127.0.0.1:8080`), `CAPTURE_REFRESH_MS` (delay before the gallery auto-reloads after "Capture All", default 1000). `GET /server-status` combines the primary `/status` response with direct pings to each registered secondary's `/status`; the gallery header polls it every five seconds and shows a red `Primary Down` pill when the primary backend is unreachable. The gallery serves two pages — `/` (Photos) and `/video` (Video) — switched by mode tabs in the header that also `POST /mode` so the primary's GPIO/keyboard behavior follows the page being viewed; each page renders only its kind's sessions/batches (server-side classification by content: a folder containing `.mp4`/`.h264` files is a video batch). `VIDEO_EXTENSIONS`/`video_cards_html`/`video_html`/`base_page`/`PAGE_JS` (shared JS as a plain-string constant with `__REFRESH_MS__`/`__PAGE_MODE__` tokens) in `web_gallery.py`. `cam_preview.py` uses its own `PREVIEW_PORT` (default `9090`) and `SIZE` (default `640x480`).

## Conventions

- Concurrency is `threading` + `http.server` (`ThreadingHTTPServer`). Camera access is serialized by module-level `capture_lock` — keep new endpoints capturing via `grab_frames()` under that lock, never concurrently.
- Camera count is `range(2)`; cameras that fail to open are skipped. GPIO, registration, and network failures only warn. Preserve all defensive degradation.
- Frame pipeline: `capture_array()` → flip → `cv2.cvtColor(..., RGB2BGR)` → save / `imencode` to JPEG. Every full-res frame the primary writes gets a `_thumb.jpg` (~480px) beside it (`write_frame()` for local, imdecode in `handle_upload` for uploaded); the gallery never shows `_thumb.jpg` as a real image.
- Sessions live in `captures/sessions.json` (primary only) and never move files — batches are appended by name, the layout stays flat. Session registration happens in `new_batch()` only; `handle_upload`'s explicit-`batch` path does not register (so sync backfills stay ungrouped). Gallery groups by the manifest and downloads batch/session ZIPs via `/zip/<batch>`, `/zip/session/<name>`, and `/zip/multi?sessions=<csv>&batches=<csv>` (multi-select combined ZIP; arcnames `sessions/<name>/…` / `batches/…`). `POST /session/start` with an existing name resumes it via the gallery's per-session Continue button; starting another session while one is active is refused.
- Gallery sorting is client-side DOM reordering driven by three dropdowns in the subbar (sessions: newest/oldest/name — newest/oldest compare `data-session-created` from `sessions.json`'s `created` or a parsed earliest-batch fallback; batches within sessions: newest/oldest; photos within batches: filename or device cam ID via natural sort). Preferences persist in `localStorage` key `mcGallerySort`; `applySorts()` must run after every `refreshGallery()` and precede `initLightbox()`. Ungrouped ranks by its newest batch timestamp.
- Ungrouped batches get a server-routed Delete (`POST /captures/delete?batch=` proxied to cam_server; session-owned batches are refused there), using the same two-step confirm pattern as session delete. Selection mode (`Select` in the subbar) shows per-session checkboxes (including Ungrouped) and one combined download; selection state is transient and reset by gallery refreshes.
- Mobile (≤640px) layout: sticky bottom bar with a large Capture All; Sync/Purge live in its ⋮ overflow menu; header action group is hidden; grid is forced to two columns with icon-only Save buttons. `triggerCapture(btn)`/`triggerSync(btn)` take an optional button argument for the spinner state, so pass `this` from any button that calls them. A global `[hidden] { display: none !important; }` rule exists because class `display` values otherwise override the `hidden` attribute — rely on it for show/hide toggling.
- Secondaries store captures locally and write `<file>.ok` after a successful upload; `GET /sync` pushes everything missing a marker. Sync markers are plain files next to the image.
- `web_gallery.py` is pure stdlib (runs on any machine); `cam_server.py` imports the Pi-only camera stack. Keep `web_gallery.py` importable/testable off-device — the gallery was verified with a local run + browser.
- No comments in code, no tests, no external instruction files.
- Work with incremental git commits: one commit per logical change, made as each unit is complete and verified, with short imperative messages. Never commit unrelated churn or generated artifacts alongside it.
