#!/bin/sh

VIDEO_FILE=${VIDEO_FILE:-/videos/video.mp4}
RTSP_URL=${RTSP_URL:-rtsp://rtsp_server:8554/live}

sleep 5

# Source videos are pre-encoded with 2s keyframes; copy avoids persistent CPU encoding.
exec ffmpeg -re -stream_loop -1 \
    -i "$VIDEO_FILE" \
    -c:v copy \
    -an \
    -f rtsp \
    -rtsp_transport tcp \
    "$RTSP_URL"
