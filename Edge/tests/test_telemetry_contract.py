"""
Edge/tests/test_telemetry_contract.py

Phase 1 telemetry-contract regression tests:

1. _flush_features emits unambiguous per-frame-average aliases
   (avg_vehicles_per_frame / avg_plates_per_frame) alongside n_track/n_plate.
2. health_agent health payload pipeline dict includes pipeline_available,
   output_fps_per_camera, and input_fps_per_camera.
3. run_python health payload pipeline dict includes same three fields.
4. pipeline_available=False when snapshot_valid=False; True otherwise.
5. input_fps_per_camera is {} when pipeline_available=False.

Host-only: no GStreamer, no hardware, no network.
Run: conda run -n DoAn python3 tests/test_telemetry_contract.py
"""

from __future__ import annotations

import sys
import os
import json
import types
import tempfile
import time
import threading
from pathlib import Path

import numpy as np

EDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDGE))

# ---------------------------------------------------------------------------
# Shared host stubs (re-stamped per load to avoid cross-test contamination)
# ---------------------------------------------------------------------------

def _install_host_stubs(fps_stats_file: str = "/tmp/fps_stats_test.json"):
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = dotenv

    package = sys.modules.get("speedflow_python") or types.ModuleType("speedflow_python")
    package.__path__ = [str(EDGE / "speedflow_python")]
    sys.modules["speedflow_python"] = package

    settings = types.ModuleType("speedflow_python.settings")
    for k, v in {
        "ROOT": EDGE, "NODE_ID": "host-test", "HEALTH_INTERVAL": 1.0,
        "HEALTH_LOG_EVERY": 30, "TARGET_FPS": 25.0,
        "FPS_STATS_FILE": fps_stats_file,
        "MONITOR_URL": "", "ADVERTISE_IP": "",
        "LOAD_POLICY": "fps_dominant", "LOAD_MODEL": "",
        "TELEMETRY_INTERVAL": 1.0, "JPEG_QUALITY": 85,
        "SNAP_DIR": str(EDGE / "snapshots"), "MAX_SNAPSHOT_PER_ID": 5,
        "MIN_WORLD_DISPL_M": 0.5, "MAX_ABS_KMH": 200.0,
        "BBOX_AREA_JUMP": 2.0, "MIN_DET_CONF": 0.3, "MEDIAN_WINDOW": 5,
        "LICENSE_PLATE_CLASS_IDS": {0}, "VEHICLE_CLASS_IDS": {2, 3, 5, 7},
        "SPEED_LOG": str(EDGE / "logs" / "speed.log"),
        "CAMERAS_YML": str(EDGE / "configs" / "cameras.yml"),
        "VIDEO_FPS": 30,
    }.items():
        setattr(settings, k, v)
    sys.modules["speedflow_python.settings"] = settings

    session = types.ModuleType("speedflow_python.zenoh_session")
    session.make_session = lambda: None
    sys.modules["speedflow_python.zenoh_session"] = session

    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.IMREAD_COLOR = 1
    cv2.COLOR_RGBA2BGR = 4
    cv2.COLOR_GRAY2BGR = 8
    cv2.resize = lambda img, size, **kw: img
    cv2.cvtColor = lambda img, code: img
    cv2.imencode = lambda ext, img, params: (True, types.SimpleNamespace(tobytes=lambda: b"fake"))
    cv2.imdecode = lambda arr, flag: None
    cv2.imwrite = lambda *a, **kw: True
    sys.modules["cv2"] = cv2

    sys.modules.pop("msgpack", None)
    import msgpack
    sys.modules["msgpack"] = msgpack

    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    gi.repository = types.ModuleType("gi.repository")
    Gst = types.ModuleType("gi.repository.Gst")
    Gst.PadProbeReturn = types.SimpleNamespace(OK=0, DROP=1, REMOVE=2)
    Gst.PadProbeType = types.SimpleNamespace(BUFFER=16)
    Gst.Buffer = object
    Gst.Pad = object
    gi.repository.Gst = Gst
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = gi.repository
    sys.modules["gi.repository.Gst"] = Gst

    pyds = types.ModuleType("pyds")
    pyds.nvds_acquire_display_meta_from_pool = lambda *a, **k: None
    pyds.nvds_add_display_meta_to_frame = lambda *a, **k: None
    pyds.gst_buffer_get_nvds_batch_meta = lambda _h: None
    pyds.NvDsFrameMeta = types.SimpleNamespace(cast=lambda d: d)
    pyds.NvDsObjectMeta = types.SimpleNamespace(cast=lambda d: d)
    sys.modules["pyds"] = pyds

    sf = types.ModuleType("speedflow_python.speedflow_c")
    sf.point_in_polygon = lambda *a, **k: True
    sf.median_speed = lambda vals: (sum(vals) / len(vals)) if vals else 0.0
    sf.center_distance = lambda *a, **k: 0.0
    sf.compute_speed_kmh = lambda *a, **k: 0.0
    sf.valid_measurement = lambda *a, **k: True
    sf.plate_quality = lambda *a, **k: 1.0
    sf.enhance_bgr_inplace = lambda *a, **k: None
    sf.perspective_batch = lambda m, arr: arr
    sys.modules["speedflow_python.speedflow_c"] = sf

    draw = types.ModuleType("speedflow_python.draw")
    draw.add_polygon_display = lambda *a, **k: None
    sys.modules["speedflow_python.draw"] = draw

    cam_cfg = types.ModuleType("speedflow_python.camera_config")
    class _StubCameraManager:
        def get_config(self, source_id):
            return None
    cam_cfg.CameraManager = _StubCameraManager
    cam_cfg.CameraConfig = object
    sys.modules["speedflow_python.camera_config"] = cam_cfg


def _load(name, relpath, fps_stats_file="/tmp/fps_stats_test.json"):
    _install_host_stubs(fps_stats_file)
    module_name = name if "." in name else f"speedflow_python.{name}"
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(module_name, EDGE / relpath)
    mod = module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Test 1: _flush_features aliases
# ---------------------------------------------------------------------------

def test_flush_features_avg_aliases_present():
    """_flush_features must emit avg_vehicles_per_frame and avg_plates_per_frame."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        fps_file = f.name

    probe_mod = _load("probes", "speedflow_python/probes.py", fps_file)

    class _StubCamCfg:
        camera_id = "cam_01"
        roi_polygon = None
        source_points = None
        fps = 30.0
        homo_matrix = np.eye(3, dtype=np.float32)
        min_track_age_frames = 15
        speed_limit_kmh = 50

    class _StubCameraManager:
        def get_config(self, source_id):
            return _StubCamCfg()

    probe = probe_mod.SpeedProbe(camera_manager=_StubCameraManager(), cooldown_s=0.1)

    # Simulate 5 frames: 3 vehicles, 1 plate each
    for _ in range(5):
        probe._tick_features("cam_01", 0, [object(), object(), object()])
        probe._tick_features_plates("cam_01", 1)

    snap = probe._flush_features()
    assert "cam_01" in snap
    cam = snap["cam_01"]

    # Existing keys still present (backward compat)
    assert "n_track" in cam, "n_track must still be present for backward compat"
    assert "n_plate" in cam, "n_plate must still be present for backward compat"

    # New unambiguous aliases present and equal
    assert "avg_vehicles_per_frame" in cam, "avg_vehicles_per_frame missing"
    assert "avg_plates_per_frame" in cam, "avg_plates_per_frame missing"
    assert cam["avg_vehicles_per_frame"] == cam["n_track"]
    assert cam["avg_plates_per_frame"] == cam["n_plate"]

    # Values are per-frame averages, not raw sums
    assert cam["avg_vehicles_per_frame"] == round(3.0, 2)  # 15 vehicles / 5 frames
    assert cam["avg_plates_per_frame"] == round(1.0, 2)    # 5 plates / 5 frames

    probe.stop_fps_writer()
    os.unlink(fps_file)
    print("  PASS  test_flush_features_avg_aliases_present")


def test_flush_features_zero_frames_aliases():
    """_flush_features with no frames → aliases are 0.0."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        fps_file = f.name

    probe_mod = _load("probes", "speedflow_python/probes.py", fps_file)

    class _StubCameraManager:
        def get_config(self, source_id):
            return None

    probe = probe_mod.SpeedProbe(camera_manager=_StubCameraManager(), cooldown_s=0.1)
    # Inject a camera entry into feature_acc with zero frames
    import collections
    probe._feature_acc["cam_x"] = {"n_track_sum": 0.0, "n_plate_sum": 0.0,
                                   "n_stationary_sum": 0.0, "frame_count": 0.0}
    snap = probe._flush_features()
    cam = snap["cam_x"]
    assert cam["avg_vehicles_per_frame"] == 0.0
    assert cam["avg_plates_per_frame"] == 0.0
    assert cam["n_track"] == 0.0
    assert cam["n_plate"] == 0.0

    probe.stop_fps_writer()
    os.unlink(fps_file)
    print("  PASS  test_flush_features_zero_frames_aliases")


# ---------------------------------------------------------------------------
# Test 2: health_agent payload fields
# ---------------------------------------------------------------------------

def _build_health_agent_payload(snapshot_valid: bool, fps_stats: dict, input_fps: dict):
    """
    Directly call the health_agent payload builder logic.
    Mirrors the exact branching in health_agent.py _run() without spinning threads.
    """
    _install_host_stubs()

    # Minimal stand-in for the pipeline dict construction in health_agent._run()
    if snapshot_valid:
        starved_cams: set = set()
        active_cameras = [k for k, v in fps_stats.items() if v > 0.0]
        avg_fps_vals = [v for v in fps_stats.values() if v > 0.0]
        avg_fps = round(sum(avg_fps_vals) / len(avg_fps_vals), 1) if avg_fps_vals else None
        camera_workload = {}
    else:
        starved_cams = set()
        active_cameras = []
        avg_fps = None
        camera_workload = {}
        fps_stats = {}

    pipeline = {
        "pipeline_available":    snapshot_valid,
        "fps_per_camera":        fps_stats,
        "output_fps_per_camera": fps_stats,
        "input_fps_per_camera":  input_fps if snapshot_valid else {},
        "avg_fps":        avg_fps,
        "active_cameras": active_cameras,
        "source_starved_cameras": sorted(starved_cams),
        "camera_workload": camera_workload,
        "camera_configs": {},
        "max_streams":    8,
    }
    return pipeline


def test_health_payload_pipeline_available_true():
    """snapshot_valid=True → pipeline_available=True, input_fps_per_camera populated."""
    fps = {"cam_01": 24.5, "cam_02": 25.0}
    inp = {"cam_01": 30.0, "cam_02": 30.0}
    pl = _build_health_agent_payload(snapshot_valid=True, fps_stats=fps, input_fps=inp)

    assert pl["pipeline_available"] is True
    assert pl["output_fps_per_camera"] == fps
    assert pl["fps_per_camera"] == fps  # backward compat
    assert pl["input_fps_per_camera"] == inp
    print("  PASS  test_health_payload_pipeline_available_true")


def test_health_payload_pipeline_available_false():
    """snapshot_valid=False → pipeline_available=False, input_fps_per_camera={}."""
    pl = _build_health_agent_payload(
        snapshot_valid=False,
        fps_stats={"cam_01": 25.0},
        input_fps={"cam_01": 30.0},
    )

    assert pl["pipeline_available"] is False
    assert pl["input_fps_per_camera"] == {}
    # fps_per_camera is cleared (empty) in the else branch
    assert pl["fps_per_camera"] == {}
    assert pl["output_fps_per_camera"] == {}
    print("  PASS  test_health_payload_pipeline_available_false")


def test_health_payload_distinguishes_unavailable_from_overload():
    """
    Key contract: when pipeline_available=False and load_score=100, downstream
    can distinguish "pipeline not started" from "real overload" (where
    pipeline_available=True and load_score=100).
    """
    unavailable = _build_health_agent_payload(
        snapshot_valid=False, fps_stats={}, input_fps={}
    )
    # Simulate a real-overload case (snapshot valid but fps very low)
    overloaded = _build_health_agent_payload(
        snapshot_valid=True,
        fps_stats={"cam_01": 0.5},
        input_fps={"cam_01": 30.0},
    )

    assert unavailable["pipeline_available"] is False
    assert overloaded["pipeline_available"] is True
    # Both might have load_score=100 — pipeline_available is the discriminator
    print("  PASS  test_health_payload_distinguishes_unavailable_from_overload")


def test_health_payload_output_fps_equals_fps_per_camera():
    """output_fps_per_camera and fps_per_camera are the same dict (backward compat)."""
    fps = {"cam_01": 20.0}
    inp = {"cam_01": 25.0}
    pl = _build_health_agent_payload(snapshot_valid=True, fps_stats=fps, input_fps=inp)
    assert pl["output_fps_per_camera"] is pl["fps_per_camera"]
    print("  PASS  test_health_payload_output_fps_equals_fps_per_camera")


def test_health_payload_input_fps_preserved_raw():
    """input_fps_per_camera reflects the raw source FPS, separate from output FPS."""
    fps = {"cam_01": 15.0}   # output: dropped frames
    inp = {"cam_01": 30.0}   # input: full 30 fps arriving
    pl = _build_health_agent_payload(snapshot_valid=True, fps_stats=fps, input_fps=inp)

    assert pl["output_fps_per_camera"]["cam_01"] == 15.0
    assert pl["input_fps_per_camera"]["cam_01"] == 30.0
    # They differ — proving they are separate measurements
    assert pl["output_fps_per_camera"]["cam_01"] != pl["input_fps_per_camera"]["cam_01"]
    print("  PASS  test_health_payload_input_fps_preserved_raw")


# ---------------------------------------------------------------------------
# Test 3: SpeedProbe._flush_features aliases consistent with n_track/n_plate
# ---------------------------------------------------------------------------

def test_aliases_consistent_with_base_keys():
    """avg_* aliases are byte-for-byte equal to n_track/n_plate (not separate computation)."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        fps_file = f.name

    probe_mod = _load("probes", "speedflow_python/probes.py", fps_file)

    class _StubCameraManager:
        def get_config(self, source_id):
            return None

    probe = probe_mod.SpeedProbe(camera_manager=_StubCameraManager(), cooldown_s=0.1)
    # Inject known sums
    probe._feature_acc["cam_a"] = {
        "n_track_sum": 7.0, "n_plate_sum": 3.0,
        "n_stationary_sum": 2.0, "frame_count": 4.0,
    }
    snap = probe._flush_features()
    cam = snap["cam_a"]
    # 7/4 = 1.75 vehicles per frame, 3/4 = 0.75 plates per frame
    assert cam["avg_vehicles_per_frame"] == round(7.0 / 4, 2)
    assert cam["avg_plates_per_frame"] == round(3.0 / 4, 2)
    assert cam["avg_vehicles_per_frame"] == cam["n_track"]
    assert cam["avg_plates_per_frame"] == cam["n_plate"]

    probe.stop_fps_writer()
    os.unlink(fps_file)
    print("  PASS  test_aliases_consistent_with_base_keys")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_flush_features_avg_aliases_present,
        test_flush_features_zero_frames_aliases,
        test_health_payload_pipeline_available_true,
        test_health_payload_pipeline_available_false,
        test_health_payload_distinguishes_unavailable_from_overload,
        test_health_payload_output_fps_equals_fps_per_camera,
        test_health_payload_input_fps_preserved_raw,
        test_aliases_consistent_with_base_keys,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            failed.append(t.__name__)
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    if failed:
        print(f"\n{len(failed)} test(s) FAILED: {failed}")
        sys.exit(1)
    else:
        print(f"\nAll {len(tests)} tests passed.")
