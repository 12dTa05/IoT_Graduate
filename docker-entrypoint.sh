#!/bin/bash
set -e

echo "=== Traffic Monitor Docker Entrypoint ==="

# Set LD_PRELOAD for libgomp
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libgomp.so.1

# Validate required environment variables
if [ -z "$VIDEO_SOURCE" ]; then
    echo "ERROR: VIDEO_SOURCE environment variable is not set"
    echo "Example: VIDEO_SOURCE=rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101"
    exit 1
fi

if [ -z "$WEBRTC_SERVER" ]; then
    echo "WARNING: WEBRTC_SERVER not set, using default: 192.168.0.158"
    export WEBRTC_SERVER="192.168.0.158"
fi

if [ -z "$WEBRTC_ROOM" ]; then
    echo "WARNING: WEBRTC_ROOM not set, using default: demo"
    export WEBRTC_ROOM="demo"
fi

if [ -z "$CONFIG_FILE" ]; then
    echo "WARNING: CONFIG_FILE not set, using default: /app/configs/config_cam.txt"
    export CONFIG_FILE="/app/configs/config_cam.txt"
fi

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: Config file not found: $CONFIG_FILE"
    exit 1
fi

# Print configuration
echo "Configuration:"
echo "  VIDEO_SOURCE: $VIDEO_SOURCE"
echo "  WEBRTC_SERVER: $WEBRTC_SERVER"
echo "  WEBRTC_ROOM: $WEBRTC_ROOM"
echo "  CONFIG_FILE: $CONFIG_FILE"
echo "  LD_PRELOAD: $LD_PRELOAD"

# Check DeepStream installation
if [ ! -d "/opt/nvidia/deepstream/deepstream" ]; then
    echo "ERROR: DeepStream not found at /opt/nvidia/deepstream/deepstream"
    exit 1
fi

echo "DeepStream path: /opt/nvidia/deepstream/deepstream"

# Check GStreamer plugins
echo "Checking GStreamer plugins..."
gst-inspect-1.0 nvstreammux > /dev/null 2>&1 || {
    echo "ERROR: nvstreammux plugin not found"
    exit 1
}
echo "GStreamer DeepStream plugins OK"

# Create output directories
mkdir -p /app/logs/overspeed_snaps
mkdir -p /app/output

# If no command specified, run run_webrtc.py with environment variables
if [ $# -eq 0 ]; then
    echo "Starting run_webrtc.py..."
    exec python3 /app/run_webrtc.py "$VIDEO_SOURCE" \
        --server "$WEBRTC_SERVER" \
        --room "$WEBRTC_ROOM" \
        --cfg "$CONFIG_FILE"
else
    # Execute provided command
    echo "Executing: $@"
    exec "$@"
fi
