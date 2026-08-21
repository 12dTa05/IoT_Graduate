#!/bin/sh

VIDEO_FILE=${VIDEO_FILE:-/videos/video.mp4}
RTSP_URL=${RTSP_URL:-rtsp://rtsp_server:8554/live}

sleep 5

# Input must be prepared once with prepare_video.sh. Runtime must only copy the
# already-clean H.264 stream; encoding here would repeat on every restart.
exec ffmpeg -re -stream_loop -1 \
    -i "$VIDEO_FILE" \
    -c:v copy \
    -an \
    -f rtsp \
    -rtsp_transport tcp \
    "$RTSP_URL"
