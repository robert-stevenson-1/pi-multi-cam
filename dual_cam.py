from picamera2 import Picamera2
from libcamera import controls
import cv2
import numpy as np
import time

try:
    import gpiod
    from gpiod.line import Direction, Bias, Value
except ImportError:
    gpiod = None
    print("gpiod failed to import")

# "continuous" or "interval" (triggers focus every 5s)
AF_MODE = "continuous"

# GPIO pin for hardware capture trigger (-1 = disabled, BCM numbering)
GPIO_TRIGGER_PIN = 19

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
    import sys; sys.exit(1)

# --- GPIO trigger setup ---
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
                consumer="dual-cam-trigger",
                config={GPIO_TRIGGER_PIN: settings},
            )
            print(f"GPIO trigger enabled on pin {GPIO_TRIGGER_PIN}")
        except Exception as e:
            print(f"WARNING: GPIO trigger failed: {e}")
            gpio_request = None

def capture_frames(frames):
    """Save current frames to disk."""
    print("Capturing...")
    for idx, frame in enumerate(frames):
        cv2.imwrite(f"cam{idx}.jpg", frame)
    print(f"Saved cam0.jpg{' and cam1.jpg' if len(frames) > 1 else ''}")

gpio_debounce_until = 0

while True:
    frames = []
    for cam in cameras:
        if AF_MODE == "interval":
            if not hasattr(cam, "_last_af_trigger"):
                cam._last_af_trigger = 0
            if time.time() - cam._last_af_trigger >= 5:
                cam.set_controls({"AfTrigger": controls.AfTriggerEnum.Start})
                cam._last_af_trigger = time.time()
        raw = cam.capture_array()
        frame = cv2.cvtColor(cv2.flip(raw, -1), cv2.COLOR_RGB2BGR)
        frames.append(frame)

    combined = np.hstack(frames)
    cv2.imshow("Cameras (c=capture, q=quit)", combined)

    key = cv2.waitKey(1) & 0xFF
    if key == ord("q"):
        print("Quitting.")
        break
    elif key == ord("c"):
        capture_frames(frames)

    # GPIO trigger check (active low: pin reads 0 when button pressed)
    if gpio_request is not None and time.time() > gpio_debounce_until:
        if gpio_request.get_value(GPIO_TRIGGER_PIN) == Value.ACTIVE:
            capture_frames(frames)
            gpio_debounce_until = time.time() + 0.5

if gpio_request is not None:
    gpio_request.release()
cv2.destroyAllWindows()
for cam in cameras:
    cam.stop()
