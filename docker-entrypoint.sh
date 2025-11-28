#!/bin/bash
set -e

echo "=== Traffic Monitor Docker Entrypoint (File Display Mode) ==="

# Set LD_PRELOAD for libgomp
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libgomp.so.1

# Check DISPLAY variable
if [ -z "$DISPLAY" ]; then
    echo "WARNING: DISPLAY not set, using default: :0"
    export DISPLAY=":0"
fi

# Print configuration
echo "Configuration:"
echo "  DISPLAY: $DISPLAY"
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

# If no command specified, run run_file.py with video file
if [ $# -eq 0 ]; then
    if [ -z "$VIDEO_FILE" ]; then
        echo "ERROR: VIDEO_FILE environment variable is not set"
        echo "Example: VIDEO_FILE=/app/test_videos/test.mp4"
        exit 1
    fi
    
    if [ ! -f "$VIDEO_FILE" ]; then
        echo "ERROR: Video file not found: $VIDEO_FILE"
        exit 1
    fi
    
    echo "Starting run_file.py with video: $VIDEO_FILE"
    exec python3 /app/run_file.py "$VIDEO_FILE"
else
    # Execute provided command
    echo "Executing: $@"
    exec "$@"
fi
