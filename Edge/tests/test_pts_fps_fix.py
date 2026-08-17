"""
tests/test_pts_fps_fix.py
=======================
Focused tests for Option A FPS bounding (configured camera FPS bound):

  1. Raw callback FPS is bounded by configured FPS: published = min(raw, configured).
  2. Fallback when configured FPS is unavailable/missing/zero: published = raw callback FPS.
  3. Raw callback FPS of 30 with configured 30 -> published 30 (not halved).
  4. Raw callback FPS of 15 with configured 30 -> published 15.
  5. Multi-camera independent bounding.
  6. Telemetry diagnostics: raw_callback_fps_per_camera, fps_burst_per_camera, fps_bound_by_per_camera.
"""

from __future__ import annotations

from typing import Dict, Optional
import pytest


def _compute_configured_fps_bound(
    frame_counts: Dict[str, int],
    window_dur: float,
    configured_fps: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict]:
    """
    Replicates the Option A FPS bounding logic in SpeedProbe._fps_writer_loop:
      published FPS = min(raw callback FPS, configured camera FPS).
    """
    configured_fps = configured_fps or {}

    fps: Dict[str, float] = {}
    for cam_id, n_frames in frame_counts.items():
        fps[cam_id] = round(n_frames / max(window_dur, 0.001), 1)

    raw_cb_fps: Dict[str, float] = dict(fps)
    burst_cams: Dict[str, float] = {}
    fps_bound_by: Dict[str, str] = {}

    for cam_id, cfg_fps in configured_fps.items():
        if cfg_fps <= 0:
            continue
        cb_fps = raw_cb_fps.get(cam_id, 0.0)
        if cb_fps > cfg_fps:
            fps[cam_id] = cfg_fps
            burst_cams[cam_id] = round(cb_fps - cfg_fps, 1)
            fps_bound_by[cam_id] = "configured"

    input_fps: Dict[str, float] = {
        cam_id: fps[cam_id]
        for cam_id in fps
    }

    return {
        "fps": fps,
        "raw_cb_fps": raw_cb_fps,
        "input_fps": input_fps,
        "fps_burst": burst_cams,
        "fps_bound_by": fps_bound_by,
    }


WINDOW_DUR = 1.0


# ── Option A Key Requirements: 30->30 and 15->15 ──────────────────────────

def test_raw_30_configured_30_publishes_30_not_15():
    """Raw callback 30 fps with configured 30 fps -> published 30.0 (not halved to 15)."""
    cam = "cam_01"
    frame_counts = {cam: 30}
    result = _compute_configured_fps_bound(
        frame_counts, WINDOW_DUR, configured_fps={cam: 30.0}
    )
    assert result["raw_cb_fps"][cam] == 30.0
    assert result["fps"][cam] == 30.0
    assert result["input_fps"][cam] == 30.0
    assert cam not in result["fps_bound_by"]
    assert result["fps_burst"] == {}


def test_raw_15_configured_30_publishes_15():
    """Raw callback 15 fps with configured 30 fps -> published 15.0."""
    cam = "cam_01"
    frame_counts = {cam: 15}
    result = _compute_configured_fps_bound(
        frame_counts, WINDOW_DUR, configured_fps={cam: 30.0}
    )
    assert result["raw_cb_fps"][cam] == 15.0
    assert result["fps"][cam] == 15.0
    assert result["input_fps"][cam] == 15.0
    assert cam not in result["fps_bound_by"]
    assert result["fps_burst"] == {}


def test_burst_above_configured_fps_is_clamped():
    """Raw callback 45 fps with configured 30 fps -> published 30.0, burst delta 15.0."""
    cam = "cam_01"
    frame_counts = {cam: 45}
    result = _compute_configured_fps_bound(
        frame_counts, WINDOW_DUR, configured_fps={cam: 30.0}
    )
    assert result["raw_cb_fps"][cam] == 45.0
    assert result["fps"][cam] == 30.0
    assert result["input_fps"][cam] == 30.0
    assert result["fps_bound_by"][cam] == "configured"
    assert result["fps_burst"][cam] == pytest.approx(15.0, abs=0.1)


# ── Fallback tests when configured FPS is missing or non-positive ─────────

def test_configured_fps_missing_falls_back_to_raw_callback():
    """Missing configured FPS -> published FPS = raw callback FPS."""
    cam = "cam_no_cfg"
    frame_counts = {cam: 45}
    result = _compute_configured_fps_bound(frame_counts, WINDOW_DUR, configured_fps=None)
    assert result["fps"][cam] == 45.0
    assert result["raw_cb_fps"][cam] == 45.0
    assert result["input_fps"][cam] == 45.0
    assert result["fps_bound_by"] == {}
    assert result["fps_burst"] == {}


def test_configured_fps_zero_or_negative_falls_back_to_raw_callback():
    """Zero or negative configured FPS is ignored -> fallback to raw callback FPS."""
    cam = "cam_bad_cfg"
    frame_counts = {cam: 45}
    result = _compute_configured_fps_bound(
        frame_counts, WINDOW_DUR, configured_fps={cam: 0.0}
    )
    assert result["fps"][cam] == 45.0
    assert result["fps_bound_by"] == {}


# ── Non-standard FPS & Multi-camera isolation ────────────────────────────

def test_configured_fps_respects_custom_fps_limit():
    """configured_fps=15 -> clamps burst to 15."""
    cam = "cam_15fps"
    frame_counts = {cam: 40}
    result = _compute_configured_fps_bound(
        frame_counts, WINDOW_DUR, configured_fps={cam: 15.0}
    )
    assert result["fps"][cam] == 15.0
    assert result["raw_cb_fps"][cam] == 40.0
    assert result["fps_bound_by"][cam] == "configured"
    assert result["fps_burst"][cam] == pytest.approx(25.0, abs=0.1)


def test_multi_camera_independent_bounds():
    """Each camera is evaluated independently with its own configured rate."""
    cam_a = "cam_A"
    cam_b = "cam_B"
    cam_c = "cam_C"
    frame_counts = {cam_a: 60, cam_b: 20, cam_c: 25}
    cfg = {cam_a: 30.0, cam_b: 30.0, cam_c: 15.0}

    result = _compute_configured_fps_bound(
        frame_counts, WINDOW_DUR, configured_fps=cfg
    )

    # cam_a: 60 raw > 30 configured -> 30
    assert result["fps"][cam_a] == 30.0
    assert result["fps_bound_by"][cam_a] == "configured"
    assert result["fps_burst"][cam_a] == 30.0

    # cam_b: 20 raw <= 30 configured -> 20
    assert result["fps"][cam_b] == 20.0
    assert cam_b not in result["fps_bound_by"]

    # cam_c: 25 raw > 15 configured -> 15
    assert result["fps"][cam_c] == 15.0
    assert result["fps_bound_by"][cam_c] == "configured"
    assert result["fps_burst"][cam_c] == 10.0


# ── JSON Serialization ────────────────────────────────────────────────────

def test_telemetry_fields_serializable():
    """Telemetry sub-dicts must be JSON-safe."""
    cam = "cam_json"
    frame_counts = {cam: 45}
    result = _compute_configured_fps_bound(
        frame_counts, WINDOW_DUR, configured_fps={cam: 30.0}
    )

    import json
    payload = json.dumps(result)
    parsed = json.loads(payload)
    assert isinstance(parsed["raw_cb_fps"], dict)
    assert isinstance(parsed["fps_burst"], dict)
    assert isinstance(parsed["fps_bound_by"], dict)
    assert parsed["fps"]["cam_json"] == 30.0
