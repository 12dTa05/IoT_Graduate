#!/bin/sh

VIDEO_FILE=${VIDEO_FILE:-/videos/video.mp4}
RTSP_URL=${RTSP_URL:-rtsp://rtsp_server:8554/live}

# ── Bandwidth tuning ─────────────────────────────────────────────────────────
# TARGET_FPS: 30 fps is sufficient for vehicle speed detection (33ms resolution
#   at 80 km/h gives sub-meter accuracy). Halving from 60→30 saves ~58% bitrate.
# VIDEO_BITRATE: hard cap at 1.5 Mbps per camera (4 cams = 6 Mbps total LAN).
#   H.264 at 1920×1080@30fps is very readable at this rate for monitoring.
# VIDEO_MAXRATE / VIDEO_BUFSIZE: VBR headroom — allows brief peaks for scene cuts.
# ─────────────────────────────────────────────────────────────────────────────
TARGET_FPS=${TARGET_FPS:-30}
VIDEO_BITRATE=${VIDEO_BITRATE:-1500k}
VIDEO_MAXRATE=${VIDEO_MAXRATE:-2000k}
VIDEO_BUFSIZE=${VIDEO_BUFSIZE:-3000k}

sleep 5

exec ffmpeg -re -stream_loop -1 \
    -i "$VIDEO_FILE" \
    -vf "fps=${TARGET_FPS}" \
    -c:v copy \
    -preset ultrafast \
    -tune zerolatency \
    -b:v "${VIDEO_BITRATE}" \
    -maxrate "${VIDEO_MAXRATE}" \
    -bufsize "${VIDEO_BUFSIZE}" \
    -g $((TARGET_FPS * 2)) \
    -an \
    -f rtsp \
    -rtsp_transport tcp \
    "$RTSP_URL"
