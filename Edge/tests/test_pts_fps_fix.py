"""
tests/test_pts_fps_fix.py
=======================
Focused tests for the PTS-based FPS root-cause fix (Oracle remediation):

  1. Burst callbacks with 30fps PTS produce reported_fps ≤~ 30
     and raw_callback_fps > 30.
  2. PTS-unavailable fallback preserves current callback-only behaviour.
  3. Out-of-order PTS (encoder reset) is counted in pts_dropped but does
     not crash the writer.
  4. Single-PTS camera (only 1 frame in window) keeps callback rate.
  5. _input_fps is PTS-derived source rate when PTS is available,
     and falls back to bounded output fps when PTS is unavailable.
  6. PTS ring is drained each window — stale removed-camera state is
     gone after the next writer flush.
"""

from __future__ import annotations

import time
from collections import deque, defaultdict
from typing import Dict, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Minimal stand-in for the SpeedProbe writer PTS-merge logic extracted from
# probes.py._fps_writer_loop (lines ~738-794).  We replicate only the PTS
# part here so tests exercise the exact algorithm without a GStreamer
# runtime.
# ---------------------------------------------------------------------------

# Mirror of the constants in probes.py
_PTS_RING_MAXLEN = 256  # deque(maxlen=256)


def _compute_pts_fps_bound(
    frame_counts: Dict[str, int],
    pts_ring: Dict[str, deque],
    window_dur: float,
    configured_fps: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict]:
    """
    Replicates the PTS-bounding logic in SpeedProbe._fps_writer_loop,
    including the configured-fps fallback bound.

    ``configured_fps`` maps camera_id → CameraConfig.fps.  When provided
    and the PTS ring stays empty for a camera, the published FPS is
    bounded by that value (the authoritative authored native rate).

    Returns a dict with:
      fps              – bounded FPS per camera (output)
      raw_cb_fps       – raw OSD callback rate
      src_pts_fps      – PTS-measured source rate (only cameras with ≥2 valid)
      input_fps        – PTS-derived source rate; fallback = bounded fps
      fps_burst        – cameras where callback > bound source rate
      pts_dropped      – out-of-order PTS count per camera
      fps_bound_by     – camera → "pts" | "configured" when clamped
    """
    configured_fps = configured_fps or {}

    fps: Dict[str, float] = {}
    for cam_id, n_frames in frame_counts.items():
        fps[cam_id] = round(n_frames / max(window_dur, 0.001), 1)

    raw_cb_fps: Dict[str, float] = dict(fps)
    src_pts_fps: Dict[str, float] = {}
    dropped_pts: Dict[str, int] = {}
    burst_cams: Dict[str, float] = {}
    fps_bound_by: Dict[str, str] = {}

    for cam_id, ring in list(pts_ring.items()):
        # Snapshot and drain — per-window; no stale state carries forward.
        pts_snapshot = list(ring)
        ring.clear()
        if len(pts_snapshot) < 2:
            continue

        drops = sum(
            1 for i in range(1, len(pts_snapshot))
            if pts_snapshot[i] <= pts_snapshot[i - 1]
        )
        if drops:
            dropped_pts[cam_id] = dropped_pts.get(cam_id, 0) + drops

        monotonic: List[int] = [pts_snapshot[0]]
        for i in range(1, len(pts_snapshot)):
            if pts_snapshot[i] > monotonic[-1]:
                monotonic.append(pts_snapshot[i])
        if len(monotonic) < 2:
            continue

        dt_ns = monotonic[-1] - monotonic[0]
        n_frames = len(monotonic) - 1
        # A real source spans at least ~1 ms between frames.  Sub-ms
        # spans come from garbage PTS (e.g. fully out-of-order sequences
        # collapsing to a near-zero window) — treat as invalid.
        if dt_ns < 1_000_000 or dt_ns <= 0:
            continue

        measured_src_fps = round((n_frames * 1e9) / dt_ns, 1)
        src_pts_fps[cam_id] = measured_src_fps

        cb_fps = raw_cb_fps.get(cam_id, 0.0)
        if cb_fps > measured_src_fps:
            fps[cam_id] = measured_src_fps
            burst_cams[cam_id] = round(cb_fps - measured_src_fps, 1)
            fps_bound_by[cam_id] = "pts"

    # Configured-fps bound (PTS-unavailable fallback): for cameras whose
    # ring had <2 valid PTS entries (live CSI/USB with no buf_pts, sparse
    # window, or all-OOO timestamps), bound published FPS by the authored
    # native FPS (CameraConfig.fps).  PTS evidence is more precise, so
    # cameras that already won in the PTS pass skip this.  Mirror of the
    # storage-time filter in production _tick_fps (only positive values
    # ever reach the writer's configured-fps cache).
    for cam_id, cfg_fps in configured_fps.items():
        if cfg_fps <= 0:
            continue
        if cam_id in src_pts_fps:
            continue
        cb_fps = raw_cb_fps.get(cam_id, 0.0)
        if cb_fps > cfg_fps:
            fps[cam_id] = cfg_fps
            burst_cams[cam_id] = round(cb_fps - cfg_fps, 1)
            fps_bound_by[cam_id] = "configured"

    # _input_fps: PTS-derived source rate when available, else bounded fps.
    input_fps: Dict[str, float] = {
        cam_id: src_pts_fps.get(cam_id, fps[cam_id])
        for cam_id in fps
    }

    return {
        "fps": fps,
        "raw_cb_fps": raw_cb_fps,
        "src_pts_fps": src_pts_fps,
        "input_fps": input_fps,
        "fps_burst": burst_cams,
        "pts_dropped": dropped_pts,
        "fps_bound_by": fps_bound_by,
    }


def _make_pts_sequence(fps: float, n_frames: int, start_ns: int = 0) -> List[int]:
    """
    Build a monotonic PTS sequence at `fps` Hz, `n_frames` frames,
    starting at `start_ns`.  Returns list of PTS values (ns).
    """
    step_ns = int(1e9 / fps)
    return [start_ns + i * step_ns for i in range(n_frames)]


# ── Fixtures ────────────────────────────────────────────────────────────────

WINDOW_DUR = 1.0  # TELEMETRY_INTERVAL default


@pytest.fixture
def ring_factory():
    """
    Returns a callable that produces a fresh defaultdict(deque) ring.
    Each call resets the factory so tests are isolated.
    """
    rings: Dict[str, deque] = defaultdict(
        lambda: deque(maxlen=_PTS_RING_MAXLEN)
    )
    return rings


# ── Test 1: burst callbacks with 30fps PTS → reported fps ≤~30 ─────────────

def test_burst_30fps_pts_bounds_fps(ring_factory):
    """
    Simulate a 30fps source where the writer sees 45 OSD callbacks in one
    window (burst delivery).  PTS sequence is the canonical 30fps sequence
    for 45 frames.

    Expected:
      raw_cb_fps   = 45 / 1.0 = 45.0
      src_pts_fps  ≈ 30.0  (45 frames at 33.3ms each = 1.35s span)
      fps (published) ≤ 30.0  (bounded by PTS source rate)
      fps_burst present with delta ≈ 15.0
    """
    cam = "cam_01"
    frame_counts = {cam: 45}
    ring = ring_factory
    # 45 frames of true 30fps source (each 33.33ms apart).
    # No pre-fill — window is isolated, self-contained.
    ring[cam].extend(_make_pts_sequence(30.0, 45, 0))

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["raw_cb_fps"][cam] == 45.0, (
        "raw callback fps must reflect OSD callbacks, not PTS"
    )
    assert result["src_pts_fps"][cam] == pytest.approx(30.0, abs=0.1), (
        f"source_pts_fps should be ~30.0, got {result['src_pts_fps'].get(cam)}"
    )
    assert result["fps"][cam] == pytest.approx(30.0, abs=0.1), (
        f"published fps must be bounded by PTS source rate, got {result['fps'][cam]}"
    )
    assert result["fps_burst"][cam] == pytest.approx(15.0, abs=0.1), (
        f"fps_burst should be ~15.0, got {result['fps_burst'].get(cam)}"
    )


def test_raw_callback_fps_still_exceeds_source_on_burst(ring_factory):
    """
    Sanity check: the raw_callback_fps telemetry field must be present
    and strictly greater than source_pts_fps when a burst occurs.
    """
    cam = "cam_02"
    frame_counts = {cam: 60}
    ring = ring_factory
    ring[cam].extend(_make_pts_sequence(30.0, 60, 0))

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["raw_cb_fps"][cam] > result["src_pts_fps"].get(cam, 0.0)
    assert result["fps_burst"][cam] is not None


# ── Test 2: PTS-unavailable fallback → callback-only path ──────────────────

def test_pts_unavailable_fallback_preserves_callback_rate(ring_factory):
    """
    When buf_pts is 0 / None the ring has < 2 entries and the writer must
    return the raw callback FPS unchanged (no clamp applied).
    """
    cam = "cam_03"
    frame_counts = {cam: 45}
    ring = ring_factory
    # Ring has 0 entries (pts_ns was falsy for every frame) — same as PTS
    # never arriving from this source.
    assert len(ring[cam]) == 0

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["fps"][cam] == 45.0, (
        "PTS unavailable → callback rate must pass through unchanged"
    )
    assert cam not in result["src_pts_fps"], (
        "src_pts_fps must not contain cameras without PTS evidence"
    )
    assert result["fps_burst"] == {}, (
        "fps_burst must be empty when no camera was burst-clamped"
    )


def test_single_pts_entry_keeps_callback_rate(ring_factory):
    """
    Ring has exactly 1 entry → <2 valid → no PTS measurement, callback
    rate preserved.
    """
    cam = "cam_single"
    frame_counts = {cam: 30}
    ring = ring_factory
    ring[cam].append(1_000_000_000)  # one frame only

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["fps"][cam] == 30.0
    assert cam not in result["src_pts_fps"]


# ── Test 3: no-burst case — PTS and callback agree ─────────────────────────

def test_no_burst_pts_and_callback_match(ring_factory):
    """
    When delivery matches source rate, callback FPS == PTS-measured FPS.
    fps_burst must be empty.
    """
    cam = "cam_match"
    n_frames = 30
    frame_counts = {cam: n_frames}
    ring = ring_factory
    ring[cam].extend(_make_pts_sequence(30.0, n_frames, 0))

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["fps"][cam] == pytest.approx(30.0, abs=0.1)
    assert result["src_pts_fps"][cam] == pytest.approx(30.0, abs=0.1)
    assert result["fps_burst"] == {}, (
        "no burst when callback rate ≤ source rate"
    )


# ── Test 4: out-of-order PTS counted but does not crash ────────────────────

def test_out_of_order_pts_counted_in_dropped(ring_factory):
    """
    Insert a non-monotonic PTS value (simulating encoder reset /
    wraparound).  The drop must be recorded in pts_dropped and the
    monotonic subsequence must still produce a valid measurement.
    """
    cam = "cam_ooo"
    frame_counts = {cam: 20}
    ring = ring_factory
    # Build a clean 20fps sequence, then inject a reset in the middle.
    pts = _make_pts_sequence(20.0, 10, 0)
    # Encoder reset: next PTS drops back to near-zero.
    pts += [10_000_000, 60_000_000, 110_000_000, 160_000_000, 210_000_000,
            260_000_000, 300_000_000, 350_000_000, 400_000_000, 450_000_000]
    ring[cam].extend(pts)

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["pts_dropped"][cam] == 1, (
        f"expected 1 PTS drop (reset), got {result['pts_dropped'].get(cam)}"
    )
    # After dropping the bad value, measurement should still be valid.
    assert cam in result["src_pts_fps"]
    assert result["src_pts_fps"][cam] > 0.0


def test_all_pts_out_of_order_keeps_callback_rate(ring_factory):
    """
    If every PTS is out-of-order (e.g. encoder never timestamped),
    the monotonic subsequence has length 1 and the writer keeps the
    callback rate for that camera.
    """
    cam = "cam_all_ooo"
    frame_counts = {cam: 25}
    ring = ring_factory
    # Completely non-monotonic: [10, 5, 8, 3, 7, ...]
    ring[cam].extend([10, 5, 8, 3, 7, 2, 9, 1, 6, 4, 11, 0])

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["fps"][cam] == 25.0, (
        "all-OOO PTS → must keep callback rate"
    )
    assert cam not in result["src_pts_fps"]


# ── Test 5: multi-camera isolation ─────────────────────────────────────────

def test_multi_camera_pts_independent(ring_factory):
    """
    Each camera's PTS ring is independent — a burst on cam_A must not
    affect the published FPS of cam_B.
    """
    cam_a = "cam_A"
    cam_b = "cam_B"
    frame_counts = {cam_a: 60, cam_b: 30}   # A burst, B normal
    ring = ring_factory
    # A: 60 callbacks, true 30fps source → burst
    ring[cam_a].extend(_make_pts_sequence(30.0, 60, 0))
    # B: 30 callbacks, true 30fps source → match
    ring[cam_b].extend(_make_pts_sequence(30.0, 30, 0))

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["fps"][cam_a] == pytest.approx(30.0, abs=0.1)
    assert result["fps"][cam_b] == pytest.approx(30.0, abs=0.1)
    assert result["raw_cb_fps"][cam_a] == 60.0
    assert result["raw_cb_fps"][cam_b] == 30.0
    assert cam_a in result["fps_burst"]
    assert cam_b not in result["fps_burst"]


# ── Test 6: low-fps source (10 fps) with same burst pattern ─────────────────

def test_low_fps_source_10fps_pts_bound(ring_factory):
    """
    Source at 10 fps, writer sees 20 callbacks (2× burst).  Published FPS
    must be ~10, not 20.
    """
    cam = "cam_10fps"
    frame_counts = {cam: 20}
    ring = ring_factory
    ring[cam].extend(_make_pts_sequence(10.0, 20, 0))

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert result["fps"][cam] == pytest.approx(10.0, abs=0.1)
    assert result["raw_cb_fps"][cam] == 20.0
    assert result["fps_burst"][cam] == pytest.approx(10.0, abs=0.1)


# ── Test 7: telemetry dict round-trip ──────────────────────────────────────

def test_telemetry_fields_serialisable(ring_factory):
    """
    All telemetry sub-dicts must be JSON-safe (ints, floats, strings).
    """
    cam = "cam_json"
    frame_counts = {cam: 45}
    ring = ring_factory
    ring[cam].extend(_make_pts_sequence(30.0, 45, 0))

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    import json
    payload = json.dumps(result)
    parsed = json.loads(payload)
    assert isinstance(parsed["raw_cb_fps"], dict)
    assert isinstance(parsed["src_pts_fps"], dict)
    assert isinstance(parsed["fps_burst"], dict)
    assert isinstance(parsed["pts_dropped"], dict)


# ── Test 8: _input_fps is PTS-derived source rate when available ────────────

def test_input_fps_reflects_pts_source_rate_when_available(ring_factory):
    """When PTS evidence is valid, _input_fps must equal src_pts_fps
    (the native source rate), NOT the bounded output fps."""
    cam = "cam_input_pts"
    # Burst: 60 callbacks but only 30fps PTS.
    frame_counts = {cam: 60}
    ring = ring_factory
    ring[cam].extend(_make_pts_sequence(30.0, 60, 0))

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    # Output fps is bounded by PTS source rate.
    assert result["fps"][cam] == pytest.approx(30.0, abs=0.1)
    # src_pts_fps carries the PTS-measured rate.
    assert result["src_pts_fps"][cam] == pytest.approx(30.0, abs=0.1)
    # _input_fps must reflect src_pts_fps, not the raw callback rate.
    assert result["input_fps"][cam] == pytest.approx(30.0, abs=0.1)
    assert result["input_fps"][cam] != result["raw_cb_fps"][cam], (
        "_input_fps should differ from raw callback fps (60) when PTS is available"
    )


def test_input_fps_fallback_to_bounded_output_when_pts_unavailable(ring_factory):
    """When PTS is unavailable (ring empty), _input_fps must equal fps
    (the bounded output fps) — conservative, never None."""
    cam = "cam_noPTS"
    frame_counts = {cam: 45}
    ring = ring_factory
    # No PTS appended → ring empty.
    assert len(ring[cam]) == 0

    result = _compute_pts_fps_bound(frame_counts, ring, WINDOW_DUR)

    assert cam not in result["src_pts_fps"], "no PTS evidence → src_pts_fps must be absent"
    assert result["input_fps"][cam] == result["fps"][cam] == 45.0, (
        "PTS unavailable → input_fps must fall back to bounded output fps"
    )


# ── Test 9: ring drain prevents stale removed-camera state ──────────────────

def test_ring_drain_clears_stale_camera_state(ring_factory):
    """After one writer window the ring is cleared.  A camera that stops
    sending frames has no PTS evidence in the next window — it keeps its
    callback rate (the safe fallback) instead of using a stale measurement."""
    cam = "cam_removed"
    ring = ring_factory

    # Window 1: camera active with 30fps PTS.
    ring[cam].extend(_make_pts_sequence(30.0, 30, 0))
    result1 = _compute_pts_fps_bound({cam: 30}, ring, WINDOW_DUR)
    assert result1["src_pts_fps"][cam] == pytest.approx(30.0, abs=0.1)

    # After window 1 the helper has cleared the ring (drain).
    assert len(ring[cam]) == 0, "ring must be empty after window drain"

    # Window 2: camera gone — no new PTS, but still has a callback count
    # (e.g. last few buffered frames).  PTS evidence must be absent; the
    # writer falls back to callback rate.
    result2 = _compute_pts_fps_bound({cam: 5}, ring, WINDOW_DUR)
    assert cam not in result2["src_pts_fps"], (
        "no PTS after drain → src_pts_fps must be absent in window 2"
    )
    assert result2["fps"][cam] == 5.0, "fallback to callback rate when ring empty"


# ── Test 10: configured-fps bound when PTS is unavailable ────────────────────
# (Jetson live CSI/USB: buf_pts=0/None, raw callback rate bursts above 30)

def test_no_pts_burst_clamped_to_configured_fps_30(ring_factory):
    """Missing PTS + 45 callback frames in 1 s + configured fps=30 →
    published FPS = 30.0 (not the raw 45)."""
    cam = "cam_cfg30_burst"
    frame_counts = {cam: 45}
    ring = ring_factory
    # Ring empty → no PTS evidence.
    result = _compute_pts_fps_bound(
        frame_counts, ring, WINDOW_DUR,
        configured_fps={cam: 30.0},
    )
    assert result["fps"][cam] == 30.0, (
        f"expected 30.0 (configured-fps bound), got {result['fps'][cam]}"
    )
    assert result["raw_cb_fps"][cam] == 45.0, "raw_cb_fps must not be clamped"
    assert cam not in result["src_pts_fps"]
    assert result["fps_bound_by"][cam] == "configured"
    assert result["fps_burst"][cam] == pytest.approx(15.0, abs=0.1)


def test_no_pts_callback_below_configured_unchanged(ring_factory):
    """Missing PTS + 20 callback frames + configured fps=30 → callback
    rate (20) ≤ configured fps (30), so no clamping occurs."""
    cam = "cam_cfg30_normal"
    frame_counts = {cam: 20}
    ring = ring_factory
    result = _compute_pts_fps_bound(
        frame_counts, ring, WINDOW_DUR,
        configured_fps={cam: 30.0},
    )
    assert result["fps"][cam] == 20.0
    assert cam not in result["fps_bound_by"]
    assert result["fps_burst"] == {}


def test_valid_30fps_pts_wins_over_configured_fps(ring_factory):
    """Valid 30fps PTS sequence + configured fps=30 → PTS path wins;
    published FPS ~30, fps_bound_by='pts'."""
    cam = "cam_pts_win"
    frame_counts = {cam: 45}
    ring = ring_factory
    ring[cam].extend(_make_pts_sequence(30.0, 45, 0))
    result = _compute_pts_fps_bound(
        frame_counts, ring, WINDOW_DUR,
        configured_fps={cam: 30.0},
    )
    assert result["fps"][cam] == pytest.approx(30.0, abs=0.1)
    assert result["src_pts_fps"][cam] == pytest.approx(30.0, abs=0.1)
    assert result["fps_bound_by"][cam] == "pts"
    # The PTS path already knows this is a burst; configured-fps pass
    # must NOT have overwritten fps_bound_by.
    assert "configured" not in result["fps_bound_by"].values()


def test_configured_fps_zero_ignored_falls_back_to_callback(ring_factory):
    """configured_fps=0 or None → pass is skipped, callback rate
    preserved (same as PTS-unavailable legacy behaviour)."""
    cam = "cam_nocfg"
    frame_counts = {cam: 50}
    ring = ring_factory
    result = _compute_pts_fps_bound(
        frame_counts, ring, WINDOW_DUR,
        configured_fps={cam: 0.0},  # explicitly zero
    )
    assert result["fps"][cam] == 50.0
    assert result["fps_bound_by"] == {}


def test_configured_fps_no_entry_in_dict_falls_back_to_callback(ring_factory):
    """Camera not in configured_fps dict → callback rate preserved
    (no configured value to bound by)."""
    cam = "cam_uncfg"
    frame_counts = {cam: 50}
    ring = ring_factory
    result = _compute_pts_fps_bound(
        frame_counts, ring, WINDOW_DUR,
        configured_fps={"other_cam": 30.0},
    )
    assert result["fps"][cam] == 50.0
    assert cam not in result["fps_bound_by"]


def test_configured_fps_respects_nonstandard_fps_value(ring_factory):
    """configured_fps=15 (not 30) → bound to 15 when PTS unavailable."""
    cam = "cam_15fps"
    frame_counts = {cam: 40}
    ring = ring_factory
    result = _compute_pts_fps_bound(
        frame_counts, ring, WINDOW_DUR,
        configured_fps={cam: 15.0},
    )
    assert result["fps"][cam] == 15.0
    assert result["fps_bound_by"][cam] == "configured"
    assert result["fps_burst"][cam] == pytest.approx(25.0, abs=0.1)


def test_multi_camera_configured_fps_independent(ring_factory):
    """Each camera bound independently: cam_A burst, cam_B below configured."""
    cam_a = "cam_A"
    cam_b = "cam_B"
    frame_counts = {cam_a: 60, cam_b: 20}
    ring = ring_factory
    # No PTS for either → both use configured-fps fallback.
    cfg = {cam_a: 30.0, cam_b: 30.0}
    result = _compute_pts_fps_bound(
        frame_counts, ring, WINDOW_DUR, configured_fps=cfg,
    )
    assert result["fps"][cam_a] == 30.0
    assert result["fps_bound_by"][cam_a] == "configured"
    assert result["fps"][cam_b] == 20.0
    assert cam_b not in result["fps_bound_by"]
    assert result["raw_cb_fps"][cam_a] == 60.0
    assert result["raw_cb_fps"][cam_b] == 20.0