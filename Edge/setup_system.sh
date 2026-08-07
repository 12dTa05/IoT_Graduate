#!/bin/bash
# =============================================================
# Edge/setup_system.sh — One-shot Edge node provisioning
#
# Provision a fresh Jetson Orin to run the SpeedFlow pipeline.
# Tested on: JetPack 6.x / DeepStream 7.1 / Ubuntu 22.04 aarch64
#
# Usage:
#   chmod +x setup_system.sh
#   sudo ./setup_system.sh          # interactive — prompts for node identity
#   sudo ./setup_system.sh jetson_B # non-interactive
#
# Node → IP → local camera streams → global Edge camera IDs:
#   jetson_A  192.168.212.20  Docker cam1,cam2  →  cam_01, cam_02
#   jetson_B  192.168.212.21  Docker cam3,cam4  →  cam_03, cam_04
#
# What it does:
#   1. Install system packages (GStreamer, build tools, Python, Docker)
#   2. Verify DeepStream SDK + install Python bindings
#   3. Install Python dependencies (requirements.txt)
#   4. Configure .env for this node
#   5. Generate Edge/configs/cameras.yml for this node's two cameras
#   6. Generate Camera/docker-compose.yml for this node's two camera services
#   7. Start Camera simulator (Docker)
#   8. Set up /dev/shm, /etc/hosts, runtime dirs
# =============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
info()  { printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m[ OK ]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[WARN]\033[0m %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "Run with sudo: sudo ./setup_system.sh"

# ──────────────────────────────────────────────────────────────
# 0. Determine node identity
# ──────────────────────────────────────────────────────────────
if [[ $# -ge 1 ]]; then
    NODE_ID="$1"
else
    echo ""
    echo "  Available node identities:"
    echo "    jetson_A  →  192.168.212.20  (cam1, cam2  →  cam_01, cam_02)"
    echo "    jetson_B  →  192.168.212.21  (cam3, cam4  →  cam_03, cam_04)"
    echo ""
    read -rp "  Enter NODE_ID for this device [jetson_A]: " NODE_ID
    NODE_ID="${NODE_ID:-jetson_A}"
fi

# CAM_LOCAL_NUMS : the two Docker service names (cam1 cam2 OR cam3 cam4)
# CAM_EDGE_IDS   : the two global Edge camera IDs (cam_01 cam_02 OR cam_03 cam_04)
case "$NODE_ID" in
    jetson_A)
        ADVERTISE_IP="192.168.212.20"
        CAM_LOCAL_NUMS=(1 2)
        CAM_EDGE_IDS=(cam_01 cam_02)
        ;;
    jetson_B)
        ADVERTISE_IP="192.168.212.21"
        CAM_LOCAL_NUMS=(3 4)
        CAM_EDGE_IDS=(cam_03 cam_04)
        ;;
    *)
        die "Unknown NODE_ID '$NODE_ID'. Expected jetson_A or jetson_B."
        ;;
esac

SERVER_IP="116.118.9.125"

info "Provisioning node: $NODE_ID ($ADVERTISE_IP)"
info "Local camera streams : cam${CAM_LOCAL_NUMS[0]}, cam${CAM_LOCAL_NUMS[1]}"
info "Global Edge camera IDs: ${CAM_EDGE_IDS[0]}, ${CAM_EDGE_IDS[1]}"
info "Server: $SERVER_IP"
echo ""

# ──────────────────────────────────────────────────────────────
# 1. System packages
# ──────────────────────────────────────────────────────────────
info "Installing system packages…"
apt-get update -qq

apt-get install -y -qq \
    build-essential cmake pkg-config \
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
    v4l-utils curl jq sshpass

ok "System packages installed"

# ──────────────────────────────────────────────────────────────
# 2. Docker (for Camera simulator)
# ──────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker…"
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker "${SUDO_USER:-$USER}" 2>/dev/null || true
    systemctl enable --now docker
    ok "Docker installed"
else
    ok "Docker already installed ($(docker --version | cut -d' ' -f3))"
fi

if ! docker compose version &>/dev/null; then
    info "Installing Docker Compose plugin…"
    apt-get install -y -qq docker-compose-plugin 2>/dev/null \
        || pip3 install -q docker-compose
    ok "Docker Compose installed"
else
    ok "Docker Compose already installed"
fi

# ──────────────────────────────────────────────────────────────
# 3. Verify DeepStream SDK
# ──────────────────────────────────────────────────────────────
DEEPSTREAM_DIR="/opt/nvidia/deepstream/deepstream"
if [ -d "$DEEPSTREAM_DIR" ]; then
    DS_VER=$(cat "$DEEPSTREAM_DIR/version" 2>/dev/null | head -1 || echo "unknown")
    ok "DeepStream SDK found: $DS_VER"

    # Install DeepStream Python bindings if available
    DS_PYTHON="$DEEPSTREAM_DIR/sources/deepstream_python_apps"
    if [ -d "$DS_PYTHON" ]; then
        DS_WHL=$(find "$DS_PYTHON" -name "*.whl" -path "*python*" 2>/dev/null | head -1)
        if [ -n "$DS_WHL" ]; then
            pip3 install -q "$DS_WHL" && ok "DeepStream Python bindings installed"
        fi
    fi
else
    warn "DeepStream SDK not found at $DEEPSTREAM_DIR"
    warn "Install JetPack + DeepStream first, then re-run this script."
fi

# ──────────────────────────────────────────────────────────────
# 4. Python dependencies
# ──────────────────────────────────────────────────────────────
info "Installing Python packages…"
pip3 install --upgrade pip setuptools wheel -q
pip3 install -r "$SCRIPT_DIR/requirements.txt" -q
ok "Python packages installed"

# ──────────────────────────────────────────────────────────────
# 5. Configure .env
# ──────────────────────────────────────────────────────────────
info "Writing .env for $NODE_ID…"
cat > "$SCRIPT_DIR/.env" <<EOF
# =============================================================
# Edge/.env — Generated by setup_system.sh ($(date +%F))
# Node: $NODE_ID ($ADVERTISE_IP)
# =============================================================

# --- Node identity ---
NODE_ID=$NODE_ID
EDGE_ID=$NODE_ID

# --- Load balancing experiment mode ---
# LOAD_POLICY: actual | predict_no_base | predict_with_base
LOAD_POLICY=actual
# LOAD_MODEL: formula | dl
LOAD_MODEL=formula

# --- Network ---
ADVERTISE_IP=$ADVERTISE_IP

# --- Central Monitoring Server ---
MONITOR_URL=http://$SERVER_IP:9090

# --- RTSP Push (→ MediaMTX on Server) ---
RTSP_PUSH_URL=rtsp://$SERVER_IP:8554/$NODE_ID
# 2.5 Mbps is sufficient for 1280x720@25fps monitoring quality.
RTSP_PUSH_BITRATE=2500000

# --- Zenoh (P2P peer mode) ---
ZENOH_QUEUE_MAXSIZE=1000

# --- Health Agent ---
HEALTH_INTERVAL=1.0
# Log the LoadScore line once every N health cycles.
HEALTH_LOG_EVERY=12
TARGET_FPS=27.0
FPS_STATS_FILE=/dev/shm/speedflow_fps.json

# --- Pipeline / Video ---
VIDEO_FPS=25.0
GPU_ID=0
MAX_STREAMS=8
# 1280x720: each tile is 640x360 at 4-cam tiling — saves ~44% GPU memory
# bandwidth and ~30% encoder bitrate vs 1920x1080.
MUX_WIDTH=1920
MUX_HEIGHT=1080

# --- AI / DeepStream paths (relative to Edge/) ---
INFER_CONFIG=configs/config_infer_primary_yolo11.txt
SGIE_CONFIG=configs/config_infer_secondary_lpd.txt
LPR_CONFIG=configs/config_infer_secondary_lpr.txt
ANALYTICS_CFG=configs/config_nvdsanalytics.txt
TRACKER_CFG=configs/config_tracker_NvDCF_perf.yml
TRACKER_LPD_CFG=configs/config_tracker_lpd.yml
CAMERAS_YML=configs/cameras.yml
TRACKER_LIB=/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so

# --- Detection / Speed thresholds ---
SPEED_LIMIT_KMH=80.0
# 82 is visually indistinguishable from 100 for license plate images
# but produces files ~3-4x smaller — critical for WS bandwidth on violations.
JPEG_QUALITY=82
MAX_SNAPSHOT_PER_ID=1
MIN_WORLD_DISPL_M=0.5
MAX_ABS_KMH=160.0
BBOX_AREA_JUMP=2.5
MIN_DET_CONF=0.45
MEDIAN_WINDOW=5

# --- Output paths (relative to Edge/) ---
SPEED_LOG=logs/speed_log.csv
SNAP_DIR=logs/overspeed_snaps
EOF
ok ".env written for $NODE_ID"

# ──────────────────────────────────────────────────────────────
# 6. Generate cameras.yml for this node's two cameras
#
# Mapping:
#   jetson_A: source_id 0→cam_01 (rtsp://$ADVERTISE_IP:8554/cam1)
#             source_id 1→cam_02 (rtsp://$ADVERTISE_IP:8554/cam2)
#   jetson_B: source_id 0→cam_03 (rtsp://$ADVERTISE_IP:8554/cam3)
#             source_id 1→cam_04 (rtsp://$ADVERTISE_IP:8554/cam4)
# ──────────────────────────────────────────────────────────────
CAMERAS_YML="$SCRIPT_DIR/configs/cameras.yml"
mkdir -p "$(dirname "$CAMERAS_YML")"
info "Generating cameras.yml for $NODE_ID (${CAM_EDGE_IDS[0]}, ${CAM_EDGE_IDS[1]})…"

cat > "$CAMERAS_YML" <<EOF
# =============================================================
# Edge/configs/cameras.yml
# GENERATED by setup_system.sh on $(date +%F) — DO NOT EDIT BY HAND
# Node : $NODE_ID ($ADVERTISE_IP)
# Cameras: ${CAM_EDGE_IDS[0]} (local cam${CAM_LOCAL_NUMS[0]}) · ${CAM_EDGE_IDS[1]} (local cam${CAM_LOCAL_NUMS[1]})
#
# Node → IP → local Docker stream → global camera ID:
#   jetson_A  192.168.212.20  cam1 → cam_01 · cam2 → cam_02
#   jetson_B  192.168.212.21  cam3 → cam_03 · cam4 → cam_04
#
# source_points and roi_polygon are in MUX resolution (1920×1080).
# If MUX_WIDTH/MUX_HEIGHT changes, scale all coordinates by the new ratio.
# =============================================================
max_streams: 8

tiler_mode: auto
tiler_rows: 4
tiler_cols: 2

cameras:
  ${CAM_EDGE_IDS[0]}:
    camera_id: "${CAM_EDGE_IDS[0]}"
    source_id: 0
    uri: "rtsp://$ADVERTISE_IP:8554/cam${CAM_LOCAL_NUMS[0]}"
    enabled: true
    name: "Camera ${CAM_LOCAL_NUMS[0]}"
    fps: 25.0
    speed_limit_kmh: 80.0
    homography:
      source_points:
        - [656, 26]
        - [1176, 32]
        - [1592, 1016]
        - [72, 1000]
      target_width: 15
      target_height: 60
    roi_polygon: [656, 26, 1176, 32, 1592, 1016, 72, 1000]
    output:
      record: true
      record_path: "output/${CAM_EDGE_IDS[0]}.mp4"

  ${CAM_EDGE_IDS[1]}:
    camera_id: "${CAM_EDGE_IDS[1]}"
    source_id: 1
    uri: "rtsp://$ADVERTISE_IP:8554/cam${CAM_LOCAL_NUMS[1]}"
    enabled: true
    name: "Camera ${CAM_LOCAL_NUMS[1]}"
    fps: 25.0
    speed_limit_kmh: 80.0
    homography:
      source_points:
        - [656, 26]
        - [1176, 32]
        - [1592, 1016]
        - [72, 1000]
      target_width: 15
      target_height: 60
    roi_polygon: [656, 26, 1176, 32, 1592, 1016, 72, 1000]
    output:
      record: true
      record_path: "output/${CAM_EDGE_IDS[1]}.mp4"
EOF
ok "cameras.yml generated"

# ──────────────────────────────────────────────────────────────
# 7. Generate Camera/docker-compose.yml for this node
#
# rtsp_server + exactly the two local camera services for this node.
# VIDEO_FILE is /videos/sample.mp4 (user replaces as needed).
# Camera/.env variables are NOT interpolated by compose at runtime,
# so all values are written literally here.
#
# Mapping:
#   jetson_A: services cam1, cam2  (RTSP paths /cam1, /cam2)
#   jetson_B: services cam3, cam4  (RTSP paths /cam3, /cam4)
# ──────────────────────────────────────────────────────────────
CAMERA_DIR="$PROJECT_DIR/Camera"
COMPOSE_FILE="$CAMERA_DIR/docker-compose.yml"
mkdir -p "$CAMERA_DIR"
info "Generating Camera/docker-compose.yml for $NODE_ID (cam${CAM_LOCAL_NUMS[0]}, cam${CAM_LOCAL_NUMS[1]})…"

cat > "$COMPOSE_FILE" <<EOF
# =============================================================
# Camera/docker-compose.yml
# GENERATED by Edge/setup_system.sh on $(date +%F) — DO NOT EDIT BY HAND
# Node : $NODE_ID ($ADVERTISE_IP)
# Services: rtsp_server · cam${CAM_LOCAL_NUMS[0]} · cam${CAM_LOCAL_NUMS[1]}
#
# Node → local camera services:
#   jetson_A  →  cam1, cam2
#   jetson_B  →  cam3, cam4
#
# To swap the video source, replace /videos/sample.mp4 with your file
# and restart: docker compose restart cam${CAM_LOCAL_NUMS[0]} cam${CAM_LOCAL_NUMS[1]}
# =============================================================
services:
  rtsp_server:
    image: bluenviron/mediamtx:latest
    container_name: rtsp_server
    ports:
      - "8554:8554"
    volumes:
      - ./mediamtx.yml:/mediamtx.yml
    restart: unless-stopped

  cam${CAM_LOCAL_NUMS[0]}:
    build: .
    container_name: cam${CAM_LOCAL_NUMS[0]}
    depends_on:
      - rtsp_server
    environment:
      - VIDEO_FILE=/videos/sample.mp4
      - RTSP_URL=rtsp://rtsp_server:8554/cam${CAM_LOCAL_NUMS[0]}
    volumes:
      - ./videos:/videos
    restart: unless-stopped

  cam${CAM_LOCAL_NUMS[1]}:
    build: .
    container_name: cam${CAM_LOCAL_NUMS[1]}
    depends_on:
      - rtsp_server
    environment:
      - VIDEO_FILE=/videos/sample.mp4
      - RTSP_URL=rtsp://rtsp_server:8554/cam${CAM_LOCAL_NUMS[1]}
    volumes:
      - ./videos:/videos
    restart: unless-stopped
EOF
ok "Camera/docker-compose.yml generated"

# ──────────────────────────────────────────────────────────────
# 8. Runtime directories
# ──────────────────────────────────────────────────────────────
info "Setting up runtime environment…"
mkdir -p "$SCRIPT_DIR/logs"
mkdir -p "$SCRIPT_DIR/logs/overspeed_snaps"
touch /dev/shm/speedflow_fps.json 2>/dev/null || warn "Cannot create /dev/shm FPS file"

# ──────────────────────────────────────────────────────────────
# 9. /etc/hosts peer hints
# ──────────────────────────────────────────────────────────────
PEER_HINT="# IoT_Graduate peer nodes"
if ! grep -q "$PEER_HINT" /etc/hosts 2>/dev/null; then
    info "Adding peer hints to /etc/hosts…"
    cat >> /etc/hosts <<'HOSTS'

# IoT_Graduate peer nodes
192.168.212.20 jetson_A
192.168.212.21 jetson_B
HOSTS
    ok "/etc/hosts updated"
fi

# ──────────────────────────────────────────────────────────────
# 10. Start Camera simulator
# ──────────────────────────────────────────────────────────────
if [ -d "$CAMERA_DIR" ] && [ -f "$COMPOSE_FILE" ]; then
    info "Starting Camera simulator (Docker)…"
    cd "$CAMERA_DIR"
    docker compose up -d --build 2>/dev/null || docker-compose up -d --build 2>/dev/null || warn "Camera Docker start failed"
    ok "Camera simulator running"
    cd "$SCRIPT_DIR"
else
    warn "Camera/ directory not found — skip camera simulator"
fi

# ──────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────
printf "\n"
ok "═══════════════════════════════════════════════"
ok "  $NODE_ID ($ADVERTISE_IP) provisioned!"
ok "═══════════════════════════════════════════════"
echo ""
info "Camera streams : rtsp://$ADVERTISE_IP:8554/cam${CAM_LOCAL_NUMS[0]}  rtsp://$ADVERTISE_IP:8554/cam${CAM_LOCAL_NUMS[1]}"
info "Edge camera IDs: ${CAM_EDGE_IDS[0]}  ${CAM_EDGE_IDS[1]}"
echo ""
info "Run the pipeline:"
info "  cd $SCRIPT_DIR && python3 main.py --mode rtsp_push"
echo ""
