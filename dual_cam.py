from picamera2 import Picamera2
from libcamera import controls
import cv2
import numpy as np
import time

# "continuous" or "interval" (triggers focus every 5s)
AF_MODE = "continuous"

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
        print("Capturing...")
        for idx, frame in enumerate(frames):
            cv2.imwrite(f"cam{idx}.jpg", frame)
        print(f"Saved cam0.jpg{' and cam1.jpg' if len(frames) > 1 else ''}")

cv2.destroyAllWindows()
for cam in cameras:
    cam.stop()
