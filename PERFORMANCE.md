# Performance on Raspberry Pi 1 Model B

## Hardware Constraints

| Resource | Pi 1B Spec | Impact |
|----------|-----------|--------|
| CPU | ARM11 @ 700MHz, single-core | ~15-30s per TFLite inference |
| RAM | 512MB (shared with GPU) | ~350MB usable; must be frugal |
| GPU | VideoCore IV | Not used (no GPU-accelerated TFLite for ARMv6) |
| Storage | 64GB SD card (Class 10: ~20MB/s) | Image saves take ~100ms; auto-pruning at configurable limit |
| USB | USB 2.0 (shared bus) | Camera + storage compete for bandwidth |

## Measured Performance (Approximate)

| Operation | Time | CPU % | RAM |
|-----------|------|-------|-----|
| Frame capture (640×480) | ~100ms | 15% | 5MB |
| ROI extraction + grayscale | ~10ms | 5% | 2MB |
| Motion detection (frame diff) | ~15ms | 8% | 3MB |
| JPEG save (ROI crop) | ~80ms | 20% | 3MB |
| TFLite inference (MobileNetV1 INT8, OpenCV DNN) | 20-40s | 95% | ~80MB |
| EXIF strip | ~20ms | 5% | 2MB |
| SQLite insert | ~5ms | 2% | 1MB |

### Total idle footprint

Motion detector loop: ~30MB RAM, ~20% CPU at 10 FPS check rate.

### Web server footprint

The built-in web server (`webserver.py`) uses the Python standard library `http.server` — no Flask or other framework. Idle footprint is ~15-30MB RAM and negligible CPU. Serving a gallery page with thumbnails briefly spikes to ~5% CPU as it reads directory listings. The MemoryMax for the systemd unit is set to 100MB as a safety cap.

### During classification

Classification temporarily spikes to ~95% CPU and ~110MB RAM. The decoupled architecture ensures the motion detector keeps running (it's I/O bound, not CPU bound).

## Optimisation Strategies

### 1. Decoupled architecture (implemented)

The motion detector and classifier run as separate processes. The detector saves to a filesystem queue; the classifier picks up files when CPU is available. This means:
- Bird arrival is captured in <200ms regardless of classification load
- Classification can take 30 seconds without missing the next bird
- If the Pi is overloaded, photos queue up and get classified later

### 2. ROI-only processing (implemented)

Processing only the ROI (typically 60-80% of pixels) reduces computation for motion detection by up to 40%.

### 3. Adaptive frame rate

Reduce check rate at night (birds are diurnal). Example cron approach:
- Daytime (6am-8pm): 10 FPS motion check → fast response
- Night (8pm-6am): 1 FPS or pause entirely → saves power

### 4. Batch classification

Instead of classifying each photo immediately, accumulate a batch and classify during off-peak hours (e.g., midnight). This avoids CPU contention during peak bird activity.

### 5. Model quantisation

INT8 quantisation (already used) is essential. The difference:
- FLOAT32 MobileNetV1: ~60-90 seconds per inference (and may OOM)
- INT8 MobileNetV1: ~20-40 seconds per inference, fits in RAM

### 6. OpenCV DNN as inference backend

On the Pi 1B (armv6l) running Trixie (Python 3.13), the official `tflite-runtime`
pip package is unavailable (no armv6l wheels, no Python 3.13 builds). OpenCV's
DNN module (4.8+) loads `.tflite` models natively via `cv2.dnn.readNet()` —
it is installed via apt and requires zero extra dependencies. Performance is
comparable to tflite-runtime on this hardware.

### 7. Offload option

For users who want faster classification, the README documents an optional architecture where the Pi captures photos and a more powerful machine on the LAN classifies them. This keeps the privacy model intact (no cloud) while enabling <1s classification.

## Memory Management

With 512MB total (350MB usable), memory is the tightest constraint:
- Linux kernel + base services: ~100MB
- Python interpreter: ~15MB
- Motion detector: ~30MB
- Web server (idle): ~20MB
- Classifier (during inference): ~80MB
- SQLite: ~5MB
- **Total during classification: ~250MB** (leaves ~100MB headroom)

To stay safe:
- Only one TFLite inference at a time (enforced by single classifier process)
- Images are loaded one at a time, never batched in memory
- NumPy arrays are explicitly freed after use
- The classifier uses `Nice=10` to yield to the motion detector
- The web server systemd unit has `MemoryMax=100M` as a safety cap

## Storage Management

With a 64GB SD card, storage is plentiful but not unlimited. The system manages it automatically:

- **Auto-pruning** (`utils.prune_old_files`): When the classified photos directory exceeds `storage.max_storage_mb` (default: 50,000 MB / ~50 GB), the oldest photos are deleted first.
- **Keep-best mode**: Setting `storage.keep_best_only: true` retains only the highest-confidence photo per species per day, dramatically reducing storage usage.
- **Disk check before capture**: The motion detector verifies free space before saving new snapshots.
- **GitHub sync disabled by default**: Photos stay on the SD card. Copy them off with `scp` whenever you like.
