# 🐦 Smart Bird Feeder — Raspberry Pi 1B

An offline, privacy-first smart bird feeder that detects birds, photographs them, classifies species using on-device ML, and generates statistics — all running on a Raspberry Pi 1 Model B.

## Why This Project?

This project demonstrates practical IoT skills: embedded Linux, edge ML inference on constrained hardware, computer vision, data pipelines, system automation, and privacy-by-design architecture — all on a $25 single-board computer from 2012.

---

## Hardware

| Component | Details |
|-----------|---------|
| Board | Raspberry Pi 1 Model B (ARM11 @ 700MHz, 512MB RAM, ARMv6) |
| Camera | Raspberry Pi Camera Module v1.3 (or USB webcam) |
| Storage | 16-32GB microSD (Class 10 / A1 recommended) |
| Power | 5V / 1A micro-USB PSU |
| Enclosure | Weatherproof case (3D-printed or commercial) |
| Feeder | Standard bird feeder positioned 30-50cm from camera |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Raspberry Pi 1B                     │
│                                                      │
│  ┌──────────┐    ┌──────────┐    ┌───────────────┐  │
│  │  Camera   │───▶│ Motion   │───▶│ Snapshot      │  │
│  │  Stream   │    │ Detector │    │ Capture       │  │
│  └──────────┘    └──────────┘    └──────┬────────┘  │
│                                         │            │
│                                         ▼            │
│  ┌──────────────────────────────────────────────┐   │
│  │  Classification Queue (filesystem-based)      │   │
│  └──────────────┬───────────────────────────────┘   │
│                  │                                    │
│                  ▼                                    │
│  ┌──────────────────────┐   ┌────────────────────┐  │
│  │ OpenCV DNN / TFLite  │──▶│ Statistics Engine   │  │
│  │ (MobileNetV1 quant.) │   │ (SQLite + JSON)    │  │
│  └──────────────────────┘   └────────┬───────────┘  │
│                                       │              │
│                                       ▼              │
│                              ┌────────────────────┐  │
│                              │ GitHub Sync (cron) │  │
│                              └────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### Key Design Decisions for Pi 1B

1. **Motion detection via frame differencing** — no ML needed, just NumPy/OpenCV pixel math
2. **Decoupled capture and classification** — motion detector saves photos to a queue directory; a separate classifier process picks them up on its own schedule (avoids blocking)
3. **OpenCV DNN as primary ML backend** — `tflite-runtime` has no armv6l wheels and Trixie ships Python 3.13 with no community builds. OpenCV 4.8+ can load `.tflite` models natively via `cv2.dnn.readNet()`, and it's already installed via apt. Zero extra dependencies. Falls back to tflite-runtime if available.
4. **Filesystem-based queue** — no message broker needed; just directories (`captures/` → `classified/`)
5. **SQLite for stats** — zero-config, low memory, perfect for this scale
6. **Cron-driven GitHub sync** — pushes daily summary + selected photos via Git

---

## Privacy-by-Design

| Threat | Mitigation |
|--------|------------|
| Camera captures people | Region-of-Interest (ROI) crop to feeder area only; frames outside ROI are never saved |
| Network exposure | No cloud APIs, no open ports; outbound-only Git push over SSH |
| Photo metadata | EXIF stripped before storage and upload |
| Stored images | Only cropped bird images are retained; full frames are discarded immediately |
| GitHub uploads | Only bird crops + stats are pushed; never raw frames |
| WiFi | Optional — works fully offline; sync only when connected |

---

## Setup

### 1. OS & Dependencies

```bash
# Flash Raspberry Pi OS Lite (Legacy, Bullseye — last ARMv6 support)
# Enable camera: sudo raspi-config → Interface → Camera

# Install system packages
sudo apt-get update

# NOTE: libatlas-base-dev was removed in Debian Trixie (2025+).
# Use libopenblas-dev instead if you're on Trixie/Bookworm+:
#   Bullseye/older: sudo apt-get install -y libatlas-base-dev
#   Trixie/newer:   sudo apt-get install -y libopenblas-dev
sudo apt-get install -y python3-pip python3-venv python3-opencv \
    python3-picamera2 python3-numpy python3-pil sqlite3 git libopenblas-dev

# Create project venv (--system-site-packages to reuse apt-installed opencv/numpy)
python3 -m venv --system-site-packages ~/bird-feeder-env
source ~/bird-feeder-env/bin/activate

# Install Python packages (tflite-runtime is NOT needed — OpenCV DNN handles inference)
pip install Pillow piexif PyYAML schedule
```

### 2. Download the Model

```bash
# MobileNetV1 quantized for image classification
mkdir -p ~/smart-bird-feeder/models
cd ~/smart-bird-feeder/models

# Download the iNaturalist bird model (or use the provided script)
python3 ../scripts/download_model.py
```

### 3. Configure

Edit `config/settings.yaml` to match your setup (camera, ROI, species list, etc).

### 4. Run

```bash
# Start the motion detector + capture service
python3 src/motion_detector.py &

# Start the classifier (separate process, runs when CPU is free)
python3 src/classifier.py &

# Or use the systemd services (recommended)
sudo cp scripts/*.service /etc/systemd/system/
sudo systemctl enable --now bird-capture bird-classify
```

---

## Project Structure

```
smart-bird-feeder/
├── README.md
├── config/
│   └── settings.yaml          # All configuration
├── src/
│   ├── motion_detector.py     # Camera + motion detection + snapshot
│   ├── classifier.py          # TFLite bird species classification
│   ├── stats_engine.py        # SQLite stats + JSON export
│   ├── privacy.py             # ROI crop, EXIF strip, blur
│   ├── github_sync.py         # Push results to GitHub
│   └── utils.py               # Shared utilities
├── scripts/
│   ├── download_model.py      # Fetch TFLite model + labels
│   ├── setup.sh               # One-shot setup script
│   ├── bird-capture.service   # systemd unit
│   ├── bird-classify.service  # systemd unit
│   └── daily_sync.sh          # Cron script for GitHub push
├── models/                    # TFLite model + labels (gitignored)
├── data/
│   ├── captures/              # Raw motion-triggered snapshots (queue)
│   ├── classified/            # Species-labeled bird photos
│   ├── stats/                 # JSON + SQLite statistics
│   └── birds.db               # SQLite database
├── docs/
│   ├── HARDWARE_SETUP.md
│   ├── PERFORMANCE.md
│   └── PRIVACY.md
├── tests/
│   ├── test_motion.py
│   ├── test_classifier.py
│   └── test_privacy.py
├── .gitignore
├── requirements.txt
└── LICENSE
```

---

## Enhancement Roadmap

Each of these extensions adds a demonstrable skill to your portfolio. They're ordered roughly by effort and impact.

### Phase 2 — Better ML (Weeks 3-4)

- [ ] **Fine-tune on local species**: Use transfer learning (MobileNetV1 → your top 20 local species) with TensorFlow on a desktop PC, export INT8 TFLite. Document the training pipeline in a Jupyter notebook — this alone is a strong portfolio piece.
- [ ] **Add audio classification with BirdNET**: Cornell's BirdNET TFLite model identifies birds by song. Fuse audio + visual confidence for much higher accuracy. Requires a USB microphone (~$5).
- [ ] **Confidence calibration**: Track and plot model confidence vs. accuracy over time. Show that you understand model reliability, not just accuracy.

### Phase 3 — IoT & Embedded Skills (Weeks 5-6)

- [ ] **MQTT telemetry**: Publish sightings to a local MQTT broker (Mosquitto). Demonstrates IoT protocol knowledge.
- [ ] **Environmental sensors**: Add a BME280 (temperature, humidity, pressure) via I2C. Correlate weather with bird activity — "Blue Tits visit 40% more on rainy mornings."
- [ ] **Power monitoring**: Log CPU temperature and power draw over time with `vcgencmd`. Show you understand thermal management on embedded devices.
- [ ] **OTA updates**: Implement a simple self-update mechanism (Git pull + systemd restart). Shows operational thinking.

### Phase 4 — Data & Visualization (Weeks 7-8)

- [ ] **GitHub Pages dashboard**: Auto-generate a static site (Chart.js or D3.js) from your JSON stats. Push to GitHub Pages — viewers see live bird data without any server.
- [ ] **Species activity heatmap**: Time-of-day × species matrix showing when each species is most active.
- [ ] **Seasonal trends**: After a few months of data, plot migration patterns and seasonal species shifts.
- [ ] **Data export**: CSV and JSON APIs for your data, making it easy for others to analyze.

### Phase 5 — Advanced Architecture (Ongoing)

- [ ] **Edge-to-hub offload**: Keep the Pi as a thin capture device; send images over LAN to a more powerful classifier (Jetson Nano, old laptop). Demonstrates distributed IoT architecture while maintaining privacy.
- [ ] **Multi-feeder mesh**: Support multiple Pi cameras feeding into a single classifier/dashboard. Uses mDNS for zero-config discovery.
- [ ] **Container deployment**: Package the entire stack in Docker (with Balena for Pi fleet management). Shows DevOps skills.
- [ ] **CI/CD pipeline**: GitHub Actions to lint, test, and build on every push. Auto-generate release artifacts.
- [ ] **Prometheus + Grafana monitoring**: Export system metrics (CPU, RAM, disk, capture rate) to Prometheus. Build a Grafana dashboard showing system health alongside bird data.

### Stretch Goals

- [ ] **Bird visitor alerts**: Rare species detection → push notification via Ntfy (self-hosted, privacy-preserving push notifications).
- [ ] **Feeder level monitoring**: Ultrasonic sensor to detect when the feeder needs refilling. Automate a "refill reminder" commit to the data repo.
- [ ] **Time-lapse generation**: Stitch daily best-of photos into a monthly time-lapse video (ffmpeg on a nightly cron job).
- [ ] **Citizen science integration**: Export data in eBird-compatible format for contribution to real ornithological research.

---

## Can the Pi 1B Really Run ML?

Yes — with caveats. Here's an honest assessment:

| Aspect | Reality |
|--------|---------|
| **Inference speed** | 20-40 seconds per image with INT8 MobileNetV1 via OpenCV DNN. Slow by modern standards, but birds sit for 1-5 minutes. |
| **Accuracy** | MobileNetV1 is not state-of-the-art, but with fine-tuning on local species (20-30 classes instead of 1000), accuracy is surprisingly good (~80-90% top-1). |
| **Memory** | Tight at 512MB. The decoupled architecture is essential — you can't run the camera and classifier simultaneously in one process. |
| **TFLite on ARMv6** | The official `tflite-runtime` has no armv6l wheels, and Trixie's Python 3.13 has no community builds. We use OpenCV DNN (4.8+) instead, which loads `.tflite` models natively — zero extra dependencies. |
| **Reliability** | Runs 24/7 for months without issues if you manage memory carefully and use systemd for auto-restart. |
| **Alternative** | For faster classification, the Phase 5 edge-to-hub offload sends photos to a LAN machine. Still fully offline and private. |

The engineering challenge of making ML work on extreme hardware is itself the portfolio piece. Any interviewer who works with IoT or edge computing will appreciate the constraints you navigated.

---

## License

MIT — see [LICENSE](LICENSE).
