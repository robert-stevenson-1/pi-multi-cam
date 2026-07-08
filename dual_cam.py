from picamera2 import Picamera2
import cv2
import numpy as np

picam0 = Picamera2(0)
picam1 = Picamera2(1)

picam0.start()
picam1.start()

while True:
    raw0 = picam0.capture_array()
    raw1 = picam1.capture_array()
    frame0 = cv2.cvtColor(cv2.flip(raw0, -1), cv2.COLOR_RGB2BGR)
    frame1 = cv2.cvtColor(cv2.flip(raw1, -1), cv2.COLOR_RGB2BGR)
    combined = np.hstack((frame0, frame1))
    cv2.imshow("Cameras (c=capture, q=quit)", combined)

    key = cv2.waitKey(1) & 0xFF
    if key == ord("q"):
        print("Quitting.")
        break
    elif key == ord("c"):
        print("Capturing...")
        cv2.imwrite("cam0.jpg", frame0)
        cv2.imwrite("cam1.jpg", frame1)
        print("Saved cam0.jpg and cam1.jpg")

cv2.destroyAllWindows()
picam0.stop()
picam1.stop()
