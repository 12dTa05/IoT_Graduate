#!/usr/bin/env bash
# ponytail: one-time h264 encoder for RTSP streaming (fixed 30fps, 2s IDR, no B-frames)
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 INPUT.mp4 [OUTPUT.mp4]" >&2
    exit 2
fi

input="$1"
output="${2:-$1}"

if [ ! -f "$input" ]; then
    echo "Error: input file not found: $input" >&2
    exit 1
fi

tmp="${output}.tmp.$$.mp4"
trap 'rm -f "$tmp"' EXIT INT TERM

ffmpeg -hide_banner -loglevel warning -y \
    -i "$input" \
    -map 0:v:0 \
    -an \
    -c:v libx264 \
    -preset ultrafast \
    -tune zerolatency \
    -pix_fmt yuv420p \
    -r 30 \
    -g 60 \
    -keyint_min 60 \
    -sc_threshold 0 \
    -bf 0 \
    -force_key_frames "expr:gte(t,n_forced*2)" \
    -x264-params "repeat-headers=1" \
    -b:v 2M \
    -maxrate 2M \
    -bufsize 4M \
    -movflags +faststart \
    -f mp4 \
    "$tmp"

mv -f "$tmp" "$output"
trap - EXIT INT TERM
printf 'Prepared: %s -> %s\n' "$input" "$output"
