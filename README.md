# Smart Bird Feeder — Raspberry Pi 1B

An offline, privacy-first smart bird feeder that detects birds, photographs them, classifies species using on-device ML, and generates statistics — all running on a Raspberry Pi 1 Model B.

Browse your bird photos and monitor system health from any device on your local network via the built-in web server.

## Hardware

| Component | Details |
|-----------|---------|
| Board | Raspberry Pi 1 Model B (ARM11 @ 700MHz, 512MB RAM, ARMv6) |
| Camera | Raspberry Pi Camera Module v2 (IMX219, 8MP, 3280×2464 native — or USB webcam) |
| Storage | 64GB microSD (Class 10 / A1 recommended) |
| Power | 5V / 1A micro-USB PSU |
| Enclosure | Weatherproof case (3D-printed or commercial) |
| Feeder | Standard bird feeder positioned 30-50cm from camera |

## Architecture

```
                          Raspberry Pi 1B
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  ┌──────────┐    ┌──────────┐    ┌───────────────┐          │
  │  │  Camera   │───>│ Motion   │───>│ Snapshot      │          │
  │  │  Stream   │    │ Detector │    │ Capture       │          │
  │  └──────────┘    └──────────┘    └──────┬────────┘          │
  │                                         │                    │
  │                         disk space check │                   │
  │                                         v                    │
  │  ┌──────────────────────────────────────────────┐           │
  │  │  Classification Queue (filesystem-based)      │           │
  │  └──────────────┬───────────────────────────────┘           │
  │                  │                                            │
  │                  v                                            │
  │  ┌──────────────────────┐   ┌────────────────────┐          │
  │  │ OpenCV DNN / TFLite  │──>│ Statistics Engine   │          │
  │  │ (MobileNetV1 quant.) │   │ (SQLite + JSON)    │          │
  │  └──────────────────────┘   └────────┬───────────┘          │
  │                                       │                      │
  │                          storage pruning & keep_best_only    │
  │                                       │                      │
  │                                       v                      │
  │                              ┌────────────────────┐          │
  │                              │ Web Server (:8080)  │          │
  │                              │  Gallery + Health   │          │
  │                              └────────────────────┘          │
  └──────────────────────────────────────────────────────────────┘
             │
             │  LAN (any browser)
             v
    ┌──────────────────┐
    │  Phone / Laptop  │
    │  Photo gallery   │
    │  Health dashboard│
    └──────────────────┘
```

### Key Design Decisions for Pi 1B

1. **Motion detection via frame differencing** — no ML needed, just NumPy/OpenCV pixel math
2. **Decoupled capture and classification** — motion detector saves photos to a queue directory; a separate classifier process picks them up on its own schedule (avoids blocking)
3. **OpenCV DNN as primary ML backend** — `tflite-runtime` has no armv6l wheels and Trixie ships Python 3.13 with no community builds. OpenCV 4.8+ can load `.tflite` models natively via `cv2.dnn.readNet()`, and it's already installed via apt. Zero extra dependencies. Falls back to tflite-runtime if available.
4. **Filesystem-based queue** — no message broker needed; just directories (`captures/` -> `classified/`)
5. **SQLite for stats** — zero-config, low memory, perfect for this scale
6. **Local-first storage** — all photos stay on the 64GB SD card; grab them off the Pi whenever you like
7. **Built-in web server** — browse photos and check system health from any device on the LAN (no cloud, no external dependencies)

---

## Privacy-by-Design

| Threat | Mitigation |
|--------|------------|
| Camera captures people | Region-of-Interest (ROI) crop to feeder area only; frames outside ROI are never saved |
| Photo metadata | EXIF stripped before storage |
| Stored images | Only cropped bird images are retained; full frames are discarded immediately |
| Network exposure | No cloud APIs; web server is LAN-only; no inbound connections from the internet |
| WiFi | Optional — works fully offline |

See [PRIVACY.md](PRIVACY.md) for the full privacy design document.

---

## Setup

### Quick Start (automated)

```bash
git clone <repo-url> ~/smart-bird-feeder
cd ~/smart-bird-feeder
chmod +x setup.sh
./setup.sh
```

The setup script installs system packages, creates a virtualenv, downloads the ML model, and optionally installs systemd services (motion detector, classifier, and web server) for auto-start on boot.

Once setup finishes, open `http://<pi-ip>:8080/onboarding` in your browser to run the guided setup wizard (see [Onboarding Wizard](#onboarding-wizard) below).

### Manual Setup

#### 1. OS & Dependencies

```bash
# Flash Raspberry Pi OS Lite (Legacy, Bullseye — last ARMv6 support)
# Enable camera: sudo raspi-config -> Interface -> Camera

# Install system packages
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv python3-opencv \
    python3-picamera2 python3-numpy python3-pil sqlite3 git libopenblas-dev

# Create project venv (--system-site-packages to reuse apt-installed opencv/numpy)
python3 -m venv --system-site-packages ~/smart-bird-feeder/venv
source ~/smart-bird-feeder/venv/bin/activate

# Install Python packages
pip install -r requirements.txt
```

#### 2. Download the Model

```bash
python3 download_model.py
```

#### 3. Configure

Edit `settings.yaml` to match your setup (camera type, ROI coordinates, sensitivity, etc.).

#### 4. Run

```bash
# Start the motion detector
python3 motion_detector.py &

# Start the classifier (separate process, runs when CPU is free)
python3 classifier.py &

# Start the photo browser & health dashboard
python3 webserver.py &

# Or install all three as systemd services (recommended)
sudo cp bird-capture.service bird-classify.service bird-webserver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bird-capture bird-classify bird-webserver
```

The web server will be available at `http://<pi-ip>:8080`.

---

## Onboarding Wizard

After running `setup.sh` (or starting the web server manually), open `http://<pi-ip>:8080/onboarding` in your browser. The wizard walks you through five steps to configure your feeder:

1. **Camera Check** — takes a test photo to verify the camera is connected and working.
2. **Orientation** — confirms the image is right-side-up and lets you apply 0°/90°/180°/270° rotation.
3. **Focus Check** — analyzes sharpness and warns if the lens needs cleaning or adjusting.
4. **Crop Area (ROI)** — drag a rectangle over the test photo to define the region of interest. Only this area is captured and saved (everything outside is discarded for privacy).
5. **Review & Finish** — shows a summary of your configuration (camera, model, ROI) and the status of each systemd service, with controls to start/stop/restart them.

Until the wizard is completed, a banner on the main page links to `/onboarding`. Once finished, the flag is saved and the banner disappears.

---

## Web Server

The built-in web server lets you browse bird photos and monitor the system from any device on your local network — phone, tablet, or laptop. It uses only the Python standard library (no Flask needed) and runs with a ~30MB memory footprint.

| URL | Description |
|-----|-------------|
| `http://<pi-ip>:8080/` | Photo gallery — browse by species, see thumbnails |
| `http://<pi-ip>:8080/onboarding` | **Guided setup wizard** — camera, orientation, focus, ROI, services |
| `http://<pi-ip>:8080/stats` | Capture statistics — species rankings, daily/hourly charts |
| `http://<pi-ip>:8080/calibration` | **Confidence calibration & sightings inspector** |
| `http://<pi-ip>:8080/health` | Live health dashboard — disk, processes, recent sightings |
| `http://<pi-ip>:8080/api/stats` | All-time statistics as JSON |
| `http://<pi-ip>:8080/api/calibration` | Calibration metrics and debug data as JSON |
| `http://<pi-ip>:8080/api/health` | System health data as JSON |
| `http://<pi-ip>:8080/api/feedback` | POST — record a correct/incorrect verdict for a sighting |

The health dashboard auto-refreshes every 30 seconds and shows:
- Process status (motion detector & classifier running/stopped)
- Disk usage with visual progress bar
- Photo counts (classified, queued, unclassified)
- Species breakdown
- Database stats and recent sightings
- Log file size
- **Power & Thermal**: CPU temperature, core voltage, throttle status (via `vcgencmd`), and a 24-hour history log

### Confidence Calibration Page (`/calibration`)

The calibration page helps you track whether the model's confidence scores are meaningful and debug misclassifications:

- **Calibration summary** — total sightings, average confidence, number of manually verified sightings, verified accuracy, and ECE (Expected Calibration Error, lower = better-calibrated).
- **Confidence distribution histogram** — how sightings are spread across 10% confidence buckets (0–100%).
- **Reliability diagram (calibration curve)** — for each confidence bucket, the fraction of verified sightings that were actually correct. Points on the diagonal = perfect calibration. Green circles = model is underconfident; red = overconfident. Circle size scales with the number of verified samples in that bucket.
- **Daily average confidence trend** — 30-day bar chart showing whether model confidence is drifting.
- **Species confidence table** — per-species average, min, and max confidence, plus verified accuracy.
- **Sightings inspector** — the 50 most recent classifications with photo thumbnails, confidence scores, expandable top-K prediction lists, and ✓/✗ feedback buttons to mark each as correct or incorrect. Feedback is stored in the SQLite database and feeds into the calibration curve.

You can also generate a static health report to copy off the Pi:

```bash
python3 generate_health_html.py
# Creates data/health.html — open in any browser
```

---

## Project Structure

```
smart-bird-feeder/
├── motion_detector.py        # Camera + motion detection + snapshot
├── classifier.py             # Bird species classification (OpenCV DNN / TFLite)
├── stats_engine.py           # SQLite stats + JSON export
├── privacy.py                # ROI crop, EXIF strip, blur
├── github_sync.py            # Push results to GitHub (optional, disabled by default)
├── utils.py                  # Shared utilities (config, logging, paths, pruning)
├── webserver.py              # Local network photo browser & health dashboard
├── generate_health_html.py   # Generate static health.html report
├── download_model.py         # Fetch TFLite model + labels
├── settings.yaml             # All configuration
├── setup.sh                  # One-shot setup script
├── requirements.txt          # Python dependencies
├── bird-capture.service      # systemd unit — motion detector
├── bird-classify.service     # systemd unit — classifier
├── bird-webserver.service    # systemd unit — web server
├── daily_sync.sh             # Cron script for GitHub push (if enabled)
├── test_classifier.py        # Classifier tests
├── test_motion.py            # Motion detection tests
├── test_privacy.py           # Privacy module tests
├── models/                   # TFLite model + labels (gitignored)
│   ├── bird_model.tflite
│   └── bird_labels.txt
├── data/                     # Runtime data (gitignored)
│   ├── captures/             # Motion-triggered snapshots (classification queue)
│   ├── classified/           # Species-labeled bird photos
│   │   └── <species_name>/   # One directory per species
│   ├── stats/                # JSON statistics exports
│   ├── birds.db              # SQLite database
│   └── power_log.csv         # CPU temperature & voltage time-series (vcgencmd)
├── HARDWARE_SETUP.md
├── PERFORMANCE.md
├── PRIVACY.md
└── LICENSE
```

---

## Storage & Data Management

Photos are stored locally on the SD card. Storage is managed automatically:

- **Auto-pruning**: When disk usage exceeds `storage.max_storage_mb` (default: 50 GB), the oldest photos are deleted first.
- **Keep-best mode**: Set `storage.keep_best_only: true` to keep only the highest-confidence photo per species per day.
- **Disk check before capture**: The motion detector checks available space before saving new snapshots.

To grab your data off the Pi:

```bash
# Copy all classified photos
scp -r pi@<pi-ip>:~/smart-bird-feeder/data/classified/ ./bird-photos/

# Copy statistics
scp pi@<pi-ip>:~/smart-bird-feeder/data/stats/ ./bird-stats/

# Copy the database
scp pi@<pi-ip>:~/smart-bird-feeder/data/birds.db ./
```

GitHub sync is available but **disabled by default**. To enable it, set `github.enabled: true` in `settings.yaml` and configure `github.repo_url`.

---

## Recent Changes

| Change | Details |
|--------|---------|
| **Camera resolution** | Updated to Pi Camera v2 native max: **3280×2464** (up from 640×480). `privacy.max_saved_dimension` raised to 2048 for higher-quality saves. |
| **Log retention** | Switched from size-based rotation to **daily rotation with 30-day retention** (`TimedRotatingFileHandler`). Controlled by `logging.rotation: "daily"` and `logging.backup_count: 30`. |
| **Power monitoring** | The web server starts a background thread that writes CPU temperature, core voltage, and throttle status to **`data/power_log.csv`** every 5 minutes (configurable). The health dashboard now shows a **Power & Thermal card** with current values and a collapsible 24-hour history. |

---

## Enhancement Roadmap

Ordered roughly by effort and impact.

### Phase 2 — Better ML

- [ ] **Fine-tune on local species**: Use transfer learning (MobileNetV1 -> your top 20 local species) with TensorFlow on a desktop PC, export INT8 TFLite.
- [ ] **Add audio classification with BirdNET**: Cornell's BirdNET TFLite model identifies birds by song. Fuse audio + visual confidence for much higher accuracy. Requires a USB microphone (~$5).
- [x] **Confidence calibration**: Track and plot model confidence vs. accuracy over time — live calibration curve, ECE metric, and sightings inspector at `/calibration`.

### Phase 3 — IoT & Embedded Skills

- [ ] **MQTT telemetry**: Publish sightings to a local MQTT broker (Mosquitto).
- [ ] **Environmental sensors**: Add a BME280 (temperature, humidity, pressure) via I2C. Correlate weather with bird activity — "Blue Tits visit 40% more on rainy mornings."
- [x] **Power monitoring**: Log CPU temperature and core voltage over time with `vcgencmd`. The web server background-threads a CSV logger (`data/power_log.csv`) and displays a live Power & Thermal card on the health dashboard with throttle-status detection.
- [ ] **OTA updates**: Implement a simple self-update mechanism (Git pull + systemd restart).

### Phase 4 — Data & Visualization

- [ ] **Enhanced web dashboard**: Add Chart.js graphs to the built-in web server — species activity heatmaps, daily trends, hourly distribution charts.
- [ ] **Seasonal trends**: After a few months of data, plot migration patterns and seasonal species shifts.
- [ ] **Data export**: CSV download from the web interface.

### Phase 5 — Advanced Architecture (Ongoing)

- [ ] **Edge-to-hub offload**: Keep the Pi as a thin capture device; send images over LAN to a more powerful classifier (Jetson Nano, old laptop).
- [ ] **Multi-feeder mesh**: Support multiple Pi cameras feeding into a single classifier/dashboard. Uses mDNS for zero-config discovery.
- [ ] **Container deployment**: Package the entire stack in Docker (with Balena for Pi fleet management).
- [ ] **CI/CD pipeline**: GitHub Actions to lint, test, and build on every push. Auto-generate release artifacts.
- [ ] **Prometheus + Grafana monitoring**: Export system metrics (CPU, RAM, disk, capture rate) to Prometheus.

### Stretch Goals

- [ ] **Bird visitor alerts**: Rare species detection -> push notification via Ntfy (self-hosted, privacy-preserving push notifications).
- [ ] **Feeder level monitoring**: Ultrasonic sensor to detect when the feeder needs refilling.
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

---

## License

MIT — see [LICENSE](LICENSE).
