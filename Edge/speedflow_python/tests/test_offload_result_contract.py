"""
Edge/speedflow_python/tests/test_offload_result_contract.py

Deterministic host-side tests for the sender/probe-side offload result
contract in probes.py (SpeedProbe):

  • Results with inference_ok=False (receiver's explicit inference-failure
    flag) are rejected at inject_offload_result — never queued, never
    injected into the OSD overlay — and counted in results_rejected.
  • Valid observations remain accepted even when plate_text is empty or
    confidence is 0 — empty plate_text still locks the track so it is not
    re-offloaded.
  • Legacy payloads lacking inference_ok are treated as valid (backward
    compatible).
  • Rejection counter is surfaced in _snapshot_offload_crops as
    results_rejected (next to results_received).

No GStreamer / gi / pyds / speedflow_cpp.so required — heavy deps are
stubbed, only probes.py itself is loaded.

Run:
    conda run -n DoAn python3 speedflow_python/tests/test_offload_result_contract.py
    conda run -n DoAn python3 -m pytest speedflow_python/tests/test_offload_result_contract.py -q
"""

import sys
import types
import importlib.util
from pathlib import Path

import pytest

_EDGE = Path(__file__).resolve().parents[2]  # .../Edge


def _load_probes():
    # Stub the package so probes.py's relative imports resolve without
    # executing speedflow_python/__init__.py (which needs gi).
    pkg = sys.modules.get("speedflow_python")
    if pkg is None:
        pkg = types.ModuleType("speedflow_python")
        pkg.__path__ = [str(_EDGE / "speedflow_python")]
        sys.modules["speedflow_python"] = pkg

    # Native C++ binding (speedflow_cpp.so) — not available on host.
    sf = sys.modules.get("speedflow_python.speedflow_c")
    if sf is None:
        sf = types.ModuleType("speedflow_python.speedflow_c")
        sys.modules["speedflow_python.speedflow_c"] = sf

    # GStreamer / DeepStream bindings.
    gi = sys.modules.get("gi")
    if gi is None:
        gi = types.ModuleType("gi")
        gi.require_version = lambda *a, **kw: None
        sys.modules["gi"] = gi
    grepo = sys.modules.get("gi.repository")
    if grepo is None:
        grepo = types.ModuleType("gi.repository")
        sys.modules["gi.repository"] = grepo
    gst = sys.modules.get("gi.repository.Gst")
    if gst is None:
        gst = types.ModuleType("gi.repository.Gst")
        gst.PadProbeReturn = types.SimpleNamespace(OK="OK", DROP="DROP")
        sys.modules["gi.repository.Gst"] = gst
    if sys.modules.get("pyds") is None:
        sys.modules["pyds"] = types.ModuleType("pyds")

    # settings — only the names probes.py imports at module load matter.
    settings = sys.modules.get("speedflow_python.settings")
    if settings is None:
        settings = types.ModuleType("speedflow_python.settings")
        sys.modules["speedflow_python.settings"] = settings
    settings.VEHICLE_CLASS_IDS = {2, 3, 5, 7}
    settings.SPEED_LOG = "/tmp/test_speed.csv"
    settings.JPEG_QUALITY = 85
    settings.SNAP_DIR = Path("/tmp/test_snap")
    settings.MAX_SNAPSHOT_PER_ID = 3
    settings.MIN_WORLD_DISPL_M = 0.5
    settings.MAX_ABS_KMH = 300.0
    settings.BBOX_AREA_JUMP = 4.0
    settings.MIN_DET_CONF = 0.3
    settings.MEDIAN_WINDOW = 15
    settings.LICENSE_PLATE_CLASS_IDS = {0}
    settings.FPS_STATS_FILE = "/tmp/test_fps.json"
    settings.NODE_ID = "test_node"
    settings.TELEMETRY_INTERVAL = 3600.0  # writer loop effectively never fires

    draw = sys.modules.get("speedflow_python.draw")
    if draw is None:
        draw = types.ModuleType("speedflow_python.draw")
        sys.modules["speedflow_python.draw"] = draw
    draw.add_polygon_display = lambda *a, **kw: None

    camcfg = sys.modules.get("speedflow_python.camera_config")
    if camcfg is None:
        camcfg = types.ModuleType("speedflow_python.camera_config")
        sys.modules["speedflow_python.camera_config"] = camcfg
    camcfg.CameraManager = object
    camcfg.CameraConfig = object

    mod_name = "speedflow_python.probes"
    path = _EDGE / "speedflow_python/probes.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_probes = _load_probes()
SpeedProbe = _probes.SpeedProbe


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_probe():
    """SpeedProbe with a dummy camera_manager (never touched by these paths)."""
    probe = SpeedProbe(camera_manager=object())
    return probe


def _result(stid=(0, 1), plate_text="ABC123", confidence=0.95,
            inference_ok=None, **extra):
    """Receiver result payload. inference_ok=None → legacy (key absent)."""
    r = {
        "stid": list(stid),
        "camera_id": "cam0",
        "frame_no": 100,
        "plate_text": plate_text,
        "confidence": confidence,
        "ts": 1234567890.0,
    }
    if inference_ok is not None:
        r["inference_ok"] = inference_ok
    r.update(extra)
    return r


def _inject_and_drain(probe, result):
    probe.inject_offload_result(result)
    probe._drain_offload_results()


# ---------------------------------------------------------------------------
# Injection: acceptance / rejection contract
# ---------------------------------------------------------------------------

def test_inject_rejects_inference_ok_false():
    probe = _make_probe()
    probe.inject_offload_result(_result(plate_text="FAIL", inference_ok=False))
    assert probe._offload_result_q.qsize() == 0
    assert probe._results_received == 0
    assert probe._results_rejected == 1


def test_inject_accepts_inference_ok_true():
    probe = _make_probe()
    probe.inject_offload_result(_result(plate_text="OK1", inference_ok=True))
    assert probe._offload_result_q.qsize() == 1
    assert probe._results_received == 1
    assert probe._results_rejected == 0


def test_inject_accepts_legacy_payload_without_inference_ok():
    """Old receiver payload (no inference_ok key) → valid."""
    probe = _make_probe()
    probe.inject_offload_result(_result(plate_text="LEGACY"))
    assert probe._offload_result_q.qsize() == 1
    assert probe._results_received == 1
    assert probe._results_rejected == 0


def test_inject_accepts_inference_ok_true_empty_plate_text():
    """Valid inference with empty plate_text / confidence 0 → accepted."""
    probe = _make_probe()
    probe.inject_offload_result(_result(plate_text="", confidence=0.0,
                                        inference_ok=True))
    assert probe._offload_result_q.qsize() == 1
    assert probe._results_received == 1
    assert probe._results_rejected == 0


def test_inject_rejects_false_with_any_fields():
    """inference_ok=False wins regardless of plate_text/confidence present."""
    probe = _make_probe()
    probe.inject_offload_result(_result(plate_text="", confidence=0.0,
                                        inference_ok=False))
    probe.inject_offload_result(_result(plate_text="X", confidence=0.9,
                                        inference_ok=False))
    assert probe._offload_result_q.qsize() == 0
    assert probe._results_received == 0
    assert probe._results_rejected == 2


def test_inject_mixed_batch_counts():
    probe = _make_probe()
    for r in (
        _result(plate_text="A", inference_ok=True),
        _result(plate_text="B"),                       # legacy → valid
        _result(plate_text="C", inference_ok=False),   # rejected
        _result(plate_text="", inference_ok=True),     # empty text → valid
        _result(plate_text="D", inference_ok=False),   # rejected
    ):
        probe.inject_offload_result(r)
    assert probe._offload_result_q.qsize() == 3
    assert probe._results_received == 3
    assert probe._results_rejected == 2


# ---------------------------------------------------------------------------
# Drain: injection into OSD overlay (plate_locked)
# ---------------------------------------------------------------------------

def test_drain_locks_nonempty_text():
    probe = _make_probe()
    _inject_and_drain(probe, _result(stid=(0, 7), plate_text="XYZ999"))
    assert probe.plate_locked[(0, 7)] == "XYZ999"


def test_drain_accepts_empty_plate_text_locks_track():
    """Empty plate_text still locks the track (no re-offload), no crash."""
    probe = _make_probe()
    _inject_and_drain(probe, _result(stid=(1, 2), plate_text="",
                                     confidence=0.0, inference_ok=True))
    assert (1, 2) in probe.plate_locked
    assert probe.plate_locked[(1, 2)] == ""


def test_drain_skips_result_without_stid():
    probe = _make_probe()
    _inject_and_drain(probe, _result(stid=[], plate_text="NOSTID"))
    assert len(probe.plate_locked) == 0


def test_drain_rejected_result_never_injected():
    """inference_ok=False must not touch plate_locked at all."""
    probe = _make_probe()
    _inject_and_drain(probe, _result(stid=(3, 4), plate_text="SHOULD_NOT",
                                     inference_ok=False))
    assert len(probe.plate_locked) == 0


def test_drain_multiple_results_all_applied():
    probe = _make_probe()
    for r in (
        _result(stid=(0, 1), plate_text="AAA"),
        _result(stid=(0, 2), plate_text=""),
        _result(stid=(0, 3), plate_text="CCC", inference_ok=True),
    ):
        probe.inject_offload_result(r)
    probe._drain_offload_results()
    assert probe.plate_locked[(0, 1)] == "AAA"
    assert probe.plate_locked[(0, 2)] == ""
    assert probe.plate_locked[(0, 3)] == "CCC"
    assert probe._offload_result_q.qsize() == 0


# ---------------------------------------------------------------------------
# Telemetry snapshot
# ---------------------------------------------------------------------------

def test_snapshot_exposes_results_rejected():
    probe = _make_probe()
    probe.inject_offload_result(_result(plate_text="X", inference_ok=False))
    probe.inject_offload_result(_result(plate_text="Y"))
    snap, _, _ = probe._snapshot_offload_crops(prev_count=0, prev_ts=0.0)
    assert snap["results_received"] == 1
    assert snap["results_rejected"] == 1


def test_snapshot_zero_defaults():
    probe = _make_probe()
    snap, _, _ = probe._snapshot_offload_crops(prev_count=0, prev_ts=0.0)
    assert snap["results_received"] == 0
    assert snap["results_rejected"] == 0
    # Receiver offload_inference_errors_count is surfaced in the snapshot.
    assert snap["offload_inference_errors_count"] == 0


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        test_inject_rejects_inference_ok_false,
        test_inject_accepts_inference_ok_true,
        test_inject_accepts_legacy_payload_without_inference_ok,
        test_inject_accepts_inference_ok_true_empty_plate_text,
        test_inject_rejects_false_with_any_fields,
        test_inject_mixed_batch_counts,
        test_drain_locks_nonempty_text,
        test_drain_accepts_empty_plate_text_locks_track,
        test_drain_skips_result_without_stid,
        test_drain_rejected_result_never_injected,
        test_drain_multiple_results_all_applied,
        test_snapshot_exposes_results_rejected,
        test_snapshot_zero_defaults,
    ]

    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
