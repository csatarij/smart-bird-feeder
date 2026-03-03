#!/usr/bin/env python3
"""Motion detector: watches the camera feed and saves snapshots when movement
is detected in the ROI (bird feeder area).

Design for Pi 1B:
- Uses simple frame differencing (no ML) — very lightweight.
- Processes only the ROI region, reducing pixel count.
- Respects a cooldown timer to avoid burst captures of the same bird.
- Saves images through the privacy pipeline (crop, strip, resize).

Usage:
    python3 motion_detector.py [--config settings.yaml]
"""

import argparse
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from privacy import extract_roi, save_private_image
from utils import load_config, setup_logging, ensure_directories, PROJECT_ROOT


class MotionDetector:
    """Lightweight frame-differencing motion detector optimised for Pi 1B."""

    def __init__(self, config: dict):
        self.config = config
        self.cam_cfg = config["camera"]
        self.motion_cfg = config["motion"]
        self.privacy_cfg = config["privacy"]
        self.storage_cfg = config["storage"]
        self.roi_cfg = self.motion_cfg["roi"]

        self.logger = setup_logging(config)
        ensure_directories(config)

        self.captures_dir = PROJECT_ROOT / self.storage_cfg["captures_dir"]
        self.running = True
        self.last_capture_time = 0
        self.prev_gray = None

        # Graceful shutdown
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _shutdown(self, signum, frame):
        self.logger.info("Shutdown signal received, cleaning up...")
        self.running = False

    def _init_camera(self):
        """Initialize the camera based on configuration."""
        cam_type = self.cam_cfg.get("type", "picamera")
        w = self.cam_cfg.get("resolution_width", 640)
        h = self.cam_cfg.get("resolution_height", 480)

        if cam_type == "picamera":
            try:
                from picamera2 import Picamera2
                cam = Picamera2()
                cam_config = cam.create_still_configuration(
                    main={"size": (w, h), "format": "RGB888"}
                )
                cam.configure(cam_config)
                cam.start()
                time.sleep(self.cam_cfg.get("warmup_seconds", 3))
                self.logger.info(f"PiCamera initialized at {w}x{h}")
                return cam, "picamera2"
            except ImportError:
                self.logger.warning("picamera2 not available, trying legacy picamera")
                try:
                    import picamera
                    import picamera.array
                    cam = picamera.PiCamera()
                    cam.resolution = (w, h)
                    cam.framerate = self.cam_cfg.get("framerate", 10)
                    cam.rotation = self.cam_cfg.get("rotation", 0)
                    time.sleep(self.cam_cfg.get("warmup_seconds", 3))
                    self.logger.info(f"Legacy PiCamera initialized at {w}x{h}")
                    return cam, "picamera_legacy"
                except ImportError:
                    self.logger.warning("No picamera library found, falling back to USB")
                    cam_type = "usb"

        # USB webcam fallback
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        if not cap.isOpened():
            self.logger.error("Failed to open camera!")
            sys.exit(1)
        time.sleep(self.cam_cfg.get("warmup_seconds", 3))
        self.logger.info(f"USB camera initialized at {w}x{h}")
        return cap, "usb"

    def _capture_frame(self, camera, cam_type: str) -> np.ndarray | None:
        """Capture a single frame from the camera."""
        try:
            if cam_type == "picamera2":
                return camera.capture_array()
            elif cam_type == "picamera_legacy":
                import picamera.array
                with picamera.array.PiRGBArray(camera) as output:
                    camera.capture(output, "rgb")
                    return output.array
            else:
                ret, frame = camera.read()
                if ret:
                    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                return None
        except Exception as e:
            self.logger.error(f"Frame capture error: {e}")
            return None

    def detect_motion(self, frame: np.ndarray) -> bool:
        """Detect motion in the ROI using frame differencing.

        Algorithm:
        1. Extract and grayscale the ROI only (saves CPU).
        2. Apply Gaussian blur to suppress noise.
        3. Compute absolute difference with previous frame.
        4. Threshold and count changed pixels.
        5. Trigger if changed percentage exceeds sensitivity.
        """
        roi = extract_roi(frame, self.roi_cfg)
        gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)

        ksize = self.motion_cfg.get("blur_kernel", 21)
        gray = cv2.GaussianBlur(gray, (ksize, ksize), 0)

        if self.prev_gray is None:
            self.prev_gray = gray
            return False

        # Frame difference
        delta = cv2.absdiff(self.prev_gray, gray)
        self.prev_gray = gray

        # Threshold
        thresh_val = self.motion_cfg.get("diff_threshold", 30)
        _, thresh = cv2.threshold(delta, thresh_val, 255, cv2.THRESH_BINARY)

        # Calculate percentage of changed pixels
        changed_pct = (np.count_nonzero(thresh) / thresh.size) * 100
        sensitivity = self.motion_cfg.get("sensitivity_percent", 3.0)

        if changed_pct > sensitivity:
            self.logger.debug(f"Motion detected: {changed_pct:.1f}% changed (threshold: {sensitivity}%)")
            return True
        return False

    def _is_cooldown_active(self) -> bool:
        """Check if we're still in the post-capture cooldown period."""
        cooldown = self.motion_cfg.get("cooldown_seconds", 10)
        return (time.time() - self.last_capture_time) < cooldown

    def save_snapshot(self, frame: np.ndarray) -> Path | None:
        """Save a privacy-processed snapshot to the captures queue."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"bird_{timestamp}.jpg"
        output_path = self.captures_dir / filename

        try:
            saved_path = save_private_image(
                frame=frame,
                output_path=output_path,
                roi_config=self.roi_cfg,
                privacy_config=self.privacy_cfg,
            )
            self.logger.info(f"Snapshot saved: {saved_path.name}")
            return saved_path
        except Exception as e:
            self.logger.error(f"Failed to save snapshot: {e}")
            return None

    def run(self):
        """Main detection loop."""
        self.logger.info("Starting motion detector...")
        camera, cam_type = self._init_camera()
        self.logger.info(f"Camera type: {cam_type} — watching for birds...")

        frame_interval = 1.0 / self.cam_cfg.get("framerate", 10)
        captures_today = 0

        try:
            while self.running:
                frame = self._capture_frame(camera, cam_type)
                if frame is None:
                    time.sleep(0.5)
                    continue

                if self.detect_motion(frame) and not self._is_cooldown_active():
                    path = self.save_snapshot(frame)
                    if path:
                        self.last_capture_time = time.time()
                        captures_today += 1
                        self.logger.info(
                            f"Capture #{captures_today} today — "
                            f"saved to queue for classification"
                        )

                # Sleep to maintain target framerate and save CPU
                time.sleep(frame_interval)

        finally:
            self.logger.info("Shutting down camera...")
            if cam_type == "usb":
                camera.release()
            elif cam_type == "picamera2":
                camera.stop()
            elif cam_type == "picamera_legacy":
                camera.close()
            self.logger.info("Motion detector stopped.")


def main():
    parser = argparse.ArgumentParser(description="Bird feeder motion detector")
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to settings.yaml"
    )
    args = parser.parse_args()
    config = load_config(args.config)
    detector = MotionDetector(config)
    detector.run()


if __name__ == "__main__":
    main()
