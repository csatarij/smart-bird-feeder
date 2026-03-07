#!/bin/bash
# Smart Bird Feeder — Idempotent setup script
# Safe to re-run multiple times on the same system.
# Run: chmod +x setup.sh && ./setup.sh
#
# Options:
#   --no-services    Skip systemd service installation entirely

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
INSTALL_SERVICES=true

# Parse arguments
for arg in "$@"; do
    case "$arg" in
        --no-services) INSTALL_SERVICES=false ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

echo "================================================"
echo " Smart Bird Feeder Setup"
echo " Raspberry Pi 1B Edition"
echo "================================================"
echo ""

# 1. System packages — skip already-installed ones
echo "[1/6] Installing system packages..."
PACKAGES="python3-pip python3-venv python3-opencv python3-numpy python3-pil sqlite3 git"

# Pick the right BLAS package
if apt-cache show libatlas-base-dev >/dev/null 2>&1; then
    PACKAGES="$PACKAGES libatlas-base-dev"
else
    echo "  Note: libatlas-base-dev unavailable (Debian Trixie+), using libopenblas-dev"
    PACKAGES="$PACKAGES libopenblas-dev"
fi

# Only install packages that aren't already installed
MISSING=""
for pkg in $PACKAGES; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        MISSING="$MISSING $pkg"
    fi
done

if [ -n "$MISSING" ]; then
    echo "  Installing:$MISSING"
    sudo apt-get update -qq
    sudo apt-get install -y -qq $MISSING
else
    echo "  All system packages already installed."
fi

# Pi camera library (optional, don't fail if unavailable)
if ! dpkg -s python3-picamera2 >/dev/null 2>&1; then
    sudo apt-get install -y -qq python3-picamera2 2>/dev/null || \
        sudo apt-get install -y -qq python3-picamera 2>/dev/null || \
        echo "  Note: No Pi camera library found — USB camera will be used"
fi

# 2. Python virtual environment — create only if missing
echo ""
echo "[2/6] Setting up Python virtual environment..."
if [ ! -d "$PROJECT_DIR/venv" ]; then
    python3 -m venv --system-site-packages "$PROJECT_DIR/venv"
    echo "  Created new virtual environment."
else
    echo "  Virtual environment already exists."
fi
source "$PROJECT_DIR/venv/bin/activate"

# 3. Python dependencies — pip handles already-installed packages
echo ""
echo "[3/6] Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$PROJECT_DIR/requirements.txt"

# 4. Create directories
echo ""
echo "[4/6] Creating data directories..."
mkdir -p "$PROJECT_DIR/data"/{captures,classified,stats}
mkdir -p "$PROJECT_DIR/models"

# 5. Download model (only if not already present)
echo ""
echo "[5/6] Model setup..."
if [ -f "$PROJECT_DIR/models/bird_model.tflite" ]; then
    echo "  Model already installed. To reconfigure, run: python3 download_model.py"
else
    python3 "$PROJECT_DIR/download_model.py"
fi

# 6. systemd services
echo ""
echo "[6/6] Setting up systemd services..."
if [ "$INSTALL_SERVICES" = false ]; then
    echo "  Skipped (--no-services flag)."
else
    # Always install/update service files (idempotent — path substitution + copy)
    SERVICES_CHANGED=false
    for service in bird-capture bird-classify bird-webserver; do
        SRC="$PROJECT_DIR/${service}.service"
        DEST="/etc/systemd/system/${service}.service"
        RENDERED=$(sed "s|/home/pi/smart-bird-feeder|$PROJECT_DIR|g" "$SRC")

        # Only copy if content changed (avoids unnecessary daemon-reload)
        if [ ! -f "$DEST" ] || [ "$(cat "$DEST")" != "$RENDERED" ]; then
            echo "$RENDERED" | sudo tee "$DEST" > /dev/null
            SERVICES_CHANGED=true
            echo "  Updated ${service}.service"
        else
            echo "  ${service}.service already up to date."
        fi
    done

    if [ "$SERVICES_CHANGED" = true ]; then
        sudo systemctl daemon-reload
    fi

    # Ask whether to enable auto-start (only if not already enabled)
    ALL_ENABLED=true
    for service in bird-capture bird-classify bird-webserver; do
        if ! systemctl is-enabled --quiet "$service" 2>/dev/null; then
            ALL_ENABLED=false
            break
        fi
    done

    if [ "$ALL_ENABLED" = true ]; then
        echo "  All services already enabled for auto-start."
    else
        echo ""
        read -p "  Enable services to auto-start at boot? [Y/n]: " enable_services
        if [[ ! "$enable_services" =~ ^[Nn]$ ]]; then
            sudo systemctl enable bird-capture bird-classify bird-webserver
            echo "  Services enabled for auto-start at boot."
        else
            echo "  Services installed but not enabled for auto-start."
        fi
    fi

    echo ""
    echo "  Service commands:"
    echo "    sudo systemctl start bird-capture bird-classify bird-webserver"
    echo "    sudo systemctl status bird-capture bird-classify bird-webserver"
    echo "    sudo systemctl stop bird-capture bird-classify bird-webserver"
fi

echo ""
echo "================================================"
echo " Setup complete!"
echo ""
echo " Quick start:"
echo "   cd $PROJECT_DIR"
echo "   source venv/bin/activate"
echo "   python3 webserver.py"
echo ""
echo " Then open http://<pi-ip>:8080/onboarding in your browser"
echo " to run the guided setup wizard."
echo ""
echo " Or start everything manually:"
echo "   python3 motion_detector.py &"
echo "   python3 classifier.py &"
echo "   python3 webserver.py &"
echo "================================================"
