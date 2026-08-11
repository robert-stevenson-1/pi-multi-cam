# pi-multi-cam

Synchronized multi-camera capture on Raspberry Pi 5 — one primary + up to 3 secondaries. All stdlib besides the camera stack.

## Dependencies

- `picamera2`
- `opencv-python` (`cv2`)
- `numpy`
- `gpiod` (for GPIO button trigger; optional, degrades gracefully)

## Architecture

A shared **active-low** button is wired to every Pi's `GPIO_TRIGGER_PIN` (pull-up set in software). One press makes all Pis capture simultaneously via hardware — the network is only used *after* capture for secondaries to push frames to the primary.

- **Primary** (`MODE="p"`): capture daemon + HTTP server. Bundles every capture into a timestamped folder.
- **Secondary** (`MODE="s"`, up to 3): headless capture daemon. Registers with the primary at startup and uploads its frames after each press.
- **Gallery** (`web_gallery.py`): browser UI on the primary to preview captures and trigger a software capture.

## Usage

**Primary:**

```bash
python3 cam_server.py        # MODE = "p", PI_ID = "pi1"
python3 web_gallery.py       # http://<primary-ip>:8088
```

**Each secondary** (edit `MODE = "s"`, `PI_ID`, `PRIMARY_HOST`):

```bash
python3 cam_server.py
```

No display needed on any Pi. Foreground processes — use tmux/systemd to background them.

### Triggers

- **GPIO button**: all Pis fire at once.
- **Keyboard**: primary accepts `c` (remote capture) and `q` (quit) on stdin.
- **Web gallery**: "Capture All" button → primary `POST /remote-capture` → fires every registered secondary over HTTP.

### Output

```
captures/
  20260811_143052_a1b2/
    pi1_cam0.jpg
    pi1_cam1.jpg
    pi2_cam0.jpg
```

Folder name = `YYYYMMDD_HHMMSS` + 4-char hash of the 200ms time bucket, so back-to-back presses within the same second still get distinct folders (identical across Pis for one press). Each file is tagged `<pi_id>_cam<idx>.jpg`.

## GPIO Button Wiring

```
GPIO_PIN (e.g., BCM 19) ───[button]── GND
```

Active low — button shorts pin to GND when pressed. No external resistor needed; internal pull-up is configured in software. Set `GPIO_TRIGGER_PIN = -1` to disable.

## Configuration (`cam_server.py`)

- `MODE`: `"p"` or `"s"` (`"primary"`/`"secondary"` also accepted).
- `PI_ID`: unique per Pi; embedded in every filename.
- `PRIMARY_HOST`: IP of primary (secondary only).
- `PRIMARY_PORT` / `SECONDARY_PORT`: HTTP ports (primary / secondary servers).
- `MAX_SECONDARIES`: cap on registration, default 3.
- `AF_MODE`: `"continuous"` vs `"interval"` (manual AF trigger every 5s).
- `GPIO_TRIGGER_PIN`: BCM pin, `-1` disables; active-low, 0.5s software debounce.
