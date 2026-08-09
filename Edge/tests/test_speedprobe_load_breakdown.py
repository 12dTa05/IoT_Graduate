"""
Edge/tests/test_speedprobe_load_breakdown.py

Focused host tests for the load-score breakdown feature:
  (a) SpeedProbe FPS snapshot includes a load_score_breakdown copy that is
      isolated from subsequent caller mutation
  (b) Breakdown is absent from the snapshot when never set
  (c) run_python._health_push_loop computes both _compute_load_score
      and _compute_load_score_breakdown with matching inputs, and delivers
      the breakdown to the active SpeedProbe via set_load_score_breakdown
  (d) run_python_mode clears ACTIVE_SPEED_PROBE after probe.stop_fps_writer()
      (no stale probe); mode runners register a fresh probe on each start.
  (e) FPS_STATS_FILE has exactly one writer: SpeedProbe._fps_writer_loop

All tests use fake stubs — no GStreamer, no hardware, no network.
Run on host with:
    conda run -n DoAn python3 -m pytest tests/test_speedprobe_load_breakdown.py -q
"""

from __future__ import annotations

import ast
import json as _stdlib_json
import os
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

import numpy as np

EDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDGE))

# ---------------------------------------------------------------------------
# Module-level temp for isolated FPS_STATS_FILE
# ---------------------------------------------------------------------------
_TMP = tempfile.TemporaryDirectory()
_TMP_FPS = Path(_TMP.name) / "fps_stats.json"


# ---------------------------------------------------------------------------
# Host stubs — same foundation as test_offload_counters.py
# ---------------------------------------------------------------------------

def _install_host_stubs():
    """Re-stamp stub modules so collection-order never matters."""
    # dotenv
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = dotenv

    # speedflow_python package (no __init__ import)
    package = sys.modules.get("speedflow_python")
    if package is None:
        package = types.ModuleType("speedflow_python")
        package.__path__ = [str(EDGE / "speedflow_python")]
        sys.modules["speedflow_python"] = package

    # settings
    settings = sys.modules.get("speedflow_python.settings")
    if settings is None:
        settings = types.ModuleType("speedflow_python.settings")
        sys.modules["speedflow_python.settings"] = settings
    for key, value in {
        "ROOT":                  EDGE,
        "NODE_ID":               "host-test",
        "HEALTH_INTERVAL":       1.0,
        "HEALTH_LOG_EVERY":      30,
        "TARGET_FPS":            25.0,
        "FPS_STATS_FILE":        str(_TMP_FPS),
        "MONITOR_URL":           "",
        "ADVERTISE_IP":          "",
        "LOAD_POLICY":           "fps_dominant",
        "LOAD_MODEL":            "",
        "TELEMETRY_INTERVAL":    1.0,
        "JPEG_QUALITY":          85,
        "SNAP_DIR":              str(EDGE / "snapshots"),
        "MAX_SNAPSHOT_PER_ID":   5,
        "MIN_WORLD_DISPL_M":     0.5,
        "MAX_ABS_KMH":           200.0,
        "BBOX_AREA_JUMP":        2.0,
        "MIN_DET_CONF":          0.3,
        "MEDIAN_WINDOW":         5,
        "LICENSE_PLATE_CLASS_IDS": {0},
        "VEHICLE_CLASS_IDS":     {2, 3, 5, 7},
        "SPEED_LOG":             str(EDGE / "logs" / "speed.log"),
        "CAMERAS_YML":           str(EDGE / "configs" / "cameras.yml"),
        "VIDEO_FPS":             30,
    }.items():
        setattr(settings, key, value)

    # zenoh_session
    session = types.ModuleType("speedflow_python.zenoh_session")
    session.make_session = lambda: None
    sys.modules["speedflow_python.zenoh_session"] = session

    # numpy
    import numpy
    sys.modules["numpy"] = numpy

    # cv2 stub
    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.IMREAD_COLOR = 1
    cv2.COLOR_RGBA2BGR = 4
    cv2.COLOR_GRAY2BGR = 8
    cv2.resize = lambda img, size, **kw: img
    cv2.cvtColor = lambda img, code: img
    cv2.imencode = lambda ext, img, params: (True, types.SimpleNamespace(tobytes=lambda: b"fake_jpeg"))
    cv2.imdecode = lambda arr, flag: None
    cv2.imwrite = lambda *a, **kw: True
    sys.modules["cv2"] = cv2

    # msgpack
    sys.modules.pop("msgpack", None)
    import msgpack
    sys.modules["msgpack"] = msgpack

    # queue/threading stdlib
    import queue
    import threading
    sys.modules["queue"] = queue
    sys.modules["threading"] = threading

    # gi / Gst / pyds
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

    # speedflow_c
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

    # draw
    draw = types.ModuleType("speedflow_python.draw")
    draw.add_polygon_display = lambda *a, **k: None
    sys.modules["speedflow_python.draw"] = draw

    # camera_config
    cam_cfg = types.ModuleType("speedflow_python.camera_config")
    class _StubCameraManager:
        def get_config(self, source_id):
            return None
    cam_cfg.CameraManager = _StubCameraManager
    cam_cfg.CameraConfig = object
    sys.modules["speedflow_python.camera_config"] = cam_cfg


def _load(name, relpath):
    """Load speedflow_python modules without __init__.py side effects."""
    _install_host_stubs()

    module_name = name if "." in name else f"speedflow_python.{name}"
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(module_name, EDGE / relpath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {module_name}")
    mod = module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ====================================================================
# Runtime tests — SpeedProbe breakdown snapshot
# ====================================================================

class _StubCamMgrBreakdown:
    """Stub CameraManager returning a valid dummy config."""
    def get_config(self, source_id):
        class _StubCamCfg:
            camera_id = "cam_01"
            roi_polygon = None
            source_points = None
            fps = 30.0
            homo_matrix = np.eye(3, dtype=np.float32)
            speed_limit_kmh = 50
            min_track_age_frames = 15
            source_id = 0
        return _StubCamCfg()


def _make_raw_probe(probe_mod):
    """Return a SpeedProbe with minimal wiring, no offload pub."""
    return probe_mod.SpeedProbe(
        camera_manager=_StubCamMgrBreakdown(),
        cooldown_s=0.5,
    )


def _poll_file(key: str, timeout: float = 6.0):
    """Poll the temp FPS file until key present; return snapshot or None."""
    threshold = time.time() + timeout
    while time.time() < threshold:
        if _TMP_FPS.exists():
            try:
                snap = _stdlib_json.loads(_TMP_FPS.read_text())
                if key in snap:
                    return snap
            except Exception:
                pass
        time.sleep(0.05)
    return None


def test_snapshot_contains_explicit_breakdown_copy():
    """set_load_score_breakdown → FPS snapshot includes a dict() copy.

    After the first flush that picks up the breakdown, mutating the
    caller's original dict must NOT affect the already-written payload.
    """
    probe_mod = _load("probes", "speedflow_python/probes.py")
    # Reset temp file between tests
    if _TMP_FPS.exists():
        _TMP_FPS.unlink()

    probe = _make_raw_probe(probe_mod)
    try:
        bd = {
            "fps_score": 20.0,
            "workload_bonus": 5.0,
            "thermal_bonus": 0.0,
            "composite_score": 25.0,
            "load_score": 25.0,
        }
        probe.set_load_score_breakdown(bd)

        snap = _poll_file("load_score_breakdown", timeout=6.0)
        assert snap is not None, "timed out waiting for snapshot with load_score_breakdown"
        got = snap["load_score_breakdown"]
        assert isinstance(got, dict), f"expected dict, got {type(got)}"
        assert got == bd, f"round-trip mismatch: {got} != {bd}"

        # ── Mutation isolation: the payload holds a copy ──
        bd["load_score"] = 999.0
        bd["fps_score"] = 999.0
        assert snap["load_score_breakdown"]["load_score"] == 25.0, (
            "caller mutation bled into written payload — copy not performed"
        )
        assert snap is not got, "should be a separate dict (JSON-deserialized)"
        assert got["load_score"] == 25.0, (
            "copy in out was mutated after caller change — dict() shallow-copy missing"
        )
    finally:
        probe.stop_fps_writer()


def test_breakdown_absent_when_never_set():
    """Without set_load_score_breakdown, writer never puts the key in FPS payload."""
    probe_mod = _load("probes", "speedflow_python/probes.py")
    if _TMP_FPS.exists():
        _TMP_FPS.unlink()

    probe = _make_raw_probe(probe_mod)
    try:
        # Wait for at least one write with a _telemetry.sequence ≥ 1
        threshold = time.time() + 5.0
        seq = -1
        while time.time() < threshold:
            if _TMP_FPS.exists():
                try:
                    snap = _stdlib_json.loads(_TMP_FPS.read_text())
                    tele = snap.get("_telemetry", {})
                    seq = tele.get("sequence", -1)
                    if seq >= 1:
                        assert "load_score_breakdown" not in snap, (
                            "key leaked without set_load_score_breakdown call"
                        )
                        return
                except Exception:
                    pass
            time.sleep(0.1)
        assert False, f"no sequence ≥ 1 seen (last seq={seq})"
    finally:
        probe.stop_fps_writer()


def test_fps_stats_file_sole_writer():
    """FPS_STATS_FILE written only by SpeedProbe._fps_writer_loop."""
    probe_mod = _load("probes", "speedflow_python/probes.py")

    source = Path(__file__).resolve().parents[1] / "speedflow_python" / "probes.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    # All top-level / class bodies — look for FPS_STATS_FILE writes outside SpeedProbe
    writes_to_fps = 0
    fps_refs_outside_probe = 0
    inside_speedprobe = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SpeedProbe":
            inside_speedprobe = True
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "FPS_STATS_FILE":
                    pass  # inside SpeedProbe
            break

    # All os.replace / open(…FPS_STATS_FILE…) outside SpeedProbe
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if node.name == "SpeedProbe":
                continue
            # Still inside a non-SpeedProbe class
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "FPS_STATS_FILE":
                    fps_refs_outside_probe += 1

    # Walk module-level
    for stmt in tree.body:
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef)):
            continue
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name) and sub.id == "FPS_STATS_FILE":
                fps_refs_outside_probe += 1

    # Also check functions (other than SpeedProbe methods)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "FPS_STATS_FILE":
                    fps_refs_outside_probe += 1

    # CSVLogger has no FPS_STATS_FILE refs.
    # ROIFilterProbe has none.
    assert fps_refs_outside_probe == 0, (
        f"FPS_STATS_FILE referenced {fps_refs_outside_probe} times outside SpeedProbe — "
        "sole-writer contract violated"
    )


# ====================================================================
# AST tests — run_python.py health-loop + probe-lifecycle contract
# ====================================================================

_RUN_PY = EDGE / "speedflow_python" / "run_python.py"
_RUN_PY_TREE = ast.parse(_RUN_PY.read_text(encoding="utf-8"))


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _calls_in(func_node) -> list:
    """Return all ast.Call nodes in the function body, in source order."""
    calls = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            calls.append(node)
    return calls


def test_health_loop_uses_breakdown_function():
    """_health_push_loop calls _compute_load_score_breakdown using the exact
    same argument names as _compute_load_score."""
    func = _find_func(_RUN_PY_TREE, "_health_push_loop")
    assert func is not None, "_health_push_loop function not found"

    # Gather calls to the load-score and breakdown aliases
    call_targets = {}
    for call_node in _calls_in(func):
        name = None
        if isinstance(call_node.func, ast.Name):
            name = call_node.func.id
        elif isinstance(call_node.func, ast.Attribute):
            name = call_node.func.attr
        if name in ("_compute_load_fn", "_compute_lb_fn", "set_load_score_breakdown"):
            call_targets[name] = call_node

    # Must call _compute_lb_fn (the breakdown)
    assert "_compute_lb_fn" in call_targets, (
        "_health_push_loop does not call _compute_load_score_breakdown"
    )
    lb_call = call_targets["_compute_lb_fn"]

    # Verify same argument names as _compute_load_fn
    if "_compute_load_fn" in call_targets:
        load_call = call_targets["_compute_load_fn"]
        load_kw = {kw.arg: True for kw in load_call.keywords}
        lb_kw = {kw.arg: True for kw in lb_call.keywords}
        assert load_kw == lb_kw, (
            f"_compute_load_fn and _compute_lb_fn keyword args differ: "
            f"{load_kw} vs {lb_kw}"
        )

    # Must call set_load_score_breakdown on active probe
    assert "set_load_score_breakdown" in call_targets, (
        "_health_push_loop does not call set_load_score_breakdown on active probe"
    )


def test_health_loop_flat_invalid_breakdown_present():
    """_health_push_loop assigns lb = _UNAVAILABLE_BREAKDOWN in the invalid path."""
    func = _find_func(_RUN_PY_TREE, "_health_push_loop")
    assert func is not None

    # Find body of the else (invalid) branch after snapshot_valid test
    found_invalid = False
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            # Check if test is 'snapshot_valid'
            if (isinstance(node.test, ast.Name)
                    and node.test.id == "snapshot_valid"
                    and node.orelse):
                # Walk else body for "lb = _UNAVAILABLE_BREAKDOWN"
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Assign):
                        for target in sub.targets:
                            if (isinstance(target, ast.Name)
                                    and target.id == "lb"):
                                if (isinstance(sub.value, ast.Name)
                                        and sub.value.id == "_UNAVAILABLE_BREAKDOWN"):
                                    found_invalid = True
                                    break
                break
    assert found_invalid, (
        "else (invalid-snapshot) branch missing 'lb = _UNAVAILABLE_BREAKDOWN'"
    )


def test_health_payload_includes_breakdown():
    """The computed breakdown is retained in the health payload contract."""
    func = _find_func(_RUN_PY_TREE, "_health_push_loop")
    assert func is not None
    assert any(
        isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant)
            and key.value == "load_score_breakdown"
            and isinstance(value, ast.Name)
            and value.id == "lb"
            for key, value in zip(node.keys, node.values)
        )
        for node in ast.walk(func)
    ), "health payload missing load_score_breakdown: lb"


def test_active_probe_lifecycle():
    """run_python_mode clears ACTIVE_SPEED_PROBE after probe.stop_fps_writer().

    Each mode runner (display, file, rtsp_push) registers a fresh probe.
    """
    lifecycle_func = _find_func(_RUN_PY_TREE, "run_python_mode")
    assert lifecycle_func is not None, "run_python_mode not found"

    # Collect (lineno, expr_description) for probe lifecycle calls
    clear_calls = []
    append_calls = []
    stop_calls = []

    for node in ast.walk(lifecycle_func):
        if not isinstance(node, ast.Call):
            continue
        func_expr = node.func
        if isinstance(func_expr, ast.Attribute):
            # probe.stop_fps_writer(), ACTIVE_SPEED_PROBE.clear()
            if func_expr.attr == "clear":
                if (isinstance(func_expr.value, ast.Name)
                        and func_expr.value.id == "ACTIVE_SPEED_PROBE"):
                    clear_calls.append(node.lineno)
            elif func_expr.attr == "stop_fps_writer":
                stop_calls.append(node.lineno)
        # Also check for ACTIVE_SPEED_PROBE.append in mode runners
    # Also check in non-lambda functions
    for func_name in ("run_display_mode", "run_file_mode", "run_rtsp_push_mode"):
        mode_func = _find_func(_RUN_PY_TREE, func_name)
        assert mode_func is not None, f"{func_name} not found"
        found_append = False
        for node in ast.walk(mode_func):
            if not isinstance(node, ast.Call):
                continue
            func_expr = node.func
            if isinstance(func_expr, ast.Attribute) and func_expr.attr == "append":
                if (isinstance(func_expr.value, ast.Name)
                        and func_expr.value.id == "ACTIVE_SPEED_PROBE"):
                    found_append = True
                    break
        assert found_append, (
            f"{func_name} does not append probe to ACTIVE_SPEED_PROBE"
        )

    # In run_python_mode: stop must precede clear
    assert stop_calls, "run_python_mode missing probe.stop_fps_writer()"
    assert clear_calls, "run_python_mode missing ACTIVE_SPEED_PROBE.clear()"
    assert max(stop_calls) < min(clear_calls), (
        "ACTIVE_SPEED_PROBE.clear() must come AFTER probe.stop_fps_writer() "
        f"(stop at {stop_calls}, clear at {clear_calls})"
    )


# ====================================================================
# Runner
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_snapshot_contains_explicit_breakdown_copy,
        test_breakdown_absent_when_never_set,
        test_fps_stats_file_sole_writer,
        test_health_loop_uses_breakdown_function,
        test_health_loop_flat_invalid_breakdown_present,
        test_active_probe_lifecycle,
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
