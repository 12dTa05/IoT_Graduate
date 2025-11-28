#!/bin/bash
set -e

# Default values
VIDEO_FILE="${VIDEO_FILE:-/app/videos/input.mp4}"
OUTPUT_FILE="${OUTPUT_FILE:-/app/output/output.mp4}"
HOMO_YML="${HOMO_YML:-/app/configs/points_source_target.yml}"
SPEED_LIMIT="${SPEED_LIMIT:-60}"

echo "================================================"
echo "DeepStream Traffic Monitoring - Video Processing"
echo "================================================"
echo "Video Input: $VIDEO_FILE"
echo "Output File: $OUTPUT_FILE"
echo "Homography Config: $HOMO_YML"
echo "Speed Limit: $SPEED_LIMIT km/h"
echo "================================================"

# Check if video file exists
if [ ! -f "$VIDEO_FILE" ]; then
    echo "ERROR: Video file not found: $VIDEO_FILE"
    echo ""
    echo "Usage:"
    echo "  docker run -v /path/to/video.mp4:/app/videos/input.mp4 iot-traffic-monitor"
    echo ""
    echo "Or set VIDEO_FILE environment variable:"
    echo "  docker run -e VIDEO_FILE=/app/videos/myvideo.mp4 -v /path/to/video.mp4:/app/videos/myvideo.mp4 iot-traffic-monitor"
    exit 1
fi

# Create output directory
mkdir -p "$(dirname "$OUTPUT_FILE")"
mkdir -p /app/logs/overspeed_snaps

# Run the processing
cd /app
exec python3 run_file.py "$VIDEO_FILE" --homo "$HOMO_YML" --out "$OUTPUT_FILE"
