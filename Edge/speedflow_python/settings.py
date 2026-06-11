"""
speedflow_python/settings.py

Single source of truth for all runtime configuration.
Values are loaded from Edge/.env (via python-dotenv).

All other modules import constants from here — never call
os.environ directly with hardcoded fallback strings.

Usage:
    from .settings import CAMERAS_YML, ...
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Edge/ root — two levels up from this file (speedflow_python/settings.py)
ROOT = Path(__file__).resolve().parents[1]

# Load .env from Edge/.env (silent if missing — allows overrides via real env)
_env_path = ROOT / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

def _require(key: str) -> str:
    """Read env var; raise clearly if missing (no silent defaults)."""
    val = os.environ.get(key)
    if val is None:
        raise RuntimeError(
            f"Required env var '{key}' is not set. "
            f"Check {_env_path}"
        )
    return val

def _get(key: str, cast=str):
    """Read env var with cast; raise if missing."""
    return cast(_require(key))

# -----------------------------------------------------------
# Central Monitoring Server
# -----------------------------------------------------------
MONITOR_URL = os.environ.get("MONITOR_URL", "").strip()   # empty → disabled

# -----------------------------------------------------------
# Node identity
# -----------------------------------------------------------
NODE_ID        = _get("NODE_ID")
EDGE_ID        = _get("EDGE_ID")

# -----------------------------------------------------------
# Zenoh (P2P peer mode — no broker needed)
# -----------------------------------------------------------
ZENOH_QUEUE_MAXSIZE = _get("ZENOH_QUEUE_MAXSIZE", int)

# -----------------------------------------------------------
# Health Agent
# -----------------------------------------------------------
HEALTH_INTERVAL  = _get(\"HEALTH_INTERVAL\", float)
# Log the LoadScore line only once every N health cycles.
# e.g. HEALTH_LOG_EVERY=15 + HEALTH_INTERVAL=2.0 → log every 30 s.
# Set to 1 to log every cycle (original behaviour).
HEALTH_LOG_EVERY = int(os.environ.get(\"HEALTH_LOG_EVERY\", \"15\"))
TARGET_FPS       = _get(\"TARGET_FPS\", float)
FPS_STATS_FILE   = _get(\"FPS_STATS_FILE\")

# -----------------------------------------------------------
# RTSP Push (Centralized Streaming to Server)
# -----------------------------------------------------------
RTSP_PUSH_URL     = os.environ.get("RTSP_PUSH_URL", "").strip()
RTSP_PUSH_BITRATE = int(os.environ.get("RTSP_PUSH_BITRATE", "4000000"))

# -----------------------------------------------------------
# Network identity
# -----------------------------------------------------------
ADVERTISE_IP = os.environ.get("ADVERTISE_IP", "").strip()

# -----------------------------------------------------------
# Pipeline / Video
# -----------------------------------------------------------
VIDEO_FPS   = _get("VIDEO_FPS", float)
GPU_ID      = _get("GPU_ID", int)
MAX_STREAMS = _get("MAX_STREAMS", int)
MUX_WIDTH   = _get("MUX_WIDTH", int)
MUX_HEIGHT  = _get("MUX_HEIGHT", int)

VEHICLE_CLASS_IDS       = {2, 3, 5, 7}   # COCO: car, motorbike, bus, truck
LICENSE_PLATE_CLASS_IDS = {0}

# -----------------------------------------------------------
# Paths — relative to ROOT (Edge/)
# Stored as Path objects; absolute only where system requires it
# -----------------------------------------------------------
CAMERAS_YML     = ROOT / _get("CAMERAS_YML")
INFER_CONFIG    = ROOT / _get("INFER_CONFIG")
SGIE_CONFIG     = ROOT / _get("SGIE_CONFIG")
LPR_CONFIG      = ROOT / _get("LPR_CONFIG")
ANALYTICS_CFG   = ROOT / _get("ANALYTICS_CFG")
TRACKER_CFG     = ROOT / _get("TRACKER_CFG")
TRACKER_LPD_CFG = ROOT / _get("TRACKER_LPD_CFG")

# Absolute — DeepStream system library
TRACKER_LIB     = _get("TRACKER_LIB")

PATH_LOGS       = ROOT / "logs"
PATH_LOGS.mkdir(parents=True, exist_ok=True)

SPEED_LOG = str(ROOT / _get("SPEED_LOG"))
SNAP_DIR  = ROOT / _get("SNAP_DIR")
SNAP_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------
# Detection / Speed thresholds
# -----------------------------------------------------------
SPEED_LIMIT_KMH      = _get("SPEED_LIMIT_KMH", float)
JPEG_QUALITY         = _get("JPEG_QUALITY", int)
MAX_SNAPSHOT_PER_ID  = _get("MAX_SNAPSHOT_PER_ID", int)
MIN_WORLD_DISPL_M    = _get("MIN_WORLD_DISPL_M", float)
MAX_ABS_KMH          = _get("MAX_ABS_KMH", float)
BBOX_AREA_JUMP       = _get("BBOX_AREA_JUMP", float)
MIN_DET_CONF         = _get("MIN_DET_CONF", float)
MEDIAN_WINDOW        = _get("MEDIAN_WINDOW", int)

MIN_TRACK_AGE_FRAMES = int(VIDEO_FPS * 0.5)
