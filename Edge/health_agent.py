#!/usr/bin/env python3
"""
Edge/health_agent.py

Health Agent — Collect hardware metrics and publish via Zenoh (peer mode).

Reads all configuration from Edge/.env via speedflow_python.settings.
No default values in this file — all values must be set in .env.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
import threading
import collections
from pathlib import Path
from typing import Dict, Optional, Deque, Tuple

import msgpack

from speedflow_python.zenoh_session import make_session

# Load settings from .env (must run from Edge/ or have Edge/ in path)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from speedflow_python.settings import (
    NODE_ID,
    HEALTH_INTERVAL,
    HEALTH_LOG_EVERY,
    TARGET_FPS,
    FPS_STATS_FILE,
    MONITOR_URL,
    ADVERTISE_IP,
    LOAD_POLICY,
    LOAD_MODEL,
    TELEMETRY_INTERVAL,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("health_agent")


# ---------------------------------------------------------------------------
# Payload freshness/integrity tracking (module-level state)
# ---------------------------------------------------------------------------
# Committed pipeline session_id and last seen sequence number.
# Reset on session change (pipeline restart) or when the payload goes stale
# beyond the operational window.
_state_session_id: str = ""
_state_last_seq: int = -1

# ponytail: bounded age derived from 1s operational cadence.
# 3 * max(TELEMETRY_INTERVAL, HEALTH_INTERVAL) gives ~3 s of headroom
# for a 1 s cadence.  If the atomic payload writer stalls for 3+ s,
# report the pipeline as unavailable rather than replaying stale data.
_STALE_MAX_AGE_S = 3.0 * max(TELEMETRY_INTERVAL, HEALTH_INTERVAL)


# ---------------------------------------------------------------------------
# Unified Payload Reader
# ---------------------------------------------------------------------------

def _read_payload() -> Optional[dict]:
    """
    Read and parse the unified JSON payload written atomically by SpeedProbe.
    Returns the full parsed dict or None on any error (missing file, partial
    JSON, parse error).
    """
    try:
        with open(FPS_STATS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _validate_payload(payload: Optional[dict]) -> bool:
    """
    Freshness + integrity check on a payload dict.
    Rejects:
      - None / empty dict
      - Missing or empty _telemetry.session_id
      - Missing or non-integer _telemetry.sequence
      - Stale _updated_at (older than _STALE_MAX_AGE_S relative to now)

    Only advances _state_last_seq when session matches the committed session.
    Session changes are accepted (pipeline restart) — the first valid payload
    of a new session resets both session_id and last_seq.
    """
    global _state_session_id, _state_last_seq

    if not payload:
        return False

    telemetry = payload.get("_telemetry")
    if not isinstance(telemetry, dict):
        return False

    sess_id = telemetry.get("session_id")
    seq = telemetry.get("sequence")
    if not isinstance(sess_id, str) or not sess_id:
        return False
    if not isinstance(seq, int) or seq < 0:
        return False

    # Staleness: reject if _updated_at is too old
    updated_at = payload.get("_updated_at")
    if not isinstance(updated_at, (int, float)):
        return False
    if time.time() - updated_at > _STALE_MAX_AGE_S:
        logger.debug(
            "[HealthAgent] Stale payload: _updated_at=%.1f age=%.1fs > %.1fs",
            updated_at, time.time() - updated_at, _STALE_MAX_AGE_S,
        )
        return False

    # Sequence advancement tracking
    if sess_id != _state_session_id:
        # New session (pipeline restart) — reset
        _state_session_id = sess_id
        _state_last_seq = seq
        return True

    # Same session: must strictly advance
    if seq <= _state_last_seq:
        logger.debug(
            "[HealthAgent] Non-advancing seq: got %d, last %d (session %s)",
            seq, _state_last_seq, sess_id,
        )
        return False

    _state_last_seq = seq
    return True


def _payload_parts(
    payload: Optional[dict],
) -> tuple:
    """
    Safely extract the three telemetry parts from a single payload dict.
    Returns (fps_stats, feature_stats, offload_crops) with safe defaults.

    Caller must have validated payload freshness via _validate_payload()
    before calling this function.  An invalid payload passed here will
    produce an empty fps_stats dict (score 100 = unavailable).
    """
    if payload is None:
        return {}, {}, {"received_per_s": 0.0}
    fps_stats = {k: v for k, v in payload.items()
                 if not k.startswith("_") and isinstance(v, (int, float))}
    feature_stats = payload.get("_features", {})
    offload_crops = payload.get("_offload_crops", {"received_per_s": 0.0})
    return fps_stats, feature_stats, offload_crops


def _detect_source_starved(
    fps_stats: dict,
    input_fps: dict,
    edge_cfg: dict,
    source_type_map: Optional[dict] = None,
) -> set:
    """
    Detect cameras whose source (upstream feed) is starved.

    A camera is source-starved ONLY when BOTH conditions hold:
      1. Input rate is absent/zero or materially below the expected source rate.
      2. Output rate is also absent/low (not a pure output transient).

    Source-type gate (Phase 1 validity contract):
      ``source_type_map`` maps camera_id → ``"live"`` | ``"file"`` (derived
      from the camera URI in cameras.yml).  File-playback cameras are NEVER
      classified as source-starved: even with PTS-derived input FPS (which
      reflects the native source rate, not decoder throughput), file playback
      is not a live upstream feed — a low PTS-measured rate means the file
      is playing slowly or the muxer is PTS-paced, not that the source is
      starved.  This is a DEVICE GATE — realtime source-starvation enforcement
      for file playback is not implemented (see core_pipeline.streammux
      ``live-source``); do not apply live-feed starvation math to files.
      When source_type_map is None/missing the gate is inert — every camera
      is evaluated exactly as before (backward compatible).

    When _input_fps is unavailable (empty/missing/malformed), returns an empty
    set — preserving current FPS-score behaviour exactly.

    Malformed fps_stats values (None, string, bool, NaN, inf, negative) are
    treated as unavailable (0.0) — the camera is still evaluated with its
    paired input_fps value, so a valid input alone cannot induce starvation.

    Malformed config scalars fall back to defaults (expected_source_rate=25.0,
    starved_threshold_ratio=0.2).  An unusable threshold (≤0, NaN, inf) causes
    an early empty-set return — nothing is starvable.

    Configuration (edge_node.yml, ``source_starved`` section):
      expected_source_rate   float   default 25.0  (fps, matches cameras.yml)
      starved_threshold_ratio float  default 0.2   (below 20 % = starved → 5.0 fps)
    """
    if not isinstance(input_fps, dict) or not input_fps:
        return set()

    # ponytail: guard against non-dict callers before any .get/.items
    if not isinstance(fps_stats, dict):
        fps_stats = {}
    if not isinstance(edge_cfg, dict):
        edge_cfg = {}
    if not isinstance(source_type_map, dict):
        source_type_map = {}

    sc_cfg = edge_cfg.get("source_starved", {})
    if not isinstance(sc_cfg, dict):
        sc_cfg = {}

    # ── Safe config scalars: malformed → default; negative/non-finite → default ──
    def _safe_cfg_float(v, default):
        """Convert *v* to a non-negative finite float; any malformed input → *default*."""
        if v is None:
            return default
        if isinstance(v, bool):
            return default
        if isinstance(v, (int, float)):
            if math.isfinite(v) and v >= 0.0:
                return float(v)
            return default
        if isinstance(v, str):
            try:
                fv = float(v)
                if math.isfinite(fv) and fv >= 0.0:
                    return fv
            except (ValueError, TypeError):
                pass
        return default

    expected = _safe_cfg_float(sc_cfg.get("expected_source_rate"), 25.0)
    ratio    = _safe_cfg_float(sc_cfg.get("starved_threshold_ratio"), 0.2)
    threshold = expected * ratio

    # ponytail: unusable threshold → nothing is starvable (defensible empty)
    if not (math.isfinite(threshold) and threshold > 0.0):
        return set()

    # ── Safe FPS values: malformed → 0.0 (unavailable) ──
    def _safe_fps(v):
        """Convert *v* to a non-negative finite float; any malformed input → 0.0."""
        if v is None:
            return 0.0
        if isinstance(v, bool):
            return 0.0
        if isinstance(v, (int, float)):
            if math.isfinite(v) and v >= 0.0:
                return float(v)
            return 0.0
        if isinstance(v, str):
            try:
                fv = float(v)
                if math.isfinite(fv) and fv >= 0.0:
                    return fv
            except (ValueError, TypeError):
                pass
        return 0.0

    starved: set = set()
    for cam_id in set(fps_stats) | set(input_fps):
        # File-playback cameras are excluded from starvation classification:
        # their input FPS is decoder throughput, not an upstream feed rate.
        # See the source_type_map docstring above (device gate).
        if source_type_map.get(cam_id) == "file":
            continue
        in_fps  = _safe_fps(input_fps.get(cam_id))
        out_fps = _safe_fps(fps_stats.get(cam_id))
        if in_fps < threshold and out_fps < threshold:
            starved.add(cam_id)

    return starved


def _derive_camera_workload(
    feature_stats: dict,
    fps_stats: dict,
    starved_cams: set = None,
) -> dict:
    """
    Derive per-camera workload {camera_id: n_track + n_plate} from the same
    telemetry window's _features snapshot.

    Only active (fps > 0), non-source-starved cameras are included.
    A camera is skipped — never crashes the payload builder — when its
    n_track/n_plate are missing, non-numeric, bool, non-finite, or negative.
    """
    result: Dict[str, float] = {}
    starved = starved_cams or set()

    for cam_id, feats in feature_stats.items():
        if not isinstance(feats, dict) or cam_id in starved:
            continue
        fps = fps_stats.get(cam_id, 0.0)
        if not isinstance(fps, (int, float)) or fps <= 0.0:
            continue  # inactive camera
        n_track = feats.get("n_track")
        n_plate = feats.get("n_plate")
        if not isinstance(n_track, (int, float)) or isinstance(n_track, bool):
            continue
        if not isinstance(n_plate, (int, float)) or isinstance(n_plate, bool):
            continue
        if not math.isfinite(n_track) or n_track < 0:
            continue
        if not math.isfinite(n_plate) or n_plate < 0:
            continue
        result[cam_id] = n_track + n_plate

    return result


def _read_pipeline_snapshot() -> tuple:
    """
    Read the pipeline JSON once, validate freshness/integrity, return all parts.

    Returns (valid: bool, fps_stats, feature_stats, offload_crops,
             input_fps, source_modes).

    ``source_modes`` is ``_telemetry.source_modes`` from the probe payload
    (camera_id → "live" | "file").  Missing or malformed → {} so callers
    that don't yet pass it to _detect_source_starved remain backward
    compatible.

    When valid=False:
      fps_stats is {} and the caller must not use telemetry-derived
      values (fps_stats, features, offload rate) for load scoring or
      payload publishing.  Callers report the pipeline as unavailable.
    """
    payload = _read_payload()
    if not _validate_payload(payload):
        return False, {}, {}, {}, {}, {}
    input_fps = payload.get("_input_fps", {})
    if not isinstance(input_fps, dict):
        input_fps = {}
    source_modes = {}
    telemetry = payload.get("_telemetry")
    if isinstance(telemetry, dict):
        m = telemetry.get("source_modes")
        if isinstance(m, dict):
            source_modes = {str(k): str(v) for k, v in m.items()}
    return True, *_payload_parts(payload), input_fps, source_modes


# ---------------------------------------------------------------------------
# FPS Reader (read JSON file written by SpeedProbe)
# ---------------------------------------------------------------------------

def _read_fps_stats() -> Dict[str, float]:
    """
    Read FPS per camera from the unified payload.
    Return dict {camera_id: fps} or empty dict on error.
    Delegates to _read_payload() for safe single-read.
    """
    fps_stats, _, _ = _payload_parts(_read_payload())
    return fps_stats


def _read_feature_stats() -> Dict[str, Dict[str, float]]:
    """
    Read per-camera proactive features from the unified payload.
    Delegates to _read_payload() for safe single-read.

    Returns {camera_id: {n_track, n_plate, stationary_fraction}} or {} on error.
    """
    _, feature_stats, _ = _payload_parts(_read_payload())
    return feature_stats


def _read_offload_crops() -> dict:
    """
    Read _offload_crops snapshot from the unified payload.
    Delegates to _read_payload() for safe single-read.

    Returns {processed_count, received_per_s, ts} or {"received_per_s": 0.0}.
    """
    _, _, offload_crops = _payload_parts(_read_payload())
    return offload_crops


# ---------------------------------------------------------------------------
# Metric Collector
# ---------------------------------------------------------------------------

def _collect_jetson_metrics() -> Dict:
    """
    Called when jtop is unavailable (e.g. daemon not running or JetPack
    version mismatch).  Returns all-zero metrics so the health loop never
    crashes — the load score will just be driven entirely by the FPS penalty
    until jtop recovers.

    This project targets NVIDIA Jetson devices.  Jetson does not use
    nvidia-smi for the integrated GPU; jtop/tegrastats are the correct metric
    sources.  Therefore no generic nvidia-smi fallback is used here.
    """
    logger.warning(
        "[HealthAgent] jtop unavailable — metrics are zero. "
        "Ensure the jtop daemon is running: sudo systemctl start jtop"
    )
    return {
        "gpu_percent": 0.0,
        "cpu_percent": 0.0,
        "ram_percent": 0.0,
        "gpu_temp_c":  0.0,
        "power_mw":    0.0,
        "source": "jtop_unavailable",
    }


# Path to edge_node.yml and mtime for reload-on-use
_EDGE_NODE_YML = Path(__file__).resolve().parent / "configs" / "edge_node.yml"
_EDGE_CFG: dict = {}
_EDGE_CFG_MTIME: float = 0.0


def _load_edge_node_cfg() -> dict:
    """
    Read edge_node.yml. Returns the full parsed dict, or {} on error.
    Used by _maybe_reload_edge_cfg for mtime-based hot-reload.
    """
    try:
        import yaml
        with open(_EDGE_NODE_YML, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.debug("[HealthAgent] edge_node.yml load error: %s", exc)
        return {}


def _maybe_reload_edge_cfg() -> None:
    """
    Reload _EDGE_CFG if edge_node.yml mtime has changed since last read.
    Idempotent — called before every config-consuming operation.
    """
    global _EDGE_CFG, _EDGE_CFG_MTIME
    try:
        mtime = _EDGE_NODE_YML.stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime != _EDGE_CFG_MTIME:
        _EDGE_CFG = _load_edge_node_cfg()
        _EDGE_CFG_MTIME = mtime
        logger.info("[HealthAgent] edge_node.yml reloaded (mtime changed)")


def get_edge_cfg() -> dict:
    """Return the latest edge_node.yml config, reloading when the file changed."""
    _maybe_reload_edge_cfg()
    return _EDGE_CFG


# Seed at import time once, then mtime-based reload kicks in on each use.
_EDGE_CFG = _load_edge_node_cfg()
try:
    _EDGE_CFG_MTIME = _EDGE_NODE_YML.stat().st_mtime
except OSError:
    _EDGE_CFG_MTIME = 0.0
_FPS_HISTORY: Deque[Tuple[float, float]] = collections.deque(maxlen=20)


def _compute_load_score(
    metrics: dict,
    fps_stats: dict,
    source_starved_cameras: set = None,
    feature_stats: Optional[dict] = None,
) -> tuple:
    """
    Base + Additive Bonus load score with hardware emergency floor.

    Components:
        fps_score:      piecewise linear on 0-100 scale:
                        27->0, 22->57, 19->65, 17->75, 0->100
        workload_bonus: min(max_bonus, max_bonus * (n_track + n_plate) / capacity)
        thermal_bonus:  min(max_bonus, max_bonus * ramp(onset_c, critical_c))
        recv_bonus:     min(max_bonus, max_bonus * offload_crops_received_per_s / capacity)
        trend_bonus:    min(max_bonus, max_bonus * FPS decline slope / max_decline)

    Composite:
        min(100.0, fps_score + workload_bonus + thermal_bonus + recv_bonus + trend_bonus)

    Hardware emergency floor:
        If CPU >= hw_fuse_threshold (90) OR RAM >= hw_fuse_threshold
        AND fps_clamped < TARGET_FPS - 2.0:
            score = max(composite, hw_fuse_score_floor) (default floor 80.0 = L1)
    """
    _starved = source_starved_cameras or set()

    _maybe_reload_edge_cfg()
    ls_cfg = _EDGE_CFG.get("load_score", {})
    if not isinstance(ls_cfg, dict):
        ls_cfg = {}
    hw_fuse_threshold   = float(ls_cfg.get("hw_fuse_threshold",   90.0))
    hw_fuse_score_floor = float(ls_cfg.get("hw_fuse_score_floor", 80.0))

    # ── FPS component (piecewise linear on [0, 100] scale) ──────
    active_fps_vals = [
        v for k, v in fps_stats.items()
        if v > 0.0 and k not in _starved
    ] if isinstance(fps_stats, dict) else []
    if active_fps_vals:
        avg_fps = sum(active_fps_vals) / len(active_fps_vals)
    else:
        return 100.0, "fps_dominant"

    fps_clamped = max(0.0, min(float(TARGET_FPS), avg_fps))

    if fps_clamped >= float(TARGET_FPS):
        fps_score = 0.0
    elif fps_clamped >= 22.0:
        fps_score = 57.0 * (float(TARGET_FPS) - fps_clamped) / (float(TARGET_FPS) - 22.0)
    elif fps_clamped >= 19.0:
        fps_score = 57.0 + (65.0 - 57.0) * (22.0 - fps_clamped) / (22.0 - 19.0)
    elif fps_clamped >= 17.0:
        fps_score = 65.0 + (75.0 - 65.0) * (19.0 - fps_clamped) / (19.0 - 17.0)
    else:
        fps_score = 75.0 + (100.0 - 75.0) * (17.0 - fps_clamped) / (17.0 - 0.0)

    # ── Workload bonus (n_track + n_plate / capacity) ───────────
    workload_bonus = 0.0
    wl_cfg = ls_cfg.get("workload", {})
    if (isinstance(wl_cfg, dict) and wl_cfg.get("enabled") is True
            and isinstance(feature_stats, dict)):
        wl_cap = _finite_positive(wl_cfg.get("capacity"))
        wl_max = _finite_positive(wl_cfg.get("max_bonus")) or 15.0
        if wl_cap is not None:
            wl_total = sum(
                _derive_camera_workload(feature_stats, fps_stats, _starved).values()
            )
            workload_bonus = min(wl_max, max(0.0, wl_max * (wl_total / wl_cap)))

    # ── Thermal bonus (gpu_temp_c ramp [onset, critical]) ───────
    thermal_bonus = 0.0
    th_cfg = ls_cfg.get("thermal", {})
    if isinstance(th_cfg, dict) and th_cfg.get("enabled") is True and isinstance(metrics, dict):
        temp_val = _finite_nonneg(metrics.get("gpu_temp_c"))
        onset    = _finite_nonneg(th_cfg.get("onset_c"))
        critical = _finite_nonneg(th_cfg.get("critical_c"))
        th_max   = _finite_positive(th_cfg.get("max_bonus")) or 5.0
        if temp_val is not None and onset is not None and critical is not None and onset < critical:
            if temp_val >= critical:
                thermal_bonus = th_max
            elif temp_val > onset:
                thermal_bonus = th_max * (temp_val - onset) / (critical - onset)

    # ── Received crops bonus (offload_crops_received_per_s) ─────
    recv_bonus = 0.0
    recv_cfg = ls_cfg.get("recv", {})
    if isinstance(recv_cfg, dict) and recv_cfg.get("enabled") is True and isinstance(metrics, dict):
        recv_val = _finite_nonneg(metrics.get("offload_crops_received_per_s"))
        recv_cap = _finite_positive(recv_cfg.get("capacity")) or 10.0
        recv_max = _finite_positive(recv_cfg.get("max_bonus")) or 5.0
        if recv_val is not None:
            recv_bonus = min(recv_max, max(0.0, recv_max * (recv_val / recv_cap)))

    # ── FPS trend bonus (decline rate) ──────────────────────────
    trend_bonus = 0.0
    trend_cfg = ls_cfg.get("trend", {})
    now = time.monotonic()
    if isinstance(trend_cfg, dict) and trend_cfg.get("enabled") is True:
        max_decline = _finite_positive(trend_cfg.get("max_decline_fps_per_s")) or 2.0
        tr_max      = _finite_positive(trend_cfg.get("max_bonus")) or 5.0
        if _FPS_HISTORY:
            # find oldest sample within window
            t_past, fps_past = _FPS_HISTORY[0]
            for t_h, f_h in _FPS_HISTORY:
                if now - t_h >= 0.5:
                    t_past, fps_past = t_h, f_h
                    break
            dt = now - t_past
            if dt >= 0.5:
                slope = (avg_fps - fps_past) / dt
                decline = max(0.0, -slope)
                trend_bonus = min(tr_max, max(0.0, tr_max * (decline / max_decline)))
    _FPS_HISTORY.append((now, avg_fps))

    raw_composite = fps_score + workload_bonus + thermal_bonus + recv_bonus + trend_bonus
    composite = min(100.0, max(0.0, raw_composite))

    # ── Hardware emergency floor ────────────────────────────────
    hw_saturated = (
        isinstance(metrics, dict) and (
            float(metrics.get("cpu_percent", 0.0)) >= hw_fuse_threshold or
            float(metrics.get("ram_percent", 0.0)) >= hw_fuse_threshold
        )
    )
    fps_emergency = fps_clamped < float(TARGET_FPS) - 2.0

    if hw_saturated and fps_emergency:
        score = max(composite, hw_fuse_score_floor)
    else:
        score = composite

    return round(score, 1), "fps_dominant"


def _compute_load_score_breakdown(
    metrics: dict,
    fps_stats: dict,
    source_starved_cameras: set = None,
    feature_stats: Optional[dict] = None,
) -> dict:
    """
    Pure helper yielding auditable breakdown of the load score computation.
    """
    _starved = source_starved_cameras or set()

    if not isinstance(fps_stats, dict):
        return {
            "fps_score": 100.0,
            "workload_bonus": 0.0,
            "thermal_bonus": 0.0,
            "recv_bonus": 0.0,
            "trend_bonus": 0.0,
            "composite_score": 100.0,
            "load_score": 100.0,
        }

    _maybe_reload_edge_cfg()
    ls_cfg = _EDGE_CFG.get("load_score", {})
    if not isinstance(ls_cfg, dict):
        ls_cfg = {}
    hw_fuse_threshold   = float(ls_cfg.get("hw_fuse_threshold",   90.0))
    hw_fuse_score_floor = float(ls_cfg.get("hw_fuse_score_floor", 80.0))

    active_fps_vals = [
        v for k, v in fps_stats.items()
        if v > 0.0 and k not in _starved
    ]
    if active_fps_vals:
        avg_fps = sum(active_fps_vals) / len(active_fps_vals)
    else:
        return {
            "fps_score": 100.0,
            "workload_bonus": 0.0,
            "thermal_bonus": 0.0,
            "recv_bonus": 0.0,
            "trend_bonus": 0.0,
            "composite_score": 100.0,
            "load_score": 100.0,
        }

    fps_clamped = max(0.0, min(float(TARGET_FPS), avg_fps))

    if fps_clamped >= float(TARGET_FPS):
        fps_score = 0.0
    elif fps_clamped >= 22.0:
        fps_score = 57.0 * (float(TARGET_FPS) - fps_clamped) / (float(TARGET_FPS) - 22.0)
    elif fps_clamped >= 19.0:
        fps_score = 57.0 + (65.0 - 57.0) * (22.0 - fps_clamped) / (22.0 - 19.0)
    elif fps_clamped >= 17.0:
        fps_score = 65.0 + (75.0 - 65.0) * (19.0 - fps_clamped) / (19.0 - 17.0)
    else:
        fps_score = 75.0 + (100.0 - 75.0) * (17.0 - fps_clamped) / (17.0 - 0.0)

    workload_bonus = 0.0
    wl_cfg = ls_cfg.get("workload", {})
    if (isinstance(wl_cfg, dict) and wl_cfg.get("enabled") is True
            and isinstance(feature_stats, dict)):
        wl_cap = _finite_positive(wl_cfg.get("capacity"))
        wl_max = _finite_positive(wl_cfg.get("max_bonus")) or 15.0
        if wl_cap is not None:
            wl_total = sum(
                _derive_camera_workload(feature_stats, fps_stats, _starved).values()
            )
            workload_bonus = min(wl_max, max(0.0, wl_max * (wl_total / wl_cap)))

    thermal_bonus = 0.0
    th_cfg = ls_cfg.get("thermal", {})
    if isinstance(th_cfg, dict) and th_cfg.get("enabled") is True and isinstance(metrics, dict):
        temp_val = _finite_nonneg(metrics.get("gpu_temp_c"))
        onset    = _finite_nonneg(th_cfg.get("onset_c"))
        critical = _finite_nonneg(th_cfg.get("critical_c"))
        th_max   = _finite_positive(th_cfg.get("max_bonus")) or 5.0
        if temp_val is not None and onset is not None and critical is not None and onset < critical:
            if temp_val >= critical:
                thermal_bonus = th_max
            elif temp_val > onset:
                thermal_bonus = th_max * (temp_val - onset) / (critical - onset)

    recv_bonus = 0.0
    recv_cfg = ls_cfg.get("recv", {})
    if isinstance(recv_cfg, dict) and recv_cfg.get("enabled") is True and isinstance(metrics, dict):
        recv_val = _finite_nonneg(metrics.get("offload_crops_received_per_s"))
        recv_cap = _finite_positive(recv_cfg.get("capacity"))
        recv_max = _finite_positive(recv_cfg.get("max_bonus")) or 5.0
        if recv_val is not None and recv_cap is not None:
            recv_bonus = min(recv_max, max(0.0, recv_max * (recv_val / recv_cap)))

    trend_bonus = 0.0
    trend_cfg = ls_cfg.get("trend", {})
    now = time.monotonic()
    if isinstance(trend_cfg, dict) and trend_cfg.get("enabled") is True:
        max_decline = _finite_positive(trend_cfg.get("max_decline_fps_per_s"))
        tr_max      = _finite_positive(trend_cfg.get("max_bonus")) or 5.0
        if max_decline is not None and _FPS_HISTORY:
            t_past, fps_past = _FPS_HISTORY[0]
            for t_h, f_h in _FPS_HISTORY:
                if now - t_h >= 0.5:
                    t_past, fps_past = t_h, f_h
                    break
            dt = now - t_past
            if dt >= 0.5:
                slope = (avg_fps - fps_past) / dt
                decline = max(0.0, -slope)
                trend_bonus = min(tr_max, max(0.0, tr_max * (decline / max_decline)))

    raw_composite = fps_score + workload_bonus + thermal_bonus + recv_bonus + trend_bonus
    composite = min(100.0, max(0.0, raw_composite))

    hw_saturated = (
        isinstance(metrics, dict) and (
            float(metrics.get("cpu_percent", 0.0)) >= hw_fuse_threshold or
            float(metrics.get("ram_percent", 0.0)) >= hw_fuse_threshold
        )
    )
    fps_emergency = fps_clamped < float(TARGET_FPS) - 2.0

    if hw_saturated and fps_emergency:
        load_score = max(composite, hw_fuse_score_floor)
    else:
        load_score = composite

    return {
        "fps_score": round(fps_score, 1),
        "workload_bonus": round(workload_bonus, 1),
        "thermal_bonus": round(thermal_bonus, 1),
        "recv_bonus": round(recv_bonus, 1),
        "trend_bonus": round(trend_bonus, 1),
        "composite_score": round(composite, 1),
        "load_score": round(load_score, 1),
    }


# ── Config-safe float helpers used by _compute_load_score ──────
def _finite_positive(v):
    """Return float(v) for finite v > 0.0 (not bool); None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0.0:
        return float(v)
    return None


def _finite_nonneg(v):
    """Return float(v) for finite numeric v (not bool); None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


# ---------------------------------------------------------------------------
# Health Agent Main Loop
# ---------------------------------------------------------------------------

class HealthAgent:
    """
    Collect metrics and publish periodically via Zenoh (peer mode).
    Runs a daemon thread with a persistent jtop session to avoid
    socket/fd exhaustion from opening a new jtop() context each cycle.

    When run as a standalone process, opens its own MonitorClient
    WebSocket to push health payloads to the Central Monitor Server.
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._pub = None
        self._jtop = None          # persistent jtop session
        self._monitor_client = None  # own WS client when run standalone
        # One-shot warmup_ms set by run_python.py after pipeline PLAYING
        self._warmup_ms: Optional[float] = None
        # Proactive load model — instantiated lazily in _run so that
        # edge_node.yml is read after the process fully starts.
        self._proactive_model = None
        self._cam_configs_cache: Dict[str, dict] = {}
        self._max_streams = 8   # from cameras.yml; fallback count of concurrent streams
        self._last_cfg_reload = 0.0

    def _reload_cam_configs(self) -> Dict[str, dict]:
        """Read cameras.yml for peer failover metadata in health payloads."""
        try:
            import yaml

            cam_yml = Path(__file__).resolve().parent / "configs" / "cameras.yml"
            with open(cam_yml, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

            # max_streams sourced from cameras.yml; malformed values fall back to 8
            try:
                self._max_streams = int(raw.get("max_streams", 8) or 8)
            except (TypeError, ValueError):
                self._max_streams = 8

            result: Dict[str, dict] = {}
            for cam_id, cfg in raw.get("cameras", {}).items():
                if cfg and cfg.get("enabled", True):
                    result[cam_id] = {
                        "camera_id":       cam_id,
                        "source_id":       int(cfg.get("source_id", 0)),
                        "uri":             cfg.get("uri", ""),
                        "name":            cfg.get("name", cam_id),
                        "fps":             float(cfg.get("fps", 25.0)),
                        "speed_limit_kmh": float(cfg.get("speed_limit_kmh", 80.0)),
                        "homography":      cfg.get("homography", {}),
                        "roi_polygon":     cfg.get("roi_polygon", []),
                        "output":          cfg.get("output", {}),
                    }
            self._cam_configs_cache = result
            return result
        except Exception as exc:
            logger.warning("[HealthAgent] Failed to reload camera configs: %s", exc)
            return self._cam_configs_cache

    def start(self) -> None:
        """Start agent in daemon thread."""
        # Open own MonitorClient if running standalone
        if MONITOR_URL:
            try:
                from speedflow_python.monitor_client import MonitorClient, set_default_client
                self._monitor_client = MonitorClient(MONITOR_URL, NODE_ID, ADVERTISE_IP)
                self._monitor_client.start()
                set_default_client(self._monitor_client)
                logger.info("[HealthAgent] MonitorClient started → %s", MONITOR_URL)
            except Exception as exc:
                logger.warning("[HealthAgent] MonitorClient failed to start: %s", exc)

        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="HealthAgent",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[HealthAgent] Started. Node=%s, Interval=%.1fs",
            NODE_ID, HEALTH_INTERVAL,
        )

    def stop(self) -> None:
        self._running = False
        if self._jtop is not None:
            try:
                self._jtop.close()
            except Exception:
                pass
            self._jtop = None
        if self._monitor_client is not None:
            try:
                self._monitor_client.stop()
            except Exception:
                pass
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass

    def _connect_zenoh(self):
        """Open Zenoh session, declare publisher, subscribe to traffic events."""
        import zenoh
        try:
            session = make_session()
            pub = session.declare_publisher(f"peers/status/{NODE_ID}")

            # Subscribe to overspeed events from the pipeline process and
            # forward them to the Central Monitor Server via MonitorClient.
            # This avoids having two MonitorClient WS connections for the
            # same node_id (which causes a reconnect loop on the Server).
            session.declare_subscriber(
                f"traffic/events/{NODE_ID}/**",
                self._on_traffic_event,
            )

            logger.info("[HealthAgent] Zenoh session opened (peer mode).")
            return session, pub
        except Exception as exc:
            logger.error("[HealthAgent] Cannot open Zenoh session: %s", exc)
            return None, None

    def _on_traffic_event(self, sample) -> None:
        """Forward overspeed events from pipeline → Central Monitor."""
        try:
            payload = msgpack.unpackb(sample.payload.to_bytes(), raw=False)
            if payload.get("type") == "overspeed":
                from speedflow_python.monitor_client import send_to_monitor
                send_to_monitor(payload)
        except Exception as exc:
            logger.debug("[HealthAgent] Traffic event forward error: %s", exc)

    def _open_jtop(self):
        """Try to open a persistent jtop session; return it or None.

        jtop is a Thread subclass.  The correct persistent usage (without
        the 'with' context manager) is:
            j = jtop()
            j.start()          # starts the background thread
            j.ok()             # BLOCKS until the first data packet arrives

        Calling any property (gpu, cpu, temperature, …) before ok() returns
        True raises KeyError because self._stats is still {}.

        Fix #15: run j.ok() in a daemon thread with a hard timeout so a hung
        jtop daemon (unresponsive hardware / JetPack mismatch) never deadlocks
        the HealthAgent startup.
        """
        try:
            from jtop import jtop as JTop
            j = JTop()
            j.start()
            # Block until the first data collection completes so _stats is
            # populated before _collect_metrics reads from it — but give up
            # after 10 seconds so a frozen jtop daemon never deadlocks startup.
            _ok_event = threading.Event()

            def _wait_ok():
                try:
                    if j.ok():
                        _ok_event.set()
                except Exception:
                    pass

            _t = threading.Thread(target=_wait_ok, daemon=True)
            _t.start()
            _t.join(timeout=10.0)

            if not _ok_event.is_set():
                logger.warning(
                    "[HealthAgent] jtop ok() did not return within 10 s "
                    "(hardware unresponsive?) — using zero metrics."
                )
                try:
                    j.close()
                except Exception:
                    pass
                return None

            logger.info("[HealthAgent] jtop session opened and ready (persistent).")
            return j
        except Exception as exc:
            logger.debug("[HealthAgent] jtop unavailable: %s — using zero fallback.", exc)
            return None

    def _collect_metrics(self) -> Dict:
        """Read metrics from persistent jtop session.

        Reads from jtop's dedicated properties (gpu, cpu, memory, temperature,
        power) rather than from jtop.stats.  jtop.stats is a computed property
        that calls all sub-properties internally — if any one of them raises a
        KeyError (e.g. 'power' not in _stats) the entire stats call fails.
        Reading each property individually lets us handle missing sensors
        gracefully without losing GPU% and Temp.

        If jtop is unavailable, returns all-zero metrics with source='jtop_unavailable'.
        """
        if self._jtop is not None:
            try:
                # --- GPU % ---
                # jtop.gpu is a GPU object (dict-like): {name: {status: {load:…}}}
                # The first GPU entry's status.load is the utilisation percentage.
                gpu_pct = 0.0
                try:
                    for gpu_info in self._jtop.gpu.values():
                        load = gpu_info.get("status", {}).get("load", 0.0)
                        gpu_pct = float(load)
                        break  # only first GPU
                except Exception:
                    pass

                # --- CPU % ---
                # jtop.cpu = {"total": {"idle": float, …}, "cpu": […]}
                # total.idle is the aggregate idle percentage across all cores.
                cpu_pct = 0.0
                try:
                    cpu_total = self._jtop.cpu.get("total", {})
                    cpu_idle  = cpu_total.get("idle", 100.0)
                    cpu_pct   = 100.0 - float(cpu_idle)
                except Exception:
                    pass

                # --- RAM % ---
                # jtop.memory["RAM"] = {"used": int KB, "tot": int KB, …}
                ram_pct = 0.0
                try:
                    mem = self._jtop.memory
                    ram_tot = mem["RAM"]["tot"]
                    if ram_tot > 0:
                        ram_pct = float(mem["RAM"]["used"]) / ram_tot * 100.0
                except Exception:
                    pass

                # --- Temperature ---
                # jtop.temperature = {sensor_name: {"temp": float, "online": bool, …}}
                # Sensor names on Orin/JetPack 6: "cpu", "gpu", "soc0", "soc1",
                # "soc2", "tj" etc.  Pick "gpu" first, then "tj" (junction),
                # then the first online sensor with a plausible value.
                temp_c = 0.0
                try:
                    temp_dict = self._jtop.temperature
                    for key in ("gpu", "tj", "cpu"):
                        info = temp_dict.get(key)
                        if not isinstance(info, dict):
                            continue
                        t = info.get("temp", -256)
                        if isinstance(t, (int, float)) and 0 < t < 120:
                            temp_c = float(t)
                            break
                    else:
                        # fallback: first sensor with plausible value
                        for info in temp_dict.values():
                            if not isinstance(info, dict):
                                continue
                            t = info.get("temp", -256)
                            if isinstance(t, (int, float)) and 0 < t < 120:
                                temp_c = float(t)
                                break
                except Exception:
                    pass

                # --- Power ---
                # jtop.power = {"rail": {…}, "tot": {"power": int mW, …}}
                # May not exist on all boards; guard carefully.
                power_mw = 0.0
                try:
                    pwr = self._jtop.power
                    if isinstance(pwr, dict):
                        power_mw = float(pwr.get("tot", {}).get("power", 0))
                except Exception:
                    pass

                return {
                    "gpu_percent": round(gpu_pct, 1),
                    "cpu_percent": round(cpu_pct, 1),
                    "ram_percent": round(ram_pct, 1),
                    "gpu_temp_c":  round(temp_c, 1),
                    "power_mw":    round(power_mw, 0),
                    "source":      "jtop",
                }
            except Exception as exc:
                logger.debug("[HealthAgent] jtop read error: %s — falling back.", exc)

        return _collect_jetson_metrics()

    def _run(self) -> None:
        """Main loop — collect and publish periodically."""
        self._session, self._pub = self._connect_zenoh()
        if not self._session:
            logger.error("[HealthAgent] Zenoh unavailable. Running in log-only mode.")

        self._jtop = self._open_jtop()

        # Instantiate proactive model using the proactive: section of edge_node.yml.
        get_edge_cfg()
        from speedflow_python.load_model import ProactiveModel
        self._proactive_model = ProactiveModel(
            _EDGE_CFG.get("proactive", {}),
            policy=LOAD_POLICY,
            model_type=LOAD_MODEL,
        )
        logger.info("[HealthAgent] LOAD_POLICY=%s LOAD_MODEL=%s", LOAD_POLICY, LOAD_MODEL)
        if self._proactive_model.enabled:
            logger.info("[HealthAgent] Proactive load model ENABLED "
                        "(risk_threshold=%.2f)", self._proactive_model.risk_threshold)
        else:
            logger.info("[HealthAgent] Proactive load model disabled "
                        "(set proactive.enabled: true in edge_node.yml to activate)")

        _zenoh_retry_interval = 30.0  # seconds between Zenoh reconnect attempts
        _last_zenoh_attempt = time.time()
        _log_cycle = 0  # counts health cycles; log LoadScore every HEALTH_LOG_EVERY
        _cfg_reload_interval = 30.0
        self._last_cfg_reload = 0.0

        # ponytail: monotonic deadline sleep so work duration doesn't extend the period.
        _next_deadline = time.monotonic()

        while self._running:
            try:
                if time.monotonic() - self._last_cfg_reload >= _cfg_reload_interval:
                    _maybe_reload_edge_cfg()
                    if self._proactive_model is not None:
                        self._proactive_model.reload_cfg(
                            get_edge_cfg().get("proactive", {})
                        )
                    self._reload_cam_configs()
                    self._last_cfg_reload = time.monotonic()

                # Periodically retry Zenoh if the session is not established
                if self._session is None:
                    if time.time() - _last_zenoh_attempt >= _zenoh_retry_interval:
                        logger.info("[HealthAgent] Retrying Zenoh connection...")
                        self._session, self._pub = self._connect_zenoh()
                        _last_zenoh_attempt = time.time()
                        if self._session:
                            logger.info("[HealthAgent] Zenoh reconnected successfully.")

                metrics = self._collect_metrics()

                snapshot_valid, fps_stats, feature_stats, offload_crops, input_fps, source_modes = \
                    _read_pipeline_snapshot()

                # ── Pipeline unavailable guard ─────────────────────────
                # When snapshot is invalid (stale, missing telemetry,
                # non-advancing seq), do NOT convert garbage into a
                # healthy TARGET_FPS score.  Report unavailable
                # (load_score 100 = worst) + empty pipeline section.
                if snapshot_valid:
                    starved_cams = _detect_source_starved(
                        fps_stats, input_fps, get_edge_cfg(),
                        source_type_map=source_modes,
                    )
                    load_score, omega_preset = _compute_load_score(
                        metrics, fps_stats, source_starved_cameras=starved_cams,
                        feature_stats=feature_stats,
                    )
                    # Compute breakdown for auditable payload
                    load_score_breakdown = _compute_load_score_breakdown(
                        metrics, fps_stats, source_starved_cameras=starved_cams,
                        feature_stats=feature_stats,
                    )
                    offload_crops_received_per_s = float(offload_crops.get("received_per_s", 0.0))
                    # BUG-I fix: exclude 0-fps cameras from avg_fps,
                    # matching the exclusion applied in _compute_load_score()
                    # so the reported avg_fps is consistent with the load_score value.
                    active_fps_vals = [v for v in fps_stats.values() if v > 0.0]
                    avg_fps = round(sum(active_fps_vals) / len(active_fps_vals), 1) if active_fps_vals else None
                    active_cameras = [k for k, v in fps_stats.items() if v > 0.0]
                else:
                    starved_cams = set()
                    load_score, omega_preset = 100.0, "fps_dominant"
                    load_score_breakdown = {
                        "fps_score": 100.0,
                        "workload_bonus": 0.0,
                        "thermal_bonus": 0.0,
                        "recv_bonus": 0.0,
                        "trend_bonus": 0.0,
                        "composite_score": 100.0,
                        "load_score": 100.0,
                    }
                    offload_crops_received_per_s = 0.0
                    active_fps_vals = []
                    avg_fps = None
                    active_cameras = []
                    # Zero out telemetry so downstream code (proactive model,
                    # logging) sees empty inputs, not stale data.
                    fps_stats = {}
                    feature_stats = {}
                    offload_crops = {"received_per_s": 0.0}

                # Consume one-shot warmup_ms written by run_python.py after
                # pipeline.set_state(PLAYING).  After the first heartbeat
                # it appears in, reset so it only fires once per cold-start.
                warmup_ms = self._warmup_ms
                self._warmup_ms = None

                # Active, non-source-starved cameras only.  Empty in the
                # invalid-snapshot branch (feature_stats/fps_stats are {}) —
                # nothing to derive from, matching the unavailable report.
                camera_workload = _derive_camera_workload(
                    feature_stats, fps_stats, starved_cams
                )

                payload = {
                    "type":          "health",
                    "node_id":       NODE_ID,
                    "timestamp":     time.time(),
                    "load_score":    load_score,
                    "omega_preset":  omega_preset,
                    "load_score_breakdown": load_score_breakdown,
                    "gpu_percent":   metrics["gpu_percent"],
                    "cpu_percent":   metrics["cpu_percent"],
                    "ram_percent":   metrics["ram_percent"],
                    "gpu_temp_c":    metrics["gpu_temp_c"],
                    "power_mw":      metrics["power_mw"],
                    "source":        metrics.get("source", "jtop"),
                    "pipeline": {
                        # pipeline_available distinguishes "pipeline not yet
                        # started / stale snapshot" (False, load_score=100)
                        # from real overload (True, load_score=100).
                        "pipeline_available": snapshot_valid,
                        # output_fps_per_camera = frames the probe actually
                        # processed this window (pipeline throughput).
                        # fps_per_camera is kept for backward compatibility.
                        "fps_per_camera":        fps_stats,
                        "output_fps_per_camera": fps_stats,
                        # input_fps_per_camera = PTS-derived native source
                        # frame rate (SpeedProbe measures buf_pts deltas),
                        # falling back to bounded OSD output rate when PTS
                        # is unavailable.  Used for source-starved detection.
                        "input_fps_per_camera":  input_fps if snapshot_valid else {},
                        "avg_fps":        avg_fps,
                        "active_cameras": active_cameras,
                        "source_starved_cameras": sorted(starved_cams),
                        "camera_workload": camera_workload,
                        "camera_features": feature_stats if snapshot_valid else {},
                        "camera_configs": self._cam_configs_cache,
                        "max_streams":    int(self._max_streams or 8),
                    },
                }

                if warmup_ms is not None:
                    payload["warmup_ms"] = warmup_ms

                # ── Proactive model ────────────────────────────────────────
                # Skip when snapshot is invalid — no features to compute on.
                if snapshot_valid and self._proactive_model is not None:
                    _active_ids = {k for k, v in fps_stats.items() if v > 0.0}
                    proactive_result = self._proactive_model.compute(
                        metrics,
                        {k: v for k, v in feature_stats.items() if k in _active_ids},
                        offload_crops_received_per_s=offload_crops_received_per_s,
                        fps_stats={k: v for k, v in fps_stats.items() if k in _active_ids},
                    )
                    payload.update(proactive_result)

                if self._pub:
                    self._pub.put(msgpack.packb(payload, use_bin_type=True))

                _log_cycle += 1
                if _log_cycle % HEALTH_LOG_EVERY == 1:
                    _risk_str = (
                        f" | U={payload.get('risk_index', 0.0):.3f}"
                        f" L={payload.get('l_proactive', 0.0):.3f}"
                        f" H={payload.get('h_reactive', 0.0):.3f}"
                        if payload.get("proactive_enabled") else ""
                    )
                    logger.info(
                        "LoadScore=%.1f [%s] | GPU=%.1f%% CPU=%.1f%% RAM=%.1f%% "
                        "Temp=%.1f°C Power=%.0fmW | FPS=%s%s",
                        load_score, omega_preset,
                        metrics["gpu_percent"],
                        metrics["cpu_percent"],
                        metrics["ram_percent"],
                        metrics["gpu_temp_c"],
                        metrics["power_mw"],
                        fps_stats,
                        _risk_str,
                    )

                # Push to Central Monitor Server (qua MonitorClient)
                try:
                    from speedflow_python.monitor_client import send_to_monitor
                    send_to_monitor(payload)
                except ImportError:
                    pass

            except Exception as exc:
                logger.error("[HealthAgent] Error in collect loop: %s", exc)

            # ponytail: deadline sleep — work duration does not extend the period.
            _next_deadline += HEALTH_INTERVAL
            _remaining = _next_deadline - time.monotonic()
            if _remaining > 0:
                time.sleep(_remaining)
            else:
                # Overran the interval — reset to next cycle to avoid burst.
                _next_deadline = time.monotonic()


# ---------------------------------------------------------------------------
# Standalone functions for use by run_python.py's health_push_loop.
# These expose a persistent jtop open/collect pattern without requiring
# a HealthAgent instance (avoids the __new__ stub hack).
# ---------------------------------------------------------------------------

def open_jtop_session():
    """
    Open and return a persistent jtop session.
    Identical to HealthAgent._open_jtop() — run_python.py can call this
    directly without instantiating a full HealthAgent.
    Returns the jtop object or None.
    """
    # ponytail: use a bare object with _open_jtop as an unbound method
    # instead of creating a throwaway HealthAgent() that opens MonitorClient,
    # builds ProactiveModel, etc.
    class _Stub:
        _open_jtop = HealthAgent._open_jtop
    return _Stub()._open_jtop()


def collect_metrics(jtop_session):
    """
    Read metrics from a persistent jtop session.
    Identical to HealthAgent._collect_metrics() — takes the jtop object
    returned by open_jtop_session().
    If jtop_session is None, returns all-zero fallback metrics.
    """
    # ponytail: stub so _collect_metrics uses its jtop without a full HealthAgent.
    class _Stub:
        _jtop = jtop_session
        _collect_metrics = HealthAgent._collect_metrics
    return _Stub()._collect_metrics()


# ---------------------------------------------------------------------------
# Entry point (run standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        import zenoh
    except ImportError:
        logger.error("zenoh not installed. Run: pip install zenoh")
        sys.exit(1)

    # PID file lock — prevent two health_agent instances for the same node.
    # Two instances connecting with the same node_id cause the server to close
    # the older connection (code 1000) every time the newer one sends a message,
    # creating an endless close/reconnect loop.
    import os as _os
    _PID_FILE = Path(__file__).resolve().parent / "health_agent.pid"
    _my_pid   = _os.getpid()
    if _PID_FILE.exists():
        try:
            _old_pid = int(_PID_FILE.read_text().strip())
            if _old_pid != _my_pid:
                try:
                    _os.kill(_old_pid, 0)   # signal 0 = existence check only
                    logger.error(
                        "health_agent.py is already running (PID %d). "
                        "Kill it first: kill %d", _old_pid, _old_pid
                    )
                    sys.exit(1)
                except OSError:
                    pass   # old process is dead — stale PID file, safe to continue
        except ValueError:
            pass
    _PID_FILE.write_text(str(_my_pid))

    import atexit as _atexit
    _atexit.register(lambda: _PID_FILE.unlink(missing_ok=True))

    agent = HealthAgent()
    agent.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping HealthAgent...")
        agent.stop()
