#!/bin/bash
# Smart Bird Feeder — One-shot setup script
# Run: chmod +x setup.sh && ./setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

echo "================================================"
echo " Smart Bird Feeder Setup"
echo " Raspberry Pi 1B Edition"
echo "================================================"
echo ""

# 1. System packages
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
# libatlas-base-dev was removed in Debian Trixie (RPi OS 2025+); use libopenblas-dev
BLAS_PKG="libatlas-base-dev"
if ! apt-cache show libatlas-base-dev >/dev/null 2>&1; then
    echo "  Note: libatlas-base-dev unavailable (Debian Trixie+), using libopenblas-dev"
    BLAS_PKG="libopenblas-dev"
fi

sudo apt-get install -y -qq \
    python3-pip python3-venv python3-opencv python3-numpy \
    python3-pil sqlite3 git "$BLAS_PKG" \
    python3-picamera2 2>/dev/null || \
    sudo apt-get install -y -qq python3-picamera 2>/dev/null || \
    echo "  Note: No Pi camera library found — USB camera will be used"

# 2. Python virtual environment
echo ""
echo "[2/6] Setting up Python virtual environment..."
if [ ! -d "$PROJECT_DIR/venv" ]; then
    python3 -m venv --system-site-packages "$PROJECT_DIR/venv"
fi
source "$PROJECT_DIR/venv/bin/activate"

# 3. Python dependencies
echo ""
echo "[3/6] Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$PROJECT_DIR/requirements.txt"

# 4. Create directories
echo ""
echo "[4/6] Creating data directories..."
mkdir -p "$PROJECT_DIR/data"/{captures,classified,stats}
mkdir -p "$PROJECT_DIR/models"

# 5. Download model
echo ""
echo "[5/6] Model setup..."
python3 "$PROJECT_DIR/download_model.py"

# 6. systemd services (optional)
echo ""
echo "[6/6] Setting up systemd services..."
read -p "Install systemd services for auto-start on boot? [y/N]: " install_services
if [[ "$install_services" =~ ^[Yy]$ ]]; then
    # Update paths in service files
    for service in bird-capture bird-classify; do
        sed "s|/home/pi/smart-bird-feeder|$PROJECT_DIR|g" \
            "$PROJECT_DIR/${service}.service" | \
            sudo tee "/etc/systemd/system/${service}.service" > /dev/null
    done
    sudo systemctl daemon-reload
    sudo systemctl enable bird-capture bird-classify
    echo "  Services installed. Start with:"
    echo "    sudo systemctl start bird-capture bird-classify"
else
    echo "  Skipped. You can run manually:"
    echo "    python3 src/motion_detector.py &"
    echo "    python3 src/classifier.py &"
fi

echo ""
echo "================================================"
echo " Setup complete!"
echo ""
echo " Quick start:"
echo "   cd $PROJECT_DIR"
echo "   source venv/bin/activate"
echo "   python3 src/motion_detector.py"
echo ""
echo " In another terminal:"
echo "   python3 src/classifier.py"
echo "================================================"
