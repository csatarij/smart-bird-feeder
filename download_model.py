#!/usr/bin/env python3
"""Download a TFLite bird classification model and labels.

This script downloads a quantized MobileNetV1 model suitable for the
Raspberry Pi 1B. You have several options:

Option 1 (Default): iNaturalist birds MobileNetV1 — pre-trained on 964 bird
    species, INT8 quantized (~4MB). Best balance of accuracy and speed.

Option 2: Custom fine-tuned model — if you train your own model on local
    species, place it in models/ and update config/settings.yaml.

Option 3: General ImageNet MobileNetV1 — can distinguish "bird" from other
    objects but cannot identify species. Good for testing.
"""

import os
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"


def download_file(url: str, dest: Path, description: str = ""):
    """Download a file with progress indicator."""
    print(f"Downloading {description or dest.name}...")
    print(f"  URL: {url}")

    try:
        def progress_hook(block_num, block_size, total_size):
            downloaded = block_num * block_size
            if total_size > 0:
                pct = min(100, downloaded * 100 // total_size)
                mb = downloaded / (1024 * 1024)
                print(f"\r  Progress: {pct}% ({mb:.1f} MB)", end="", flush=True)

        urllib.request.urlretrieve(url, str(dest), reporthook=progress_hook)
        print(f"\n  Saved to: {dest}")
        return True
    except Exception as e:
        print(f"\n  Download failed: {e}")
        return False


def create_sample_labels():
    """Create a sample bird labels file with common species.

    In production, this should match your model's actual label file.
    This is a starter list for testing and can be replaced with the
    labels that correspond to your chosen model.
    """
    labels = [
        "American Robin",
        "Baltimore Oriole",
        "Black-capped Chickadee",
        "Blue Jay",
        "Blue Tit",
        "Brown Thrasher",
        "Cardinal",
        "Carolina Wren",
        "Cedar Waxwing",
        "Chaffinch",
        "Coal Tit",
        "Common Blackbird",
        "Common Grackle",
        "Common Starling",
        "Dark-eyed Junco",
        "Downy Woodpecker",
        "European Goldfinch",
        "European Robin",
        "Goldcrest",
        "Goldfinch",
        "Great Spotted Woodpecker",
        "Great Tit",
        "Greenfinch",
        "House Finch",
        "House Sparrow",
        "House Wren",
        "Indigo Bunting",
        "Long-tailed Tit",
        "Magpie",
        "Mourning Dove",
        "Northern Cardinal",
        "Northern Mockingbird",
        "Nuthatch",
        "Pine Siskin",
        "Purple Finch",
        "Red-bellied Woodpecker",
        "Red-breasted Nuthatch",
        "Red-winged Blackbird",
        "Rose-breasted Grosbeak",
        "Ruby-throated Hummingbird",
        "Song Sparrow",
        "Tufted Titmouse",
        "White-breasted Nuthatch",
        "White-crowned Sparrow",
        "White-throated Sparrow",
        "Wood Pigeon",
        "Wren",
        "Yellow Warbler",
    ]

    labels_path = MODELS_DIR / "bird_labels.txt"
    with open(labels_path, "w") as f:
        for label in labels:
            f.write(label + "\n")

    print(f"Created sample labels file with {len(labels)} species: {labels_path}")
    return labels_path


def setup_imagenet_model():
    """Download the standard MobileNetV1 quantized model (ImageNet).

    This is a general-purpose classifier that can detect 'bird' as a
    category but cannot distinguish species. Good for initial testing.
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    model_url = (
        "https://storage.googleapis.com/download.tensorflow.org/"
        "models/mobilenet_v1_2018_08_02/mobilenet_v1_1.0_224_quant.tgz"
    )

    tgz_path = MODELS_DIR / "mobilenet_v1.tgz"
    ok = download_file(model_url, tgz_path, "MobileNetV1 INT8 quantized")

    if ok:
        import tarfile
        with tarfile.open(tgz_path, "r:gz") as tar:
            tar.extractall(MODELS_DIR)
        tgz_path.unlink()

        # Rename to standard name
        tflite_file = MODELS_DIR / "mobilenet_v1_1.0_224_quant.tflite"
        if tflite_file.exists():
            dest = MODELS_DIR / "bird_model.tflite"
            tflite_file.rename(dest)
            print(f"Model ready: {dest}")

    return ok


def main():
    print("=" * 60)
    print("Smart Bird Feeder — Model Setup")
    print("=" * 60)
    print()
    print("This script sets up the ML model for bird classification.")
    print()
    print("Options:")
    print("  1. Download MobileNetV1 (ImageNet) for testing")
    print("  2. Create sample labels file only (BYO model)")
    print("  3. Instructions for custom bird model")
    print()

    choice = input("Choose option [1/2/3]: ").strip()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    if choice == "1":
        if setup_imagenet_model():
            # For ImageNet, download ImageNet labels
            labels_url = (
                "https://storage.googleapis.com/download.tensorflow.org/"
                "models/mobilenet_v1_1.0_224/labels.txt"
            )
            download_file(
                labels_url,
                MODELS_DIR / "bird_labels.txt",
                "ImageNet labels"
            )
            print("\nImageNet model ready. This can classify 'bird' vs other")
            print("objects but cannot identify species. Good for testing!")
        print()

    elif choice == "2":
        create_sample_labels()
        print("\nPlace your custom .tflite model at: models/bird_model.tflite")
        print("Make sure the labels file matches your model's output classes.")
        print()

    elif choice == "3":
        print()
        print("To get a bird-specific model, you have several options:")
        print()
        print("A) Use TensorFlow Hub iNaturalist model:")
        print("   - Search tfhub.dev for 'iNaturalist' bird classifiers")
        print("   - Convert to TFLite with INT8 quantization")
        print("   - Target input size: 224x224")
        print()
        print("B) Fine-tune MobileNetV1 on a bird dataset:")
        print("   - Use the CUB-200-2011 or NABirds dataset")
        print("   - Transfer-learn from ImageNet weights")
        print("   - Quantize to INT8 for Pi 1B performance")
        print("   - See docs/TRAINING.md for a complete guide")
        print()
        print("C) Use the BirdNET model (audio-based, not image):")
        print("   - birdnet.cornell.edu — complements the visual classifier")
        print()
        create_sample_labels()

    else:
        print("Invalid choice. Run again and select 1, 2, or 3.")
        sys.exit(1)

    print("Done! Update config/settings.yaml if you changed model paths.")


if __name__ == "__main__":
    main()
