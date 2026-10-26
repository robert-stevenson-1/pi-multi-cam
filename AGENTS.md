# Agent instructions

## Runtime and verification

- Multi-Pi camera capture for Raspberry Pi 5: `cam_server.py` owns the cameras; run one instance per Pi. `cam_preview.py` also owns cameras and must not run alongside it.
- The primary runs `cam_server.py` and `web_gallery.py`; secondaries run only `cam_server.py`. Gallery pages are `/` (Photos) and `/video`; static PhotoSwipe assets are vendored under `static/` because the primary may be offline.
- Camera behavior requires Pi hardware. Do not import/run `cam_server.py` or add tests/CI on a dev machine. Validate syntax with `python3 -m py_compile cam_server.py cam_preview.py web_gallery.py pispot.py`.
- Use apt’s Raspberry Pi camera/OpenCV packages. Do not pip-install `opencv-python`: its NumPy 2 dependency can break Raspberry Pi’s `picamera2`/`simplejpeg`. `gpiod` is optional and uses pip’s v2 bindings; GPIO failures must not stop startup. `pispot.py` needs current pip Textual, not apt’s old `python3-textual`.

## Configuration and operation

- `.env` is gitignored; copy `.env.example` per Pi. Real environment variables take precedence over `.env`. `MODE`, unique `PI_ID`, `PRIMARY_HOST`, ports, `CAPTURES_DIR`, AF/GPIO and `VID_*` values are per-device settings.
- Use `./launch_primary.sh {start|stop|restart|status}` on the primary and `./launch_secondary.sh {start|stop|restart|status}` on secondaries. Logs/PIDs live in `.run/`. Start the primary first; secondaries retry registration every 30 seconds.
- `PRIMARY_HOST` on the primary must resolve to its own cam_server (normally `127.0.0.1`); on secondaries it is the primary’s address. `web_gallery.py` uses `PRIMARY_HOST`/`PRIMARY_PORT` to proxy controls.
- Still GPIO capture is simultaneous hardware triggering; video is started/stopped by the gallery and fanned out over HTTP. Secondaries save locally and upload after stop; `.ok` sidecars mark successful uploads and `/sync` backfills missing files.
- Sessions are flat folders registered in `captures/sessions.json`; `kind` separates image and video sessions. `batch_stamp()` in `cam_server.py` is the shared batch-name source.

## Camera and video behavior

- All camera operations share `capture_lock`. Keep camera configuration/capture serialized; do not reconfigure a camera while recording.
- Read `Picamera2.sensor_modes` before configuring or starting that camera; querying afterward can raise “Camera must be stopped before configuring.” Capability discovery is per camera, intersected per Pi, then limited by `VID_MAX_RES` for the 2GB RAM budget.
- Video uses a lean 4-buffer configuration with no preview/lores/raw streams. Libav encoders should try `PyavOutput` before other outputs; an output is accepted only after it produces non-empty data. Auto-stop must use the same stop/fan-out/upload path as a manual stop.
- Video start is per-camera fault tolerant: restore and skip a camera that cannot configure or produce output, continue with working cameras, and expose failures as `camera_errors` in each Pi’s `/status`. The gallery carries secondary errors into `/server-status` and shows the affected Pi in the header.
- While any video is recording, photo mode is locked. Preserve both the gallery UI guard and the primary `/mode` server-side refusal; direct `/` navigation redirects to `/video`.
- The gallery is hand-built HTML/CSS/JS in `web_gallery.py`. Keep static assets local/offline. The global `[hidden] { display: none !important; }` rule is intentional; after gallery refreshes, run `applySorts()` before `initLightbox()`.

## Remote Pi access and safety

- Before remote work, probe with `getent hosts pi-cam-0.local` and `ssh -o BatchMode=yes -o ConnectTimeout=3 pi@pi-cam-0.local true`. Use full `.local` hostnames (`pi-cam-0.local` primary; `pi-cam-1.local` through `pi-cam-3.local` secondaries); do not retry interactively if unavailable.
- On Pis, inspect `git status` before pulling; never reset/stash a dirty checkout. Deployment is push, `git pull --ff-only`, `py_compile`, then the launch script restart; restart primary before secondaries. Never use `sudo`, `systemctl`, or package installation remotely.
- Never trigger captures/recordings, run Sync/Purge, or modify `.env`/`captures/` remotely. Ask the user to perform hardware validation when a recording is needed.

## Change discipline

- Make one commit per logical change with a short imperative message. Do not include unrelated or generated files.
