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

### Network security

- The system has **no open ports** and accepts no inbound connections
- The only outbound connection is an SSH-authenticated Git push (optional)
- The system works fully offline — WiFi is optional
- No DNS queries except for Git push (if enabled)

## For contributors

If you fork this project, please maintain these privacy principles:
1. Never add cloud API calls for image processing
2. Never store full camera frames
3. Never disable EXIF stripping
4. Always crop to ROI before any storage or transmission
5. Document any new data flows in this file
