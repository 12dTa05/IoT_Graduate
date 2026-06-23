#!/usr/bin/env python3
"""Shared ffmpeg encoder detection — NVENC (GPU) with libx264 (CPU) fallback."""

import shutil
import subprocess
from typing import List


def _has_nvenc() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "h264_nvenc" in result.stdout
    except Exception:
        return False


def build_ffmpeg_cmd(out_file: str, width: int, height: int, fps: float,
                     video_encoder: str = "auto") -> List[str]:
    """Return the ffmpeg command list for piping raw BGR frames into an MP4.

    Parameters
    ----------
    out_file : str
        Output MP4 path.
    width, height : int
        Frame dimensions in pixels.
    fps : float
        Frame rate.
    video_encoder : str
        ``"auto"`` (default) → NVENC if available, else libx264.
        ``"nvenc"`` → force NVENC (error if unavailable).
        ``"x264"``  → force CPU libx264.
    """
    if video_encoder == "auto":
        use_nvenc = _has_nvenc()
    elif video_encoder == "nvenc":
        use_nvenc = True
        if not _has_nvenc():
            print("[ffmpeg] WARNING: --video-encoder nvenc requested but "
                  "h264_nvenc not found — falling back to libx264")
            use_nvenc = False
    else:  # x264
        use_nvenc = False

    if use_nvenc:
        codec_args = [
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-tune", "ll",
            "-rc", "vbr",
            "-cq", "23",
        ]
    else:
        codec_args = [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
        ]

    return [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{width}x{height}",
        "-pix_fmt", "bgr24",
        "-r", str(fps), "-i", "pipe:0",
        *codec_args,
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        str(out_file),
    ]