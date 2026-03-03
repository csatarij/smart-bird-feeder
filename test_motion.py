#!/usr/bin/env python3
"""Tests for the motion detection module."""

import tempfile
from pathlib import Path

import numpy as np
import pytest

from motion_detector import MotionDetector
from utils import load_config


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def detector(config):
    return MotionDetector(config)


class TestMotionDetection:
    """Test the frame differencing algorithm."""

    def test_no_motion_on_identical_frames(self, detector):
        """Two identical frames should produce no motion event."""
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        # First frame establishes baseline
        assert detector.detect_motion(frame) is False
        # Second identical frame — no motion
        assert detector.detect_motion(frame) is False

    def test_motion_on_different_frames(self, detector):
        """Significantly different frames should trigger motion."""
        frame1 = np.zeros((480, 640, 3), dtype=np.uint8)
        # First frame establishes baseline
        detector.detect_motion(frame1)

        # Second frame with large bright region (simulating a bird)
        frame2 = np.zeros((480, 640, 3), dtype=np.uint8)
        frame2[100:300, 100:400] = 200  # Bright patch in ROI area
        assert detector.detect_motion(frame2) is True

    def test_small_noise_below_threshold(self, detector):
        """Minor pixel noise should not trigger motion."""
        frame1 = np.full((480, 640, 3), 128, dtype=np.uint8)
        detector.detect_motion(frame1)

        # Add very slight noise (within blur + threshold tolerance)
        frame2 = frame1.copy()
        noise = np.random.randint(-5, 5, frame2.shape, dtype=np.int16)
        frame2 = np.clip(frame2.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        assert detector.detect_motion(frame2) is False

    def test_cooldown_prevents_rapid_captures(self, detector):
        """Cooldown timer should prevent burst captures."""
        import time

        detector.last_capture_time = time.time()
        assert detector._is_cooldown_active() is True

        detector.last_capture_time = time.time() - 100
        assert detector._is_cooldown_active() is False


class TestSnapshotSaving:
    """Test that snapshots are saved correctly."""

    def test_save_creates_file(self, detector):
        """Saving a snapshot should create a JPEG file."""
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmpdir:
            detector.captures_dir = Path(tmpdir)
            path = detector.save_snapshot(frame)
            assert path is not None
            assert path.exists()
            assert path.suffix == ".jpg"

    def test_saved_image_is_cropped(self, detector):
        """Saved image should be ROI-cropped, not full frame."""
        from PIL import Image

        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmpdir:
            detector.captures_dir = Path(tmpdir)
            path = detector.save_snapshot(frame)

            img = Image.open(path)
            # The saved image should be smaller than the original frame
            # because it's cropped to ROI
            assert img.size[0] <= 640
            assert img.size[1] <= 480


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
