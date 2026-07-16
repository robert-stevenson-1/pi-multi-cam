# pi-multi-cam

syncronised multi-camera capturing from single and multiple pi 5 SBCs

## Dependencies

- `picamera2`
- `opencv-python` (`cv2`)
- `numpy`
- `gpiod` (for GPIO button trigger)

## Usage

```bash
python3 dual_cam.py
```

Press `c` to capture, `q` to quit.

## GPIO Button Trigger

Set a GPIO pin to trigger capture (same as pressing `c`). Edit `dual_cam.py`:

```python
# -1 = disabled, or any BCM pin number
GPIO_TRIGGER_PIN = 17
```

**Wiring** (active low — button shorts pin to GND when pressed):

```
GPIO_PIN (e.g., BCM 17) ───[button]── GND
```

No external resistor needed — the internal pull-up is configured in software.
