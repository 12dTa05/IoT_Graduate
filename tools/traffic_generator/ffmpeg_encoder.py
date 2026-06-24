#!/usr/bin/env python3
"""Shared ffmpeg encoder detection — NVENC (GPU) with libx264 (CPU) fallback."""

import shutil
import subprocess
from typing import List


# ---------------------------------------------------------------------------
# NVENC capability detection
# ---------------------------------------------------------------------------

# Cache for the (slow) runtime encode-probe so it runs at most once per
# process — the 8-camera loop must not re-probe on every writer.
_nvenc_works_cache = None


def _has_nvenc() -> bool:
    """Fast check: is h264_nvenc compiled into the ffmpeg build?

    This only inspects ``ffmpeg -encoders`` output; it does NOT verify the
    NVIDIA driver / libcuda.so.1 is loadable at runtime.  Use
    ``_nvenc_works()`` for a real capability check.
    """
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


def _nvenc_works() -> bool:
    """Runtime check: can ffmpeg actually encode with h264_nvenc right now?

    Distinguishes "encoder is compiled in" from "encoder can run".  The
    latter fails on hosts where the NVIDIA driver / ``libcuda.so.1`` is
    absent or broken (e.g. a container without ``--gpus all``), even though
    ``ffmpeg -encoders`` lists ``h264_nvenc``.

    Performs a single-frame encode to the null muxer at 256x256 (above
    NVENC's minimum frame dimension to avoid a false negative) and returns
    True only on exit code 0.  Result is cached for the process lifetime.
    """
    global _nvenc_works_cache
    if _nvenc_works_cache is not None:
        return _nvenc_works_cache

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not _has_nvenc():
        _nvenc_works_cache = False
        return False

    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:d=1",
                "-frames:v", "1",
                "-c:v", "h264_nvenc",
                "-f", "null", "-",
            ],
            capture_output=True, timeout=15,
        )
        _nvenc_works_cache = (result.returncode == 0)
    except Exception:
        _nvenc_works_cache = False

    return _nvenc_works_cache


_nvenc_warned = False


def reset_encoder_cache() -> None:
    """Clear the cached NVENC probe result and one-shot warning flag.

    The NVENC capability probe is cached for the process lifetime (the
    8-camera loop must not re-probe per writer).  Call this if GPU
    availability may have changed and a fresh probe is wanted — e.g. a
    long-lived service that invokes the generator more than once.
    """
    global _nvenc_works_cache, _nvenc_warned
    _nvenc_works_cache = None
    _nvenc_warned = False


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
        ``"auto"`` (default) → NVENC if it can actually run (driver present),
        else libx264.  ``"nvenc"`` → force NVENC, falling back to libx264
        with a warning if the GPU/driver is unavailable.  ``"x264"`` → force
        CPU libx264.
    """
    global _nvenc_warned

    if video_encoder == "auto":
        use_nvenc = _nvenc_works()
    elif video_encoder == "nvenc":
        use_nvenc = _nvenc_works()
        if not use_nvenc and not _nvenc_warned:
            print("[ffmpeg] WARNING: --video-encoder nvenc requested but "
                  "h264_nvenc is unavailable (no GPU / driver / libcuda) — "
                  "falling back to libx264")
            _nvenc_warned = True
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
