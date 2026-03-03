# Hardware Setup Guide

## Bill of Materials

| Item | Approx. Cost | Notes |
|------|-------------|-------|
| Raspberry Pi 1 Model B | ~$10 (used) | 512MB RAM version required |
| Pi Camera Module v1.3 | ~$15 | Or any USB webcam |
| 64GB microSD | ~$10 | Class 10 / A1 recommended; photos stored locally |
| 5V 1A power supply | ~$5 | Micro-USB, stable output |
| Weatherproof enclosure | ~$10-20 | IP65+ for outdoor use |
| Bird feeder | ~$10-30 | Platform style works best (flat surface) |
| Camera mount | ~$5 | Flexible gooseneck or 3D-printed bracket |

**Total: ~$55-90**

## Camera Positioning

```
        Side View                    Top View

     Camera [C]                   ┌─────────────┐
        │  \                      │   Feeder    │
        │   \ 30-45°              │  ┌───────┐  │
        │    \                    │  │ perch  │  │
        │     \                   │  └───────┘  │
     ───┴──────[Feeder]───        │      ↑      │
                                  │    Camera    │
                                  └─────────────┘
```

- **Distance**: 30-50cm from feeder perch
- **Angle**: 30-45° downward
- **Focus**: Pre-focus on the perch area
- **Lighting**: Avoid direct sunlight on lens; north-facing is ideal
- **Background**: Plain or distant background helps motion detection

## Weatherproofing

For outdoor deployment:
1. Place the Pi in an IP65 enclosure (or a waterproof food container with silicone seal)
2. Route the camera ribbon cable through a grommet
3. Protect the camera lens with a small clear acrylic dome
4. Ensure the enclosure has passive ventilation (the Pi can overheat in sealed boxes in summer)
5. Use a waterproof cable gland for the power cable entry
6. Consider a small desiccant pack inside the enclosure

## Camera Connection

### Pi Camera Module
1. Locate the CSI port (between the Ethernet and HDMI ports on Pi 1B)
2. Lift the plastic clip gently
3. Insert the ribbon cable with the blue side facing the Ethernet port
4. Press the clip back down
5. Enable in `raspi-config → Interface Options → Camera`

### USB Webcam
1. Plug into any USB port
2. Verify with `ls /dev/video*` (should show `/dev/video0`)
3. Set `camera.type: "usb"` in `settings.yaml`

## Power Considerations

The Pi 1B draws ~1.5W idle, ~3.5W under load. For off-grid setups:
- A 10,000mAh USB battery bank can run the Pi for ~15-20 hours
- A small 5W solar panel + charge controller enables indefinite operation
- Consider a UPS HAT for graceful shutdown on power loss
