# Privacy Design Document

## Philosophy

This project follows **privacy-by-design** principles. No personal data is ever collected, stored, or transmitted. The system is designed so that privacy protection is the default, not an option.

## Data Flow & Privacy Controls

### What the camera sees vs. what is saved

The camera captures a full frame (640×480), but the system **immediately crops to the Region of Interest (ROI)** — a configurable rectangle around the bird feeder. Everything outside this rectangle is discarded in memory and never touches the filesystem.

```
Full camera frame (640×480)         What is actually saved
┌──────────────────────────┐        ┌──────────────┐
│                          │        │              │
│    ┌──────────────┐      │   →    │  [bird at    │
│    │  ROI (feeder)│      │        │   feeder]    │
│    │              │      │        │              │
│    └──────────────┘      │        └──────────────┘
│                          │        (only this region)
└──────────────────────────┘
```

### Layer-by-layer protections

| Layer | Protection | Default |
|-------|-----------|---------|
| 1. Capture | Only the ROI is ever saved | ON |
| 2. Blur | Areas outside ROI are blurred (backup if crop fails) | ON |
| 3. EXIF | All metadata stripped (GPS, timestamps, camera info) | ON |
| 4. Resize | Images downsized to remove fine detail | ON |
| 5. Storage | Only classified bird crops stored; queue is cleared | ON |
| 6. Network | No cloud APIs; outbound Git push only | ON |
| 7. Upload | Only bird crops + stats pushed; never raw frames | ON |

### What is never stored

- Full camera frames
- EXIF metadata (GPS, timestamps, camera serial numbers)
- Any image data outside the feeder ROI
- Network traffic logs
- IP addresses or device identifiers

### Storage model

All photos and data are stored **locally on the SD card** by default. GitHub sync is disabled out of the box. You control when and how data leaves the device — either by manually copying files via `scp`, or by optionally enabling the GitHub sync feature.

### Local web server

The built-in web server (`webserver.py`) listens on port 8080 and is intended for **LAN access only**. It serves:
- A photo gallery of classified bird images
- A system health dashboard
- JSON API endpoints for stats and health data

The web server does not:
- Require authentication (it trusts the local network)
- Expose any data to the internet (bind to 0.0.0.0 on LAN only)
- Accept uploads or modifications — it is strictly read-only

If your Pi is directly exposed to the internet (not behind a router/firewall), you should either disable the web server or change the bind address to `127.0.0.1` in `settings.yaml`.

### Network security

- The web server is **LAN-only** — no internet-facing ports
- GitHub sync is **disabled by default** — all data stays on the SD card
- If GitHub sync is enabled, the only outbound connection is an SSH-authenticated Git push
- The system works fully offline — WiFi is optional
- No DNS queries unless GitHub sync is enabled

## For contributors

If you fork this project, please maintain these privacy principles:
1. Never add cloud API calls for image processing
2. Never store full camera frames
3. Never disable EXIF stripping
4. Always crop to ROI before any storage or transmission
5. Document any new data flows in this file
