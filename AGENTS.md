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

## Remote dev on the PIPS Pis (optional)

On the PIPS network the Pis are reachable over SSH, and they are the only place this code can actually run. Probe before every remote session:

```bash
getent hosts pi-cam-0.local
ssh -o BatchMode=yes -o ConnectTimeout=3 pi@pi-cam-0.local true
```

If either fails, stay local and say so — no retry loops, no interactive password prompts. Always address the Pis by their full `.local` name (`pi-cam-2` does not reliably resolve via mDNS); `~/.ssh/config` maps `pi-cam-*.local` to user `pi` with the passphrase-less `~/.ssh/id_ed25519_pips`, whose public key must be in `/home/pi/.ssh/authorized_keys` on each Pi.

- Hosts: `pi-cam-0.local` is the primary (`PI_ID=pi0`), `pi-cam-1.local` … `pi-cam-3.local` the secondaries. Repo is `/home/pi/pi-multi-cam`, logs in `.run/`, systemd units `picam_primary.service` / `picam_secondary.service`.
- Deploy: commit and push here, then `ssh pi@<host> 'cd /home/pi/pi-multi-cam && git pull --ff-only'`, `ssh pi@<host> 'cd /home/pi/pi-multi-cam && python3 -m py_compile cam_server.py cam_preview.py web_gallery.py pispot.py'`, then `./launch_primary.sh restart` or `./launch_secondary.sh restart`; check with `./launch_*.sh status` and `tail .run/*.log`. Restart secondaries after the primary.
- No sudo: never run `sudo`, `systemctl`, `apt`, or edit unit files on a Pi; ask the user for privileged actions. Restart through the launch scripts only.
- Inspect before pulling: a dirty Pi checkout makes `--ff-only` fail — report it, never reset or stash on the device. `git pull` needs outbound HTTPS; if `ssh pi@<host> 'curl -sI https://github.com'` fails, stop instead of improvising.
- Safety rails: only one process may own the cameras (`cam_preview.py` must be stopped before `cam_server.py`), never trigger captures or recordings, never touch `captures/` or `.env` on a Pi, and never run sync/purge or `pispot.py` remotely.

## Architecture

Shared **active-low** button wired to every Pi's `GPIO_TRIGGER_PIN` (pull-up set in software, no external resistor). A press makes all Pis capture simultaneously via hardware — the network is only used *after* capture for secondaries to push frames to the primary.

- **Primary** (`MODE="p"`, one Pi): HTTP server on `PRIMARY_PORT` — `GET /status` (pi_id, camera count/errors, available camera indices, capture mode, active session + kind, video state, registered secondaries), `POST /register` (secondaries announce themselves), `POST /upload` (frames/videos routed by `batch` param; `kind=video` + `fname` for video files), `POST /remote-capture` (software trigger, passes its batch name to secondaries), `POST /video/start|/video/stop` (synchronized recording, `?res=&fps=&quality=&dur=&highres=`), `POST /mode` (photo/video capture mode), `POST /sync` (fans out to secondaries, backfills missing frames), `POST /purge-secondaries` (remote wipe of secondaries' local captures), `POST /session/start|stop` (`kind=image|video` param; active capture session; batches register into `sessions.json`), `POST /session/batch/delete?session=&batch=` (removes one session-owned batch; refuses while recording), `POST /captures/delete` (deletes an ungrouped batch folder — refuses batches owned by a session). Keyboard `c`/`q` on stdin, plus `v` (record start/stop when mode is video) and `x` (force stop recording).
- **Secondary** (`MODE="s"`, up to `MAX_SECONDARIES`=3): registers with `PRIMARY_HOST` at startup, HTTP server on `SECONDARY_PORT` — `GET /capture` (web-trigger path, optional `?batch=`), `GET /video/start?batch=…&res=…` / `GET /video/stop` (recording in a background thread; stop fans uploads back to the primary), `GET /status` (camera count/errors and recording state), `GET /sync` (pushes every local capture without a `.ok` marker, images and videos), `POST /purge` (deletes local captures, returns count). Registration is one-shot, so restart secondaries after restarting the primary. Headless. **Store-and-forward:** every capture is saved locally under the same batch naming scheme, uploaded, then marked `<file>.ok` on success — so offline periods are recovered by a later sync.

Two trigger paths: **GPIO** (all Pis fire at once) and **web gallery "Capture All"** → primary `/remote-capture` → `GET /capture?batch=<name>` on every registered secondary. Video is web-triggered: gallery "Start Recording" → primary `/video/start` (fan-out `GET /video/start?…` to secondaries) and "Stop Recording" → `/video/stop`; a recording's files are pushed at stop time (or by any later sync). The primary batches uploads into `captures/<YYYYMMDD_HHMMSS>_<hash>/<pi_id>_cam<idx>.jpg|mp4`; uploads carry a `batch` name (secondary-local, computed from the same scheme) or fall back to the current batch. Photo capture is refused while a video recording is in progress (cameras can't be reconfigured mid-encode).

Folder hash = first 4 hex chars of `sha256("<YYYYMMDD_HHMMSS>_<floor(time*5)>")` — the 200ms time bucket makes the name unique within a second (0.5s debounce guarantees different buckets) while staying identical across Pis (all detect the press within one bucket). Keep `batch_stamp()` (in `cam_server.py`) as the single source of batch naming; `new_batch()` wraps it for the primary and registers the batch into the active session (only when the session kind matches the batch kind).

## Video mode

Video recording is a separate capture kind from photos: sessions in `sessions.json` carry `kind` (`image` default/`video`), a session refuses to mix kinds, and only a *video* session accumulates video batches (`new_batch("video")` skips kind-mismatched sessions, so mismatching recordings stay ungrouped). Recording settings are per start (resolution, fps, quality low/medium/high → 4/10/20 Mbps, max duration where `0` = stop manually, else auto-stop timers on every device); the gallery stores them on the active video session and prefills the form.

High resolutions are opt-in: `caps.high_res_sizes` lists sensor modes above `VID_MAX_STREAM`, and the gallery only offers them when the user enables **Enable higher-resolution video modes** on the Video page. That opt-in travels as `highres=1` to the primary and all secondaries. Never treat these modes as safe defaults — software H.264 encoding on Pi 5 can overheat, drop frames or fail, and 4608×2592 is ~14 fps. The gallery displays the risk warning next to the toggle and the fps menu adds 10/14 fps steps only for those modes.

An opt-in **Auto-refresh while Pi videos upload** toggle (default on, persisted in `localStorage` as `mcAutoRefreshVideoUploads`) keeps refreshing the Video page after a recording stops until every expected reachable camera video exists in that batch; the pending batch is persisted as `mcPendingVideoUpload` and refresh stops after 15 minutes. Expected names are derived from each Pi's `available_cameras` minus `camera_errors`; do not count a camera that failed on that device. The toggle must remain available so users can disable the extra polling.

Batches inside sessions can be deleted individually via the batch card's Delete control (`POST /session/batch/delete`, gallery-proxied). The server verifies session ownership, removes the folder and manifest entry while keeping the session, and refuses while that batch is recording. Ungrouped batches keep the separate `/captures/delete` path.

Photo mode is locked while a video is recording: cam_server rejects `POST /mode?mode=photo`, the gallery prevents the Photos tab click, redirects `/` to `/video`, and redirects an already-open photo page once its status poll sees recording active. Video mode remains available so the recording can be stopped.

Video start is isolated per camera: a camera that cannot configure or produce encoder output is restored to still mode, recorded in `camera_errors`, and skipped while other cameras on that Pi continue. `/status` exposes `camera_errors: [{camera, error}]` for both roles, and `/server-status` carries secondaries' errors to the gallery. The header marks the affected Pi with an amber `Cam N failed` status and shows a toast once per distinct failure; the tooltip contains the detailed error. A successful video start clears that camera's previous error.

Picamera2 pipeline: on record start each camera is reconfigured to a lean `create_video_configuration` (4 buffers, no lores/raw — Pi 5/2GB budget, no preview anywhere), flipped via `Transform(hflip=1, vflip=1)` to match stills, then encoder/output pairs are tried defensively until one works (`H264Encoder` → `LibavH264Encoder` × `FfmpegOutput` (.mp4) → `PyavOutput` (.mp4) → `FileOutput` (.h264)). On stop, encoders finalize, cameras are restored to still configuration, and secondaries push their `<PI_ID>_cam<idx>.mp4` files. `video_recording_start/stop()` serialize through `capture_lock`; `video_lock` + `video_state` hold the shared recording state. The cam_server generates a `_thumb.jpg` from frame 0 of each uploaded/local video. GPIO on the primary in video mode toggles record start/stop (with gallery defaults).

## Capabilities & still size

`cam_server.py` reads each camera's `sensor_modes` at startup (`SENSOR_CAPS`, intersected across the Pi's own cameras; empty on failure) and derives a policy-limited ladder (`CAP_STREAM_LADDER` × `CAP_FPS_STEPS`, sizes ≤ `VID_MAX_STREAM`) so offered/accepted settings always fit the 2GB budget. Modes above that ladder are reported separately as `caps.high_res_sizes` and only become selectable when the Video page's high-resolution toggle is on. `caps: {sizes: [...], high_res_sizes: [...], full: [...]}` rides on both roles' `/status`; the gallery proxies it through `/server-status` and builds cascading dropdowns client-side (resolution list from caps; fps steps ≤ that size's max; legacy `1080`/`720` values translated; falls back to static presets when caps are missing). Settings sent by anything are still clamped in `normalise_video_settings` / `normalise_still_size` — never trust the client. Keep `high_res_sizes` out of the default menu and keep the warning visible next to the toggle.

Still resolution is a shared device state (`still_state`/`ensure_still_config`): set from the photo page (Select changes `POST /still-size?size=full|WxH`), fanned out with every remote capture (`/capture?batch=&size=`), honored by GPIO/keyboard captures, remembered on the active image session (`still_size`), and re-applied after video recording ends. Reconfiguration happens lazily inside `grab_frames()` under `capture_lock` only when the size changed.

Ungrouped cleanup: `POST /captures/delete-all?kind=image|video` deletes every batch folder not owned by a session (image kind: folders with no video files; video kind: folders with video — refused while recording). The gallery exposes it as a counted red `Delete Ungrouped (N)` button in each page's subbar with the standard two-step confirm.

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
- `VID_RES` / `VID_FPS` / `VID_QUALITY` / `VID_DURATION`: video settings fallback defaults when no settings arrive (the gallery sends them per start).
- `VID_MAX_RES`: stream/memory ceiling for the automatically offered sizes (default `1080` = 1920×1080; `720` also allowed). Higher sensor modes are exposed only via `high_res_sizes` and the Video page's opt-in toggle — do not raise the default ceiling to make them appear.

`web_gallery.py` knobs: `PORT`, `CAPTURES_DIR`, `PRIMARY_HOST`/`PRIMARY_PORT` (proxy target for controls; defaults to `127.0.0.1:8080`), `CAPTURE_REFRESH_MS` (delay before the gallery auto-reloads after "Capture All", default 1000). `GET /server-status` combines the primary `/status` response with direct pings to each registered secondary's `/status`; the gallery header polls it every five seconds and shows a red `Primary Down` pill when the primary backend is unreachable. The gallery serves two pages — `/` (Photos) and `/video` (Video) — switched by mode tabs in the header that also `POST /mode` so the primary's GPIO/keyboard behavior follows the page being viewed; each page renders only its kind's sessions/batches (server-side classification by content: a folder containing `.mp4`/`.h264` files is a video batch). The Video page owns two client-side persisted toggles: high-resolution opt-in and upload auto-refresh (`localStorage` keys `mcVideoSettings` and `mcAutoRefreshVideoUploads`; pending-batch state in `mcPendingVideoUpload`). `VIDEO_EXTENSIONS`/`video_cards_html`/`video_html`/`base_page`/`PAGE_JS` (shared JS as a plain-string constant with `__REFRESH_MS__`/`__PAGE_MODE__` tokens) in `web_gallery.py`. `cam_preview.py` uses its own `PREVIEW_PORT` (default `9090`) and `SIZE` (default `640x480`).

## Conventions

- Concurrency is `threading` + `http.server` (`ThreadingHTTPServer`). Camera access is serialized by module-level `capture_lock` — keep new endpoints capturing via `grab_frames()` under that lock, never concurrently.
- Camera count is `range(2)`; cameras that fail to open are skipped. GPIO, registration, and network failures only warn. Preserve all defensive degradation.
- Frame pipeline: `capture_array()` → flip → `cv2.cvtColor(..., RGB2BGR)` → save / `imencode` to JPEG. Every full-res frame the primary writes gets a `_thumb.jpg` (~480px) beside it (`write_frame()` for local, imdecode in `handle_upload` for uploaded); the gallery never shows `_thumb.jpg` as a real image.
- Sessions live in `captures/sessions.json` (primary only) and never move files — batches are appended by name, the layout stays flat. Session registration happens in `new_batch()` only; `handle_upload`'s explicit-`batch` path does not register (so sync backfills stay ungrouped). Gallery groups by the manifest and downloads batch/session ZIPs via `/zip/<batch>`, `/zip/session/<name>`, and `/zip/multi?sessions=<csv>&batches=<csv>` (multi-select combined ZIP; arcnames `sessions/<name>/…` / `batches/…`). `POST /session/start` with an existing name resumes it via the gallery's per-session Continue button; starting another session while one is active is refused.
- Gallery sorting is client-side DOM reordering driven by three dropdowns in the subbar (sessions: newest/oldest/name — newest/oldest compare `data-session-created` from `sessions.json`'s `created` or a parsed earliest-batch fallback; batches within sessions: newest/oldest; photos within batches: filename or device cam ID via natural sort). Preferences persist in `localStorage` key `mcGallerySort`; `applySorts()` must run after every `refreshGallery()` and precede `initLightbox()`. Ungrouped ranks by its newest batch timestamp.
- Ungrouped batches get a server-routed Delete (`POST /captures/delete?batch=` proxied to cam_server; session-owned batches are refused there), using the same two-step confirm pattern as session delete. Session-owned batches use `/session/batch/delete` instead (see Video mode); both refuse to delete a batch that is currently recording. Selection mode (`Select` in the subbar) shows per-session checkboxes (including Ungrouped) and one combined download; selection state is transient and reset by gallery refreshes.
- Mobile (≤640px) layout: sticky bottom bar with a large Capture All; Sync/Purge live in its ⋮ overflow menu; header action group is hidden; grid is forced to two columns with icon-only Save buttons. `triggerCapture(btn)`/`triggerSync(btn)` take an optional button argument for the spinner state, so pass `this` from any button that calls them. A global `[hidden] { display: none !important; }` rule exists because class `display` values otherwise override the `hidden` attribute — rely on it for show/hide toggling.
- Secondaries store captures locally and write `<file>.ok` after a successful upload; `GET /sync` pushes everything missing a marker. Sync markers are plain files next to the image.
- `web_gallery.py` is pure stdlib (runs on any machine); `cam_server.py` imports the Pi-only camera stack. Keep `web_gallery.py` importable/testable off-device — the gallery was verified with a local run + browser.
- No comments in code, no tests, no external instruction files.
- Work with incremental git commits: one commit per logical change, made as each unit is complete and verified, with short imperative messages. Never commit unrelated churn or generated artifacts alongside it.
