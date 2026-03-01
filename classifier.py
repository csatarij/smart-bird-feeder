#!/usr/bin/env python3
"""Bird species classifier with multi-backend support for Raspberry Pi 1B.

Supports three inference backends (tried in order):
  1. OpenCV DNN  — works on ANY architecture/Python version (recommended for Pi 1B + Trixie)
  2. tflite-runtime — official TFLite Python bindings (requires armv7l+)
  3. tflite_micro_runtime — community armv6l builds (Bullseye only)

Why OpenCV DNN?
  The official tflite-runtime has no armv6l wheels, and Trixie ships Python 3.13
  which has no community builds either. OpenCV DNN (4.8+) can natively load
  .tflite models with INT8 quantization — and it's already installed via apt.
  This gives us zero-dependency ML inference on the Pi 1B.

Performance note for Pi 1B:
  - INT8 quantized MobileNetV1 via OpenCV DNN: ~20-40 seconds per image
  - The decoupled architecture means capture is never blocked by classification

Usage:
    python3 src/classifier.py [--config config/settings.yaml] [--once]
"""

import argparse
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.stats_engine import StatsEngine
from src.utils import load_config, setup_logging, ensure_directories, PROJECT_ROOT


# ---------------------------------------------------------------------------
# Inference backends
# ---------------------------------------------------------------------------

class OpenCVDNNBackend:
    """Use OpenCV's DNN module to run TFLite/ONNX models.

    Available on ALL architectures and Python versions where python3-opencv
    is installed (apt). OpenCV 4.8+ supports .tflite natively. Older versions
    can load ONNX models instead.
    """

    def __init__(self, model_path: str, input_size: int, logger):
        import cv2
        self.cv2 = cv2
        self.input_size = input_size
        self.logger = logger

        self.net = cv2.dnn.readNet(model_path)
        # Use the default CPU backend (only option on Pi 1B)
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

        self.logger.info(
            f"OpenCV DNN backend loaded: {Path(model_path).name} "
            f"(OpenCV {cv2.__version__})"
        )

    def invoke(self, image_path: Path) -> np.ndarray | None:
        """Run inference and return raw output scores."""
        try:
            img = self.cv2.imread(str(image_path))
            if img is None:
                return None

            # Create blob: resize, scale, swap channels
            # For quantized uint8 models, use scalefactor=1.0, no mean subtraction
            # For float models, use scalefactor=1/127.5, mean=(127.5, 127.5, 127.5)
            blob = self.cv2.dnn.blobFromImage(
                img,
                scalefactor=1.0 / 255.0,
                size=(self.input_size, self.input_size),
                mean=(0, 0, 0),
                swapRB=True,  # BGR -> RGB
                crop=False,
            )
            self.net.setInput(blob)
            output = self.net.forward()
            return output.flatten()

        except Exception as e:
            self.logger.error(f"OpenCV DNN inference failed: {e}")
            return None


class TFLiteBackend:
    """Use tflite-runtime or tflite_micro_runtime for inference.

    Only works if a compatible wheel is installed for your arch + Python.
    Typically NOT available on armv6l + Python 3.13 (Trixie).
    """

    def __init__(self, model_path: str, input_size: int, logger):
        self.input_size = input_size
        self.logger = logger

        # Try tflite-runtime first, then tflite_micro_runtime, then full TF
        Interpreter = None
        for module_path in [
            "tflite_runtime.interpreter",
            "tflite_micro_runtime.interpreter",
            "tensorflow.lite.python.interpreter",
        ]:
            try:
                mod = __import__(module_path, fromlist=["Interpreter"])
                Interpreter = mod.Interpreter
                self.logger.info(f"TFLite backend: {module_path}")
                break
            except ImportError:
                continue

        if Interpreter is None:
            raise ImportError("No TFLite interpreter found")

        self.interpreter = Interpreter(model_path=model_path)
        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()

        self.logger.info(
            f"TFLite backend loaded: {Path(model_path).name} "
            f"(input: {self.input_details[0]['shape']}, "
            f"dtype: {self.input_details[0]['dtype']})"
        )

    def invoke(self, image_path: Path) -> np.ndarray | None:
        """Run inference and return raw output scores."""
        try:
            img = Image.open(image_path).convert("RGB")
            img = img.resize((self.input_size, self.input_size), Image.LANCZOS)
            img_array = np.array(img)

            input_dtype = self.input_details[0]["dtype"]
            if input_dtype == np.uint8:
                input_data = np.expand_dims(img_array, axis=0).astype(np.uint8)
            else:
                input_data = np.expand_dims(
                    img_array.astype(np.float32) / 127.5 - 1.0, axis=0
                )

            self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
            self.interpreter.invoke()
            output = self.interpreter.get_tensor(self.output_details[0]["index"])[0]

            # Dequantize if needed
            if self.output_details[0]["dtype"] == np.uint8:
                scale, zp = self.output_details[0]["quantization"]
                output = (output.astype(np.float32) - zp) * scale

            return output

        except Exception as e:
            self.logger.error(f"TFLite inference failed: {e}")
            return None


def create_backend(model_path: str, input_size: int, logger):
    """Try backends in order: OpenCV DNN -> TFLite -> error.

    OpenCV DNN is tried first because it works on armv6l + Python 3.13
    (Raspbian Trixie) where tflite-runtime has no pre-built wheels.
    """
    model_ext = Path(model_path).suffix.lower()

    # Strategy 1: OpenCV DNN (works everywhere)
    try:
        import cv2
        cv_version = tuple(int(x) for x in cv2.__version__.split(".")[:2])

        if model_ext == ".tflite" and cv_version < (4, 8):
            logger.warning(
                f"OpenCV {cv2.__version__} doesn't support .tflite models "
                f"(need 4.8+). Trying TFLite backend..."
            )
        else:
            backend = OpenCVDNNBackend(model_path, input_size, logger)
            logger.info("Using OpenCV DNN backend (recommended for Pi 1B + Trixie)")
            return backend

    except Exception as e:
        logger.warning(f"OpenCV DNN backend failed: {e}")

    # Strategy 2: TFLite runtime
    try:
        backend = TFLiteBackend(model_path, input_size, logger)
        logger.info("Using TFLite backend")
        return backend
    except ImportError as e:
        logger.warning(f"TFLite backend not available: {e}")

    # No backend available
    logger.error(
        "No ML backend available!\n"
        "  Option A (recommended): Ensure python3-opencv is installed:\n"
        "    sudo apt install python3-opencv\n"
        "  Option B: Install tflite-runtime (armv7l+ only):\n"
        "    pip install tflite-runtime\n"
        "  Option C: Use an ONNX model with OpenCV DNN\n"
    )
    return None


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class BirdClassifier:
    """Bird species classifier with automatic backend selection."""

    def __init__(self, config: dict):
        self.config = config
        self.cls_cfg = config["classifier"]
        self.storage_cfg = config["storage"]

        self.logger = setup_logging(config)
        ensure_directories(config)

        self.captures_dir = PROJECT_ROOT / self.storage_cfg["captures_dir"]
        self.classified_dir = PROJECT_ROOT / self.storage_cfg["classified_dir"]
        self.running = True

        self.backend = None
        self.labels = []

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _shutdown(self, signum, frame):
        self.logger.info("Shutdown signal received...")
        self.running = False

    def load_model(self) -> bool:
        """Load the ML model and labels."""
        model_path = PROJECT_ROOT / self.cls_cfg["model_path"]
        labels_path = PROJECT_ROOT / self.cls_cfg["labels_path"]

        if not model_path.exists():
            self.logger.error(
                f"Model not found at {model_path}. "
                f"Run: python3 download_model.py"
            )
            return False

        if not labels_path.exists():
            self.logger.error(f"Labels not found at {labels_path}")
            return False

        # Load labels
        with open(labels_path, "r") as f:
            self.labels = [line.strip() for line in f if line.strip()]

        # Create inference backend (auto-selects best available)
        input_size = self.cls_cfg.get("input_size", 224)
        self.backend = create_backend(str(model_path), input_size, self.logger)

        if self.backend is None:
            return False

        self.logger.info(f"Model loaded with {len(self.labels)} species labels")
        return True

    def classify(self, image_path: Path) -> list[dict] | None:
        """Classify a bird image and return top-K predictions.

        Returns:
            List of dicts: [{"species": "Blue Tit", "confidence": 0.87}, ...]
            or None if classification fails.
        """
        if self.backend is None:
            return None

        start_time = time.time()
        output = self.backend.invoke(image_path)

        if output is None:
            return None

        # Apply softmax if needed
        if output.min() < 0 or output.sum() > 1.5:
            exp_output = np.exp(output - np.max(output))
            output = exp_output / exp_output.sum()

        elapsed = time.time() - start_time

        # Get top-K predictions
        top_k = self.cls_cfg.get("top_k", 3)
        top_indices = np.argsort(output)[::-1][:top_k]

        predictions = []
        for idx in top_indices:
            if idx < len(self.labels):
                predictions.append({
                    "species": self.labels[idx],
                    "confidence": float(output[idx]),
                })

        if predictions:
            self.logger.info(
                f"Classified {image_path.name} in {elapsed:.1f}s -- "
                f"Top: {predictions[0]['species']} "
                f"({predictions[0]['confidence']:.1%})"
            )
        return predictions

    def process_queue(self, stats: StatsEngine) -> int:
        """Process all pending images in the captures queue."""
        queue_files = sorted(self.captures_dir.glob("*.jpg"))
        if not queue_files:
            return 0

        self.logger.info(f"Processing {len(queue_files)} queued image(s)...")
        processed = 0
        threshold = self.cls_cfg.get("confidence_threshold", 0.3)

        for image_path in queue_files:
            if not self.running:
                break

            predictions = self.classify(image_path)

            if predictions and predictions[0]["confidence"] >= threshold:
                top = predictions[0]
                species_dir = self.classified_dir / self._safe_dirname(top["species"])
                species_dir.mkdir(parents=True, exist_ok=True)

                dest = species_dir / image_path.name
                shutil.move(str(image_path), str(dest))

                stats.record_sighting(
                    species=top["species"],
                    confidence=top["confidence"],
                    image_path=str(dest.relative_to(PROJECT_ROOT)),
                    predictions=predictions,
                )
                processed += 1
            else:
                reason = "no predictions" if not predictions else (
                    f"low confidence ({predictions[0]['confidence']:.1%})"
                )
                self.logger.info(f"Skipping {image_path.name}: {reason}")
                unclassified_dir = self.classified_dir / "_unclassified"
                unclassified_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(image_path), str(unclassified_dir / image_path.name))

        return processed

    @staticmethod
    def _safe_dirname(species_name: str) -> str:
        """Convert species name to a safe directory name."""
        return species_name.lower().replace(" ", "_").replace("'", "")

    def run(self, once: bool = False):
        """Main classification loop (or single pass if once=True)."""
        if not self.load_model():
            self.logger.error("Cannot start without a model. Exiting.")
            sys.exit(1)

        stats = StatsEngine(self.config)
        poll_interval = self.cls_cfg.get("poll_interval", 5)
        self.logger.info(
            f"Classifier ready -- polling {self.captures_dir} "
            f"every {poll_interval}s"
        )

        try:
            while self.running:
                count = self.process_queue(stats)
                if count > 0:
                    stats.export_daily_summary()

                if once:
                    break

                time.sleep(poll_interval)
        finally:
            stats.close()
            self.logger.info("Classifier stopped.")


def main():
    parser = argparse.ArgumentParser(description="Bird species classifier")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument(
        "--once", action="store_true",
        help="Process queue once and exit (for cron/testing)",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    classifier = BirdClassifier(config)
    classifier.run(once=args.once)


if __name__ == "__main__":
    main()
