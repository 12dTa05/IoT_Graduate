#!/bin/bash
#
# Edge/setup_system.sh — One-shot Edge node provisioning
#
# Run ONCE on each Jetson before launching the pipeline:
#   chmod +x setup_system.sh && sudo ./setup_system.sh
#
# What it does:
#   1. Installs system packages (GStreamer, build tools, Python bindings)
#   2. Installs Python dependencies from requirements.txt
#   3. Installs Zenoh (C library + Python bindings via pip)
#   4. Verifies DeepStream SDK is present
#   5. Sets up /dev/shm for FPS stats, /etc/hosts for peer discovery hints
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────
info()  { printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m[ OK ]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[WARN]\033[0m %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*" >&2; exit 1; }

# ──────────────────────────────────────────────────────────────────
# 1. System packages
# ──────────────────────────────────────────────────────────────────
info "Updating package lists…"
apt-get update -qq

info "Installing system dependencies…"
apt-get install -y -qq \
    build-essential \
    cmake \
    pkg-config \
    \
    libglib2.0-dev \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstrtspserver-1.0-dev \
    \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    \
    python3-gi \
    python3-gi-cairo \
    python3-dev \
    python3-pip \
    python3-venv \
    \
    v4l-utils \
    curl \
    jq

ok "System packages installed"

# ──────────────────────────────────────────────────────────────────
# 2. Verify DeepStream SDK
# ──────────────────────────────────────────────────────────────────
DEEPSTREAM_DIR="/opt/nvidia/deepstream/deepstream"
if [ -d "$DEEPSTREAM_DIR" ]; then
    DS_VER="$("$DEEPSTREAM_DIR/sources/tools/gst-nvdsinfer/nvdsinfer" --version 2>/dev/null \
              || echo "unknown")"
    ok "DeepStream SDK found: $DS_VER"
else
    warn "DeepStream SDK not found at $DEEPSTREAM_DIR"
    warn "Install JetPack / DeepStream first, then re-run this script."
fi

# ──────────────────────────────────────────────────────────────────
# 3. Python dependencies
# ──────────────────────────────────────────────────────────────────
info "Installing Python packages…"

pip3 install --upgrade pip setuptools wheel -q

# Core ML / CV
pip3 install -q \
    numpy>=1.19.0 \
    PyYAML>=5.4.0 \
    opencv-python-headless>=4.5.0 \
    python-dotenv>=1.0.0 \
    msgpack

# WebSocket client for MonitorClient
pip3 install -q websocket-client>=1.6.0

# File watcher for dynamic camera config
pip3 install -q watchdog>=3.0.0

# aiohttp for PeerOrchestrator REST API (fallback)
pip3 install -q aiohttp>=3.13.0

# Jetson hardware stats (jtop)
pip3 install -q jetson-stats>=4.0.0 2>/dev/null && ok "jetson-stats installed" \
    || warn "jetson-stats not available (run on Jetson only)"

# DeepStream Python bindings (from SDK)
DS_PYTHON="$DEEPSTREAM_DIR/sources/deepstream_python_apps"
if [ -d "$DS_PYTHON" ]; then
    DS_PYDIST=$(find "$DS_PYTHON" -name "*.whl" -path "*python*" 2>/dev/null | head -1)
    if [ -n "$DS_PYDIST" ]; then
        pip3 install -q "$DS_PYDIST" && ok "DeepStream Python bindings installed"
    fi
fi

ok "Python packages installed"

# ──────────────────────────────────────────────────────────────────
# 4. Zenoh
# ──────────────────────────────────────────────────────────────────
info "Installing Eclipse Zenoh…"

# Python bindings
pip3 install -q eclipse-zenoh>=1.0.0 \
    && ok "eclipse-zenoh Python bindings installed" \
    || die "eclipse-zenoh install failed (try --break-system-packages)"

# ──────────────────────────────────────────────────────────────────
# 5. Runtime setup
# ──────────────────────────────────────────────────────────────────
info "Configuring runtime environment…"

# Shared memory for FPS stats
FPS_SHM="/dev/shm/speedflow_fps.json"
touch "$FPS_SHM" 2>/dev/null || warn "Cannot create $FPS_SHM (run without sudo?)"

# Ensure Edge/logs/ exists
mkdir -p "$SCRIPT_DIR/logs"

# ──────────────────────────────────────────────────────────────────
# 6. Optional: /etc/hosts peer hints
# ──────────────────────────────────────────────────────────────────
PEER_HINT="# IoT_Graduate peer nodes"
if ! grep -q "$PEER_HINT" /etc/hosts 2>/dev/null; then
    cat >> /etc/hosts <<'EOF'

# IoT_Graduate peer nodes
192.168.212.20 jetson_A
192.168.212.21 jetson_B
EOF
    ok "/etc/hosts updated with peer hints"
fi

# ──────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────
printf "\n"
ok "Edge node setup complete"
info "Next step: python3 main.py --mode rtsp_push"
