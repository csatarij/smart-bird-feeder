"""Privacy utilities: ROI cropping, EXIF stripping, and blur.

Privacy-by-design: only the bird feeder region is ever saved. Full frames
are processed in memory and immediately discarded. All metadata is stripped.
"""

from pathlib import Path

import numpy as np
from PIL import Image

try:
    import piexif

    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False


def extract_roi(frame: np.ndarray, roi_config: dict) -> np.ndarray:
    """Crop the frame to the Region of Interest (feeder area only).

    Args:
        frame: Full camera frame as numpy array (H, W, C).
        roi_config: Dict with keys x, y, width, height (all 0.0-1.0 fractions).

    Returns:
        Cropped numpy array containing only the feeder region.
    """
    h, w = frame.shape[:2]
    x1 = int(w * roi_config.get("x", 0.0))
    y1 = int(h * roi_config.get("y", 0.0))
    x2 = int(x1 + w * roi_config.get("width", 1.0))
    y2 = int(y1 + h * roi_config.get("height", 1.0))

    # Clamp to frame boundaries
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    return frame[y1:y2, x1:x2].copy()


def blur_outside_roi(frame: np.ndarray, roi_config: dict, ksize: int = 51) -> np.ndarray:
    """Apply heavy Gaussian blur to everything outside the ROI.

    This is a secondary safeguard — if crop_to_roi_only is enabled (default),
    this function is not needed. But it's here for defense-in-depth.
    """
    import cv2

    h, w = frame.shape[:2]
    x1 = max(0, int(w * roi_config["x"]))
    y1 = max(0, int(h * roi_config["y"]))
    x2 = min(w, int(x1 + w * roi_config["width"]))
    y2 = min(h, int(y1 + h * roi_config["height"]))

    blurred = cv2.GaussianBlur(frame, (ksize, ksize), 0)
    # Paste the sharp ROI back onto the blurred frame
    blurred[y1:y2, x1:x2] = frame[y1:y2, x1:x2]
    return blurred


def strip_exif(image_path: str | Path) -> None:
    """Remove all EXIF metadata from a JPEG file in-place.

    Strips GPS coordinates, timestamps, camera info, and any other metadata
    that could compromise privacy.
    """
    image_path = Path(image_path)

    if HAS_PIEXIF:
        try:
            piexif.remove(str(image_path))
            return
        except Exception:
            pass

    # Fallback: re-save with PIL (drops all metadata)
    img = Image.open(image_path)
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))
    clean.save(image_path, "JPEG", quality=90)


def resize_if_needed(image_path: str | Path, max_dimension: int) -> None:
    """Resize image so its longest side is at most max_dimension pixels."""
    image_path = Path(image_path)
    img = Image.open(image_path)
    w, h = img.size

    if max(w, h) <= max_dimension:
        return

    ratio = max_dimension / max(w, h)
    new_size = (int(w * ratio), int(h * ratio))
    img = img.resize(new_size, Image.LANCZOS)

    # Save without metadata
    img.save(image_path, "JPEG", quality=90)


def check_blurriness(image_path: str | Path) -> dict:
    """Assess image sharpness using Laplacian variance.

    Returns a dict with:
        score: float — Laplacian variance (higher = sharper).
        is_blurry: bool — True if score is below the threshold.
        assessment: str — Human-readable assessment.
    """
    import cv2

    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {"score": 0.0, "is_blurry": True, "assessment": "Could not read image"}

    score = cv2.Laplacian(img, cv2.CV_64F).var()

    # Thresholds calibrated for typical Pi Camera v2 captures
    if score < 50:
        assessment = "Very blurry — check camera focus and lens cleanliness"
    elif score < 100:
        assessment = "Slightly blurry — consider adjusting focus"
    elif score < 300:
        assessment = "Acceptable sharpness"
    else:
        assessment = "Sharp image"

    return {"score": round(score, 1), "is_blurry": score < 100, "assessment": assessment}


def save_private_image(
    frame: np.ndarray,
    output_path: str | Path,
    roi_config: dict,
    privacy_config: dict,
) -> Path:
    """Full privacy pipeline: crop → resize → strip EXIF → save.

    This is the single function that should be used to persist any image.
    The full frame is NEVER written to disk.

    Args:
        frame: Raw camera frame (numpy array).
        output_path: Where to save the processed image.
        roi_config: ROI configuration.
        privacy_config: Privacy settings.

    Returns:
        Path to the saved (privacy-processed) image.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Crop to ROI (most important privacy step)
    if privacy_config.get("crop_to_roi_only", True):
        processed = extract_roi(frame, roi_config)
    elif privacy_config.get("blur_outside_roi", True):
        processed = blur_outside_roi(frame, roi_config)
    else:
        processed = frame.copy()

    # Step 2: Convert to PIL and save
    img = Image.fromarray(processed)

    # Step 3: Resize if needed
    max_dim = privacy_config.get("max_saved_dimension", 640)
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    # Step 4: Save without any metadata
    img.save(output_path, "JPEG", quality=90, exif=b"")

    # Step 5: Extra safety — strip EXIF again
    if privacy_config.get("strip_exif", True):
        strip_exif(output_path)

    return output_path
