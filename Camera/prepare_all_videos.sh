#!/usr/bin/env bash
set -euo pipefail

root=${1:-videos}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
prepare_one="$script_dir/prepare_video.sh"

mapfile -d '' videos < <(
    find "$root" -type f -iname '*.mp4' ! -name '*.encoded.mp4' -print0
)

if ((${#videos[@]} == 0)); then
    printf 'No unencoded MP4 files found under %s\n' "$root"
    exit 0
fi

for input in "${videos[@]}"; do
    encoded="${input}.encoded.mp4"

    # Keep reruns idempotent: files already matching the runtime stream-copy
    # contract need no second encode.
    if ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_name,avg_frame_rate,has_b_frames \
        -of csv=p=0 "$input" 2>/dev/null \
        | grep -q '^h264,30/1,0$'; then
        printf 'Already prepared: %s\n' "$input"
        continue
    fi

    printf 'Encoding once: %s\n' "$input"
    "$prepare_one" "$input" "$encoded"

    # Replace atomically only after a complete, successful encode.
    mv -- "$encoded" "$input"
    printf 'Replaced: %s\n' "$input"
done

printf 'Prepared %d video(s); original names preserved.\n' "${#videos[@]}"
