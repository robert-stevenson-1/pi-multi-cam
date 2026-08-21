# PIPS - Plant Imaging Pi System

Synchronized multi-camera capture on Raspberry Pi 5 — one primary + up to 3 secondaries. All stdlib besides the camera stack.

## Dependencies

Install from `apt`, not pip: pip's `opencv-python` drags in numpy 2.x, which breaks picamera2's `simplejpeg`. Full Raspberry Pi OS images (Bookworm+) ship picamera2 pre-installed; on Lite run the full line:

```bash
sudo apt update
sudo apt install python3-picamera2 python3-opencv python3-gpiod
```

- `picamera2` (pre-installed on full Raspberry Pi OS images; `python3-picamera2` on Lite)
- `python3-opencv` (`cv2`)
- `numpy` (pulled in by the above)
- `python3-gpiod` (GPIO button trigger; optional, degrades gracefully)

## Architecture

A shared **active-low** button is wired to every Pi's `GPIO_TRIGGER_PIN` (pull-up set in software). One press makes all Pis capture simultaneously via hardware — the network is only used *after* capture for secondaries to push frames to the primary.

- **Primary** (`MODE="p"`): capture daemon + HTTP server. Bundles every capture into a timestamped folder.
- **Secondary** (`MODE="s"`, up to 3): headless capture daemon. Registers with the primary at startup, stores every capture locally, then uploads it. Unsynced frames (e.g. primary was down) are pushed later via the gallery's **Sync now**.
- **Gallery** (`web_gallery.py`): browser UI on the primary — PhotoSwipe lightbox, software capture trigger, sync, secondary purge, sessions, downloads, and live primary/secondary health status.

## Usage

**Primary:**

```bash
./launch_primary.sh start   # cam_server + web_gallery
# http://<primary-ip>:8088
```

To run the services in the foreground instead, use two terminals:

```bash
python3 cam_server.py
python3 web_gallery.py
```

**Each secondary** (`.env` with `MODE = "s"`, unique `PI_ID`, `PRIMARY_HOST` set):

```bash
python3 cam_server.py
```

**Camera debug** (standalone, instead of `cam_server.py` — both can't hold the cameras at once): live grid of all camera feeds in the browser.

```bash
python3 cam_preview.py    # http://<pi-ip>:9090
```

No display needed on any Pi. Foreground processes — use tmux/systemd to background them.

**Launch scripts** (start/stop/status/restart/toggle; toggle on bare invocation):

```bash
./launch_primary.sh        # primary: cam_server + web_gallery
./launch_secondary.sh      # each secondary: cam_server
```

Use `status` to see process IDs and the latest log lines. The full logs are in `.run/`:

```bash
./launch_primary.sh status
tail -f .run/cam_server.log .run/web_gallery.log
```

On a secondary, use `./launch_secondary.sh status` and `tail -f .run/cam_server.log`.

Start the primary before the secondaries. A secondary registers once at startup; restart it after the primary has been restarted so it registers again.

### Triggers

- **GPIO button**: all Pis fire at once.
- **Keyboard**: primary accepts `c` (remote capture) and `q` (quit) on stdin.
- **Web gallery**: "Capture All" button → primary `POST /remote-capture` → fires every registered secondary over HTTP.

### Gallery features

- **Capture All** — software trigger.
- **Sync now** — every registered secondary pushes its locally stored captures the primary is missing (recovers frames from when the primary was offline). Secondaries keep local copies after upload (marked `.ok`) until **Purge secondaries** wipes them (two-step confirmation in the UI).
- **Sessions** — start/stop a named session from the gallery; captures taken while a session is active are grouped under it (manifest in `captures/sessions.json`, folder layout unchanged). GPIO and web captures both join the active session.
- **Live status** — the header shows whether the primary cam_server is reachable, its camera count, the active session, and the live state of registered secondaries. A red **Primary Down** pill means the gallery cannot reach `PRIMARY_HOST:PRIMARY_PORT`.
- **In-place gallery updates** — refreshes keep open session sections in place; sessions can be expanded or collapsed individually or all at once.
- **Lightbox** — click any thumbnail for a fullscreen PhotoSwipe preview (swipe, keyboard, pinch-zoom). Vendored in `static/photoswipe/`, fully offline.
- **Downloads** — per-image "save", per-batch "download batch", per-session "download" as a ZIP of the full-res frames.

### Output

```
captures/
  20260811_143052_a1b2/
    pi1_cam0.jpg
    pi1_cam0_thumb.jpg     # 480px grid thumbnail
    pi1_cam1.jpg
    pi2_cam0.jpg
  sessions.json            # session manifest, primary only
```

Folder name = `YYYYMMDD_HHMMSS` + 4-char hash of the 200ms time bucket, so back-to-back presses within the same second still get distinct folders (identical across Pis for one press). Each file is tagged `<pi_id>_cam<idx>.jpg`. Secondaries mirror this layout locally; `.ok` sidecar markers track which frames have been synced to the primary.

## Hotspot TUI (`pispot.py`)

Terminal UI for the NetworkManager Wi-Fi hotspot: edit SSID/password/band/channel/hidden, start/stop, and monitor connected clients (IP, MAC, signal) with a 5s auto-refresh. Keys: `q` quit, `s` start/stop, `r` refresh.

```bash
sudo pip3 install --break-system-packages textual
sudo python3 pispot.py
```

- **Do not use apt's `python3-textual`** — Bookworm ships 0.1.13, which is far too old; the script refuses to start on it.
- **Internet sharing**: hotspot clients reach the internet through the Pi's uplink (`wlan0`/`eth0`). NetworkManager's `shared` mode provides DHCP/DNS/NAT; two things are needed on top:
  - Set the Wi-Fi country code, or AP-mode radios (notably Realtek USB dongles) can stall the uplink: `sudo raspi-config nonint do_wifi_country GB`.
  - IP forwarding — **Apply & Restart** writes `/etc/sysctl.d/99-pispot.conf` (`net.ipv4.ip_forward=1`) automatically and keeps it across reboots.
- The hotspot profile is pinned below the uplink for routing and DNS (`never-default`, high route metric, low DNS priority), so enabling the hotspot never hijacks the Pi's own internet.
- With a dual-band USB dongle, prefer Band `5 GHz` + Channel `36` so the hotspot doesn't share airtime with the 2.4 GHz uplink.

## GPIO Button Wiring

```
GPIO_PIN (e.g., BCM 19) ───[button]── GND
```

Active low — button shorts pin to GND when pressed. No external resistor needed; internal pull-up is configured in software. Set `GPIO_TRIGGER_PIN = -1` to disable.

## Configuration (`.env`, not committed)

Copy `.env.example` to `.env` on each Pi and edit per device — no code edits needed, so `git pull` won't conflict:

```bash
cp .env.example .env
nano .env
```

- `MODE`: `"p"` or `"s"` (`"primary"`/`"secondary"` also accepted).
- `PI_ID`: unique per Pi; embedded in every filename.
- `PRIMARY_HOST`: primary's IP. On the primary set it to `127.0.0.1` (it also drives web_gallery's proxy target); on secondaries it's the primary's IP.
- `PRIMARY_PORT` / `SECONDARY_PORT`: HTTP ports (primary / secondary servers).
- `MAX_SECONDARIES`: cap on registration, default 3.
- `AF_MODE`: `"continuous"` vs `"interval"` (manual AF trigger every 5s).
- `GPIO_TRIGGER_PIN`: BCM pin, `-1` disables; active-low, 0.5s software debounce.
- `CAPTURES_DIR`: output directory.
- `PORT`: web_gallery port.
- `PREVIEW_PORT`: standalone `cam_preview.py` port, default `9090`.
- `SIZE`: standalone preview size, default `640x480`.

`cam_preview.py` is a standalone camera diagnostic. It cannot run at the same time as `cam_server.py` on the same Pi because both processes need to own the cameras. Its `PREVIEW_PORT` is separate from the gallery's `PORT`.
