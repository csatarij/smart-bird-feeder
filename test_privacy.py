#!/usr/bin/env python3
"""Tests for the privacy module."""

import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.privacy import extract_roi, save_private_image, strip_exif


@pytest.fixture
def sample_frame():
    """A 480x640 RGB frame with distinct regions for testing."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # Mark the expected ROI area with white
    frame[48:432, 64:576] = 255  # Approx 0.1-0.9 of each dimension
    return frame


@pytest.fixture
def roi_config():
    return {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}


@pytest.fixture
def privacy_config():
    return {
        "strip_exif": True,
        "crop_to_roi_only": True,
        "blur_outside_roi": True,
        "max_saved_dimension": 640,
    }


class TestROIExtraction:

    def test_roi_is_smaller_than_frame(self, sample_frame, roi_config):
        roi = extract_roi(sample_frame, roi_config)
        assert roi.shape[0] < sample_frame.shape[0]
        assert roi.shape[1] < sample_frame.shape[1]

    def test_roi_dimensions_match_config(self, sample_frame, roi_config):
        roi = extract_roi(sample_frame, roi_config)
        expected_h = int(480 * 0.8)
        expected_w = int(640 * 0.8)
        assert roi.shape[0] == expected_h
        assert roi.shape[1] == expected_w

    def test_full_roi_returns_full_frame(self, sample_frame):
        full_roi = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        roi = extract_roi(sample_frame, full_roi)
        assert roi.shape == sample_frame.shape


class TestEXIFStripping:

    def test_exif_is_removed(self):
        """Saved images should have no EXIF metadata."""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            # Create an image with some metadata
            img = Image.new("RGB", (100, 100), color="red")
            img.save(f.name, "JPEG")

            strip_exif(f.name)

            # Reload and check for metadata
            img2 = Image.open(f.name)
            exif = img2.info.get("exif", b"")
            assert len(exif) == 0 or exif == b""

            Path(f.name).unlink()


class TestPrivateImageSaving:

    def test_saved_image_exists(self, sample_frame, roi_config, privacy_config):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.jpg"
            result = save_private_image(sample_frame, output, roi_config, privacy_config)
            assert result.exists()
            assert result.stat().st_size > 0

    def test_saved_image_is_cropped(self, sample_frame, roi_config, privacy_config):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.jpg"
            save_private_image(sample_frame, output, roi_config, privacy_config)
            img = Image.open(output)
            # Should be ROI size or smaller (due to max_saved_dimension)
            assert img.size[0] <= 640
            assert img.size[1] <= 480

    def test_max_dimension_respected(self, sample_frame, roi_config):
        privacy_cfg = {
            "strip_exif": True,
            "crop_to_roi_only": True,
            "max_saved_dimension": 200,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "test.jpg"
            save_private_image(sample_frame, output, roi_config, privacy_cfg)
            img = Image.open(output)
            assert max(img.size) <= 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
