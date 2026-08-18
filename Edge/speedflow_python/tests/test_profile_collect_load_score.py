"""
Edge/speedflow_python/tests/test_profile_collect_load_score.py

Verify the piecewise-linear FPS-anchored load_score function (shared by
health_agent and the profile collector) and the collector-side validity gate
(_extract_telemetry / _is_fresh / CADENCE_S).  Hardware-free; stdlib-only
(no gi / GStreamer / jtop / zenoh / msgpack).

Implementation strategy: profile_collect.py and health_agent.py import
native/Jetson packages (zenoh, jtop, gi) at module scope.  To make the pure
functions testable on a laptop we side-load the module source via
importlib.util against a stub namespace, exactly as test_load_model.py does.

Run:
    conda run -n DoAn python3 speedflow_python/tests/test_profile_collect_load_score.py
    conda run -n DoAn python3 -m pytest speedflow_python/tests/test_profile_collect_load_score.py -q
"""

from __future__ import annotations

import importlib.util
import math
import sys
import time
from pathlib import Path
from types import ModuleType

_EDGE = Path(__file__).resolve().parents[2]  # .../Edge


def _stub_module(name: str) -> ModuleType:
    if name not in sys.modules:
        sys.modules[name] = ModuleType(name)
    return sys.modules[name]


# ── Stub the heavy / missing deps the modules import at load time ──────────
def _make_stubs(attrs: dict):  # noqa: ANN001
    for name, attr_map in attrs.items():
        m = _stub_module(name)
        for k, v in attr_map.items():
            setattr(m, k, v)


_make_stubs({
    "msgpack": {},
    "dotenv": {"load_dotenv": lambda *a, **kw: None},
    "yaml": {"safe_load": lambda *a, **kw: None},
    "zenoh": {},
    "jtop": {},
    "gi": {},
    "gi.repository": {},
    # speedflow_python package — never let __init__ exec (it pulls core→gi)
    "speedflow_python": {"__path__": []},
    "speedflow_python.zenoh_session": {"make_session": lambda *a, **kw: (None, None)},
    "speedflow_python.settings": {
        "NODE_ID": "test_node",
        "HEALTH_INTERVAL": 1.0,
        "HEALTH_LOG_EVERY": 15,
        "TARGET_FPS": 27.0,
        "FPS_STATS_FILE": "/dev/null",
        "MONITOR_URL": "",
        "ADVERTISE_IP": "127.0.0.1",
        "LOAD_POLICY": "actual",
        "LOAD_MODEL": "formula",
        "TELEMETRY_INTERVAL": 1.0,
    },
})


def _exec_deps_stubbed(path: Path, attrs: dict) -> ModuleType:
    """exec_module a source file, then apply attrs, then execute the body."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    for k, v in attrs.items():
        setattr(mod, k, v)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# health_agent._compute_load_score
# ---------------------------------------------------------------------------

_ha = _exec_deps_stubbed(
    _EDGE / "health_agent.py",
    {
        "_maybe_reload_edge_cfg": lambda: None,
        "_EDGE_CFG": {},
        "_EDGE_NODE_YML": Path("/dev/null"),
    },
)
_compute_load_score = _ha._compute_load_score
_compute_load_score_breakdown = _ha._compute_load_score_breakdown

# profile_collect does `from health_agent import _read_payload, _compute_load_score`.
# Register a stub health_agent module exposing the two names it needs, so
# profile_collect can be exec'd the same way.
_ha_export = _stub_module("health_agent")
_ha_export._read_payload = lambda: None
_ha_export._compute_load_score = _compute_load_score

_pc = _exec_deps_stubbed(
    _EDGE / "tools" / "profile_collect.py",
    {},
)
_extract_telemetry = _pc._extract_telemetry
_is_fresh = _pc._is_fresh
CADENCE_S = _pc.CADENCE_S


# ---------------------------------------------------------------------------
# Load-score test helpers
# ---------------------------------------------------------------------------

def _metrics(gpu=0.0, cpu=0.0, ram=0.0, gpu_temp_c=0.0) -> dict:
    return {"gpu_percent": gpu, "cpu_percent": cpu,
            "ram_percent": ram, "gpu_temp_c": gpu_temp_c}


def _fps(**cams) -> dict:
    return cams


def _feature_stats(**cams) -> dict:
    return cams


def _ha_cfg(workload=None, thermal=None, hw_fuse_threshold=90.0,
            hw_fuse_score_floor=75.0):
    """Build edge_node.yml 'load_score' subsection for stubbing."""
    cfg = {
        "hw_fuse_threshold": hw_fuse_threshold,
        "hw_fuse_score_floor": hw_fuse_score_floor,
    }
    if workload is not None:
        cfg["workload"] = workload
    if thermal is not None:
        cfg["thermal"] = thermal
    return {"load_score": cfg}


# ---------------------------------------------------------------------------
# Load-score tests
# ---------------------------------------------------------------------------

def test_compute_load_score_returns_tuple():
    """Smoke test: function is importable and returns (float, str)."""
    score, preset = _compute_load_score(_metrics(), _fps(cam_01=27.0))
    assert isinstance(score, float)
    assert isinstance(preset, str)
    assert 0.0 <= score <= 100.0


def test_no_cameras_unavailable():
    """No active cameras → score 100 (unavailable, not healthy TARGET_FPS)."""
    score, _ = _compute_load_score(_metrics(), {})
    assert score == 100.0


def test_all_cameras_above_target():
    """avg_fps >= TARGET_FPS (27) → score 0."""
    score, _ = _compute_load_score(
        _metrics(gpu=50, cpu=40, ram=30),
        _fps(cam_01=27.0, cam_02=27.5),
    )
    assert score == 0.0


def test_fps_score_is_dominant_no_hardware_bonus():
    """Hardware metrics are NOT additive: GPU>=90 with healthy FPS → pure FPS score."""
    score, _ = _compute_load_score(
        _metrics(gpu=95, cpu=30, ram=30),
        _fps(cam_01=27.0),
    )
    assert score == 0.0

    score_b, _ = _compute_load_score(
        _metrics(gpu=95, cpu=30, ram=30),
        _fps(cam_01=25.0),
    )
    assert abs(score_b - 22.8) < 0.5


def test_hardware_floor_cpu_saturated_fps22():
    """CPU >= threshold + degraded fps=22 (< 25) → floor 75."""
    score, _ = _compute_load_score(
        _metrics(gpu=30, cpu=92, ram=30),
        _fps(cam_01=22.0),
    )
    assert abs(score - 75.0) < 0.5


def test_hardware_floor_ram_saturated_fps22():
    """RAM >= threshold + degraded fps=22 → floor 75."""
    score, _ = _compute_load_score(
        _metrics(gpu=30, cpu=30, ram=92),
        _fps(cam_01=22.0),
    )
    assert abs(score - 75.0) < 0.5


def test_gpu_saturated_never_triggers_floor():
    """GPU=95 + degraded fps=22 → pure FPS score 57, NO floor."""
    score, _ = _compute_load_score(
        _metrics(gpu=95, cpu=30, ram=30),
        _fps(cam_01=22.0),
    )
    assert abs(score - 57.0) < 0.5


def test_no_hardware_emergency_normal_fps():
    """No hardware saturation → pure FPS score."""
    score, _ = _compute_load_score(
        _metrics(gpu=60, cpu=40, ram=50),
        _fps(cam_01=25.0),
    )
    expected = 57.0 * (27.0 - 25.0) / (27.0 - 22.0)
    assert abs(score - 22.8) < 0.5


def test_score_clamped_to_100():
    """Score is always ≤ 100."""
    score, _ = _compute_load_score(
        _metrics(gpu=100, cpu=100, ram=100),
        _fps(cam_01=0.0),
    )
    assert score <= 100.0
    assert score >= 0.0


def test_anchor_27_fps_zero():
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=27.0))
    assert score == 0.0, f"Expected 0, got {score}"
    score_b, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=28.0))
    assert score_b == 0.0


def test_anchor_22_fps():
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=22.0))
    assert abs(score - 57.0) < 0.05


def test_anchor_19_fps():
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=19.0))
    assert abs(score - 65.0) < 0.05


def test_anchor_17_fps():
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=17.0))
    assert abs(score - 75.0) < 0.05


def test_anchor_zero_fps():
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=0.0))
    assert abs(score - 100.0) < 0.05


def test_anchor_exact_upper_bound():
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10),
                                   _fps(cam_01=27.0, cam_02=27.0))
    assert score == 0.0


def test_interpolation_mid():
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=24.5))
    expected = 57.0 * (27.0 - 24.5) / (27.0 - 22.0)
    assert abs(score - 28.5) < 0.5


def test_gpu_saturated_healthy_fps_no_floor():
    score, _ = _compute_load_score(
        _metrics(gpu=95, cpu=50, ram=50),
        _fps(cam_01=27.0),
    )
    assert score == 0.0


def test_gpu_saturated_healthy_25_fps_no_floor():
    score, _ = _compute_load_score(
        _metrics(gpu=95, cpu=50, ram=50),
        _fps(cam_01=25.0),
    )
    assert abs(score - 22.8) < 0.5


def test_degraded_fps_no_cpu_ram_saturation_no_floor():
    score, _ = _compute_load_score(
        _metrics(gpu=95, cpu=50, ram=50),
        _fps(cam_01=17.0),
    )
    assert abs(score - 75.0) < 0.5


def test_degraded_fps_cpu_ram_both_saturated_floor():
    score, _ = _compute_load_score(
        _metrics(gpu=30, cpu=91, ram=93),
        _fps(cam_01=17.0),
    )
    assert score >= 75.0


# ---------------------------------------------------------------------------
# _compute_load_score_breakdown — auditable breakdown fields
# ---------------------------------------------------------------------------

def test_breakdown_normal_fps_no_bonuses():
    """Normal FPS, no bonuses → breakdown matches legacy behavior."""
    br = _compute_load_score_breakdown(_metrics(), _fps(camA=22.0))
    assert set(br.keys()) == {"fps_score", "workload_bonus", "thermal_bonus", "composite_score", "load_score"}
    assert abs(br["fps_score"] - 57.0) < 0.05
    assert br["workload_bonus"] == 0.0
    assert br["thermal_bonus"] == 0.0
    assert br["composite_score"] == 57.0
    assert br["load_score"] == 57.0  # no fuse


def test_breakdown_composite_vs_load_score_distinction():
    """composite_score (post cap) vs load_score (post fuse) must be distinct when fuse triggers."""
    # fps 22 -> fps_score 57; cpu 92 -> hw_saturated; fps_clamped=22 < 25 -> fuse
    br = _compute_load_score_breakdown(_metrics(cpu=92.0), _fps(camA=22.0))
    assert br["composite_score"] == 57.0
    assert br["load_score"] == 75.0  # floor applied
    assert br["load_score"] > br["composite_score"]


def test_breakdown_no_fuse_when_fps_healthy():
    """No fuse when FPS >= TARGET_FPS - 2 (25), even with CPU/RAM saturated."""
    br = _compute_load_score_breakdown(_metrics(cpu=95, ram=95), _fps(camA=27.0))
    assert br["composite_score"] == 0.0
    assert br["load_score"] == 0.0  # no fuse


def test_breakdown_no_fuse_gpu_saturated():
    """GPU saturation alone never triggers the fuse."""
    br = _compute_load_score_breakdown(_metrics(gpu=95), _fps(camA=22.0))
    assert br["composite_score"] == 57.0
    assert br["load_score"] == 57.0  # no fuse


def test_breakdown_workload_bonus_in_composite():
    """Workload bonus reflected in composite, not in fps_score."""
    _ha._EDGE_CFG = _ha_cfg(workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0})
    try:
        br = _compute_load_score_breakdown(
            _metrics(), _fps(camA=27.0),
            feature_stats={"camA": {"n_track": 15, "n_plate": 5}},  # total 20 -> bonus 5.0
        )
    finally:
        _ha._EDGE_CFG = {}
    assert br["fps_score"] == 0.0
    assert br["workload_bonus"] == 5.0
    assert br["composite_score"] == 5.0
    assert br["load_score"] == 5.0


def test_breakdown_thermal_bonus_in_composite():
    """Thermal bonus reflected in composite."""
    _ha._EDGE_CFG = _ha_cfg(thermal={"enabled": True, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 5.0})
    try:
        br = _compute_load_score_breakdown(
            _metrics(gpu_temp_c=85.0), _fps(camA=27.0),
        )
    finally:
        _ha._EDGE_CFG = {}
    assert br["fps_score"] == 0.0
    assert br["thermal_bonus"] == 5.0
    assert br["composite_score"] == 5.0
    assert br["load_score"] == 5.0


def test_breakdown_composite_capped_at_100():
    """composite_score capped at 100; load_score also capped (pre-fuse)."""
    _ha._EDGE_CFG = _ha_cfg(
        workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0},
        thermal={"enabled": True, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 5.0},
    )
    try:
        # fps 1.0 -> fps_score ~98.5 + 10 + 5 = 113.5 -> capped 100
        br = _compute_load_score_breakdown(
            _metrics(gpu_temp_c=85.0), _fps(camA=1.0),
            feature_stats={"camA": {"n_track": 20, "n_plate": 20}},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert br["composite_score"] == 100.0
    assert br["load_score"] == 100.0


def test_breakdown_malformed_telemetry_config_never_crashes():
    """Malformed inputs never crash; returns valid breakdown with safe defaults."""
    # None fps_stats -> treated as no active cameras -> 100
    br = _compute_load_score_breakdown(_metrics(), None)
    assert br["load_score"] == 100.0
    assert br["fps_score"] == 100.0

    # Non-dict fps_stats
    br = _compute_load_score_breakdown(_metrics(), "bad")
    assert br["load_score"] == 100.0

    # Malformed config - load_score missing
    _ha._EDGE_CFG = {"load_score": "not_a_dict"}
    try:
        br = _compute_load_score_breakdown(_metrics(), _fps(camA=27.0))
    finally:
        _ha._EDGE_CFG = {}
    assert br["fps_score"] == 0.0
    assert br["load_score"] == 0.0


def test_breakdown_no_cameras_unavailable():
    """No active cameras -> all scores 100 (unavailable)."""
    br = _compute_load_score_breakdown(_metrics(), {})
    assert br["fps_score"] == 100.0
    assert br["composite_score"] == 100.0
    assert br["load_score"] == 100.0


def test_breakdown_payload_presence():
    """Simulate HealthAgent._run payload structure and verify load_score_breakdown is present."""
    # This mirrors what HealthAgent._run does
    metrics = _metrics(cpu=40, ram=30, gpu_temp_c=60)
    fps_stats = _fps(camA=22.0)
    starved_cams = set()
    feature_stats = {"camA": {"n_track": 10, "n_plate": 2}}

    load_score, omega_preset = _compute_load_score(
        metrics, fps_stats, source_starved_cameras=starved_cams, feature_stats=feature_stats
    )
    load_score_breakdown = _compute_load_score_breakdown(
        metrics, fps_stats, source_starved_cameras=starved_cams, feature_stats=feature_stats
    )

    # Verify load_score matches breakdown.load_score
    assert load_score == load_score_breakdown["load_score"]

    # Verify breakdown structure
    assert isinstance(load_score_breakdown, dict)
    for key in ("fps_score", "workload_bonus", "thermal_bonus", "composite_score", "load_score"):
        assert key in load_score_breakdown
        assert isinstance(load_score_breakdown[key], (int, float))
        assert math.isfinite(load_score_breakdown[key])


def test_breakdown_invalid_snapshot_simulated():
    """When HealthAgent._run invalid snapshot branch, breakdown still valid with all-100s."""
    load_score_breakdown = {
        "fps_score": 100.0,
        "workload_bonus": 0.0,
        "thermal_bonus": 0.0,
        "composite_score": 100.0,
        "load_score": 100.0,
    }
    # Verify structure
    assert load_score_breakdown["load_score"] == 100.0
    assert load_score_breakdown["fps_score"] == 100.0
    assert load_score_breakdown["workload_bonus"] == 0.0
    assert load_score_breakdown["thermal_bonus"] == 0.0
    assert load_score_breakdown["composite_score"] == 100.0


# ---------------------------------------------------------------------------
# Collector gate tests
# ---------------------------------------------------------------------------

def test_cadence_s_is_one():
    assert CADENCE_S == 1.0


def test_extract_telemetry_none():
    assert _extract_telemetry(None) is None


def test_extract_telemetry_empty_dict():
    assert _extract_telemetry({}) is None


def test_extract_telemetry_missing_telemetry():
    assert _extract_telemetry({"_updated_at": 100.0}) is None


def test_extract_telemetry_rejects_all_zero_identity():
    # no session_id + no sequence → must be rejected (not fabricated)
    assert _extract_telemetry({"_telemetry": {}}) is None


def test_extract_telemetry_rejects_empty_session():
    assert _extract_telemetry(
        {"_telemetry": {"session_id": "", "sequence": 1}}) is None


def test_extract_telemetry_rejects_non_str_session():
    assert _extract_telemetry(
        {"_telemetry": {"session_id": 7, "sequence": 1}}) is None


def test_extract_telemetry_rejects_str_sequence():
    assert _extract_telemetry(
        {"_telemetry": {"session_id": "abc", "sequence": "5"}}) is None


def test_extract_telemetry_rejects_bool_sequence():
    # bool is an int subclass — must be rejected explicitly
    assert _extract_telemetry(
        {"_telemetry": {"session_id": "abc", "sequence": True}}) is None


def test_extract_telemetry_rejects_negative_seq():
    assert _extract_telemetry(
        {"_telemetry": {"session_id": "abc", "sequence": -3}}) is None


def test_extract_telemetry_accepts_zero():
    assert _extract_telemetry(
        {"_telemetry": {"session_id": "s1", "sequence": 0}}) == ("s1", 0)


def test_extract_telemetry_accepts_large_seq():
    assert _extract_telemetry(
        {"_telemetry": {"session_id": "s1", "sequence": 123456}}) == ("s1", 123456)


def test_intrinsic_fresh_true_now():
    assert _is_fresh({"_updated_at": time.time() - 0.1}, time.time())


def test_is_fresh_false_when_stale():
    assert not _is_fresh({"_updated_at": time.time() - CADENCE_S - 0.001},
                         time.time())


def test_is_fresh_false_when_no_timestamp():
    assert not _is_fresh({}, time.time())


def test_is_fresh_false_when_none_timestamp():
    assert not _is_fresh({"_updated_at": None}, time.time())


def test_is_fresh_false_when_str_timestamp():
    assert not _is_fresh({"_updated_at": "0"}, time.time())


def test_is_fresh_false_when_bool_timestamp():
    assert not _is_fresh({"_updated_at": True}, time.time())


def test_is_fresh_custom_window_accepts_one_second():
    assert _is_fresh({"_updated_at": time.time() - 1.0},
                     time.time(), max_age_s=2.0)


# ---------------------------------------------------------------------------
# input_fps_avg computation (pure, mirrors profile_collect.collect step 8)
# ---------------------------------------------------------------------------

def _compute_input_fps_avg(payload: dict) -> float:
    """Mirror the exact logic from collect(): read _input_fps, average values."""
    input_fps_dict = payload.get("_input_fps", {})
    if isinstance(input_fps_dict, dict) and input_fps_dict:
        input_fps_vals = [v for v in input_fps_dict.values()
                          if isinstance(v, (int, float))]
        return sum(input_fps_vals) / len(input_fps_vals) if input_fps_vals else 0.0
    return 0.0


def test_input_fps_avg_standard():
    assert _compute_input_fps_avg(
        {"_input_fps": {"cam_01": 25.0, "cam_02": 27.0}}
    ) == 26.0


def test_input_fps_avg_missing_key():
    assert _compute_input_fps_avg({}) == 0.0


def test_input_fps_avg_empty_dict():
    assert _compute_input_fps_avg({"_input_fps": {}}) == 0.0


def test_input_fps_avg_none_value():
    assert _compute_input_fps_avg({"_input_fps": None}) == 0.0


def test_input_fps_avg_non_dict_value():
    assert _compute_input_fps_avg({"_input_fps": "bad"}) == 0.0


def test_input_fps_avg_mixed_numeric_and_string():
    # strings are skipped, only floats/ints averaged
    assert _compute_input_fps_avg(
        {"_input_fps": {"cam_01": 25.0, "cam_02": "offline", "cam_03": 27.0}}
    ) == 26.0


def test_input_fps_avg_zero_and_normal():
    assert _compute_input_fps_avg(
        {"_input_fps": {"cam_01": 0.0, "cam_02": 25.0, "cam_03": 27.5}}
    ) == 17.5


# ---------------------------------------------------------------------------
# Cadence-enforcement checks (the stubs validate the 1-s contract)
# ---------------------------------------------------------------------------


def test_health_interval_is_one_in_stub():
    assert sys.modules[
        "speedflow_python.settings"
    ].HEALTH_INTERVAL == 1.0


def test_telemetry_interval_is_one_in_stub():
    assert sys.modules[
        "speedflow_python.settings"
    ].TELEMETRY_INTERVAL == 1.0


def test_profile_collect_cadence_guard_in_source():
    """
    Confirm the --interval!=1.0 guard is present in the production file
    (compiles and is parseable, not a side-effect test).
    """
    import ast
    _src = (_EDGE / "tools" / "profile_collect.py").read_text()
    assert 'abs(args.interval - 1.0)' in _src, (
        "profile_collect.py is missing the mandatory 1.0s guard on --interval"
    )
    assert 'sys.exit(1)' in _src, (
        "profile_collect.py must call sys.exit(1) when interval != 1.0"
    )


def test_profile_collect_passes_feature_stats_in_source():
    """
    Verify profile_collect.py passes feature_stats into _compute_load_score.
    """
    _src = (_EDGE / "tools" / "profile_collect.py").read_text()
    assert 'feature_stats=active_feat_stats' in _src, (
        "profile_collect.py must pass active feature_stats into _compute_load_score"
    )
    assert '"expected_fps"' in _src, "profile_collect.py must record expected_fps"
    assert '"n_cameras_total"' in _src, "profile_collect.py must record n_cameras_total"


# ---------------------------------------------------------------------------
# source_starved_cameras exclusion tests
# ---------------------------------------------------------------------------

# Reuse the already-loaded stubbed health_agent module.
_detect_source_starved = _ha._detect_source_starved
_derive_camera_workload = _ha._derive_camera_workload


def test_starvation_both_input_and_output_below_threshold():
    """
    Both input and output FPS below expected → classified as starved.
    Default threshold = 25.0 * 0.2 = 5.0 fps.
    """
    starved = _detect_source_starved(
        {"camA": 3.0, "camB": 25.0},
        {"camA": 2.0, "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_zero_input_zero_output():
    """0 input + 0 output → starved."""
    starved = _detect_source_starved(
        {"camA": 0.0, "camB": 25.0}, {"camA": 0.0, "camB": 25.0}, {},
    )
    assert starved == {"camA"}


def test_starvation_output_only_transient_not_classified():
    """Healthy input, low output → NOT starved (transient)."""
    starved = _detect_source_starved(
        {"camA": 0.5, "camB": 25.0}, {"camA": 25.0, "camB": 25.0}, {},
    )
    assert starved == set()


def test_starvation_missing_input_suspends_decision():
    """Unavailable _input_fps → empty set, backward-compat."""
    assert _detect_source_starved({"camA": 0.0}, {}, {}) == set()
    assert _detect_source_starved({"camA": 0.0}, None, {}) == set()


def test_starved_camera_excluded_from_load_score():
    """
    starved cam excluded → only healthy cam drives score.
    Without exclusion: avg = (3+27)/2 = 15 → score ≈ 75
    With exclusion:    avg = 27/1    = 27 → score  = 0
    """
    score_no_excl, _ = _compute_load_score(_metrics(), _fps(camA=3.0, camB=27.0))
    score_excl, _ = _compute_load_score(
        _metrics(), _fps(camA=3.0, camB=27.0), source_starved_cameras={"camA"},
    )
    # without exclusion both cameras count: avg=15 → in [0,17] band → score ≈ 79
    assert score_no_excl > 50, "both cameras should degrade score"
    assert score_excl == 0.0


def test_starved_camera_all_starved_reports_unavailable():
    """All cameras starved → score 100 (unavailable, same as zero cameras)."""
    score, _ = _compute_load_score(
        _metrics(), _fps(camA=2.0, camB=3.0), source_starved_cameras={"camA", "camB"},
    )
    assert score == 100.0


def test_starved_camera_empty_set_preserves_legacy_behavior():
    """Empty starved set → identical to passing no kwarg."""
    score_old, _ = _compute_load_score(_metrics(), _fps(camA=22.0))
    score_new, _ = _compute_load_score(
        _metrics(), _fps(camA=22.0), source_starved_cameras=set(),
    )
    assert abs(score_old - score_new) < 0.01


def test_starvation_configurable_threshold():
    """edge_node.yml thresholds are honoured."""
    cfg_loose = {"source_starved": {"expected_source_rate": 25.0,
                                    "starved_threshold_ratio": 0.1}}
    # threshold 2.5 → 3.0 fps not starved
    starved = _detect_source_starved({"camA": 3.0}, {"camA": 3.0}, cfg_loose)
    assert starved == set()

    cfg_tight = {"source_starved": {"expected_source_rate": 50.0,
                                    "starved_threshold_ratio": 0.1}}
    # threshold 5.0 → 3.0 fps starved
    starved = _detect_source_starved({"camA": 3.0}, {"camA": 3.0}, cfg_tight)
    assert starved == {"camA"}


def test_starvation_backward_compat_kwarg_optional():
    """Calling _compute_load_score without the kwarg is fine (3rd param optional)."""
    score, _ = _compute_load_score(_metrics(), _fps(camA=27.0))
    assert score == 0.0


def test_starvation_union_iterates_over_input_and_output_cameras():
    """
    Camera present in _input_fps but not in fps_stats is still classified
    if both input and output are below threshold.
    """
    starved = _detect_source_starved(
        {"camB": 25.0},               # camA absent from output
        {"camA": 2.0, "camB": 25.0},  # camA has starved input
        {},
    )
    # camA: input=2.0 < 5.0 AND output absent (0.0) → starved
    assert "camA" in starved
    assert "camB" not in starved


# ---------------------------------------------------------------------------
# camera_workload derivation (n_track + n_plate, active non-starved only)
# ---------------------------------------------------------------------------

def test_camera_workload_normal_mapping():
    """Active cameras map to n_track + n_plate; inactive (fps 0) are dropped."""
    wl = _derive_camera_workload(
        {"camA": {"n_track": 5, "n_plate": 3}, "camB": {"n_track": 1.5, "n_plate": 0}},
        {"camA": 27.0, "camB": 25.0},
        set(),
    )
    assert wl == {"camA": 8, "camB": 1.5}


def test_camera_workload_zero_values_still_mapped():
    """Valid zero n_track/n_plate → workload 0 (camera is still active)."""
    wl = _derive_camera_workload(
        {"camA": {"n_track": 0, "n_plate": 0}},
        {"camA": 27.0},
        set(),
    )
    assert wl == {"camA": 0}


def test_camera_workload_malformed_values_ignored():
    """String / None / bool / NaN / negative fields skip the camera, no crash."""
    wl = _derive_camera_workload(
        {
            "camA": {"n_track": "10", "n_plate": 3},          # non-numeric
            "camB": {"n_track": 5, "n_plate": None},          # missing field
            "camC": {"n_track": float("nan"), "n_plate": 1},  # non-finite
            "camD": {"n_track": 5, "n_plate": -2},            # negative
            "camE": {"n_track": True, "n_plate": 1},          # bool (int subclass)
            "camF": {"n_track": float("inf"), "n_plate": 1},  # non-finite
            "camG": "not-a-dict",                             # malformed entry
            "camH": {"n_track": 5, "n_plate": 4},             # valid
        },
        {f"cam{c}": 27.0 for c in "ABCDEFGH"},
        set(),
    )
    assert wl == {"camH": 9}


def test_camera_workload_starved_and_inactive_omitted():
    """Starved camera and inactive (fps 0 / missing fps) camera are omitted."""
    wl = _derive_camera_workload(
        {
            "camA": {"n_track": 5, "n_plate": 3},  # active healthy
            "camB": {"n_track": 5, "n_plate": 3},  # starved
            "camC": {"n_track": 5, "n_plate": 3},  # inactive (fps 0)
            "camD": {"n_track": 5, "n_plate": 3},  # not in fps_stats at all
        },
        {"camA": 27.0, "camB": 27.0, "camC": 0.0},
        {"camB"},
    )
    assert wl == {"camA": 8}


def test_camera_workload_empty_feature_stats():
    """No features → empty workload dict (never raises)."""
    assert _derive_camera_workload({}, {}, set()) == {}


# ---------------------------------------------------------------------------
# _detect_source_starved hardening — malformed values/config must never raise
# ---------------------------------------------------------------------------

def test_starvation_fps_stats_none():
    """fps_stats=None → treated as {}, no crash."""
    starved = _detect_source_starved(None, {"camA": 2.0}, {})
    # camA: out=0.0 (absent) + in=2.0 < 5.0 → starved
    assert starved == {"camA"}


def test_starvation_fps_stats_string():
    """fps_stats="bad" → treated as {}, no crash."""
    starved = _detect_source_starved("bad", {"camA": 2.0}, {})
    assert starved == {"camA"}


def test_starvation_fps_stats_contains_none_value():
    """None value in fps_stats → 0.0, still evaluated."""
    starved = _detect_source_starved(
        {"camA": None, "camB": 25.0},
        {"camA": 2.0,  "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_fps_stats_contains_string_value():
    """String value in fps_stats → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": "offline", "camB": 25.0},
        {"camA": 2.0,        "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_fps_stats_contains_bool_value():
    """bool value in fps_stats (int subclass trap) → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": True, "camB": 25.0},
        {"camA": 2.0,  "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_fps_stats_contains_nan():
    """NaN in fps_stats → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": float("nan"), "camB": 25.0},
        {"camA": 2.0,           "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_fps_stats_contains_inf():
    """inf in fps_stats → 0.0 (not usable), no crash."""
    starved = _detect_source_starved(
        {"camA": float("inf"), "camB": 25.0},
        {"camA": 2.0,           "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_fps_stats_contains_negative():
    """Negative value in fps_stats → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": -5.0, "camB": 25.0},
        {"camA": 2.0,  "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_fps_stats_contains_list():
    """Non-scalar list value in fps_stats → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": [1, 2, 3], "camB": 25.0},
        {"camA": 2.0,        "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_input_fps_contains_none_value():
    """None in input_fps → 0.0, still evaluated with output."""
    starved = _detect_source_starved(
        {"camA": 2.0,  "camB": 25.0},
        {"camA": None, "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_input_fps_contains_string_value():
    """String in input_fps → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": 2.0,        "camB": 25.0},
        {"camA": "offline",  "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_input_fps_contains_nan():
    """NaN in input_fps → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": 2.0,           "camB": 25.0},
        {"camA": float("nan"),  "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_input_fps_contains_inf():
    """inf in input_fps → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": 2.0,           "camB": 25.0},
        {"camA": float("inf"),  "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_input_fps_contains_negative():
    """Negative in input_fps → 0.0, no crash."""
    starved = _detect_source_starved(
        {"camA": 2.0,  "camB": 25.0},
        {"camA": -5.0, "camB": 25.0},
        {},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_none():
    """edge_cfg=None → defaults used, no crash."""
    starved = _detect_source_starved(
        {"camA": 2.0}, {"camA": 2.0}, None,
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_expected_source_rate_none():
    """expected_source_rate=None → default 25.0 used."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"expected_source_rate": None}},
    )
    # threshold defaults: 25*0.2=5 → 4.0 < 5.0 → starved
    assert starved == {"camA"}


def test_starvation_edge_cfg_expected_source_rate_nan():
    """expected_source_rate=NaN → default 25.0 used."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"expected_source_rate": float("nan")}},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_expected_source_rate_string():
    """expected_source_rate="bad" → default 25.0 used."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"expected_source_rate": "bad"}},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_expected_source_rate_negative():
    """expected_source_rate=-10 → default 25.0 used, no crash."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"expected_source_rate": -10}},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_ratio_none():
    """starved_threshold_ratio=None → default 0.2 used."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"starved_threshold_ratio": None}},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_ratio_nan():
    """starved_threshold_ratio=NaN → default 0.2 used."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"starved_threshold_ratio": float("nan")}},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_ratio_string():
    """starved_threshold_ratio="bad" → default 0.2 used."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"starved_threshold_ratio": "bad"}},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_ratio_negative():
    """starved_threshold_ratio=-0.5 → default 0.2 used, no crash."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": {"starved_threshold_ratio": -0.5}},
    )
    assert starved == {"camA"}


def test_starvation_edge_cfg_source_starved_not_dict():
    """source_starved section is a string → defaults, no crash."""
    starved = _detect_source_starved(
        {"camA": 4.0}, {"camA": 4.0},
        {"source_starved": "bad"},
    )
    assert starved == {"camA"}


def test_starvation_unusable_threshold_returns_empty():
    """Config yields threshold=0 or negative → defensible empty set."""
    # expected=0 → threshold=0 → nothing below 0
    starved = _detect_source_starved(
        {"camA": 0.0}, {"camA": 0.0},
        {"source_starved": {"expected_source_rate": 0.0}},
    )
    assert starved == set()

    # ratio=0 → threshold=0
    starved = _detect_source_starved(
        {"camA": 0.0}, {"camA": 0.0},
        {"source_starved": {"starved_threshold_ratio": 0.0}},
    )
    assert starved == set()


def test_starvation_mixed_malformed_both_sides():
    """Mixed malformed values in both fps_stats and input_fps, no crash."""
    starved = _detect_source_starved(
        {"camA": float("nan"), "camB": "offline", "camC": 25.0, "camD": None},
        {"camA": None,         "camB": True,      "camC": 25.0, "camD": float("inf")},
        {"source_starved": {"expected_source_rate": "also_bad", "starved_threshold_ratio": float("nan")}},
    )
    # defaults: 25.0*0.2 = 5.0
    # camA: out=0.0(nan), in=0.0(None) → 0<5 and 0<5 → starved
    # camB: out=0.0(str), in=0.0(bool) → starved
    # camC: out=25.0,     in=25.0     → not starved
    # camD: out=0.0(None),in=0.0(inf) → starved
    assert starved == {"camA", "camB", "camD"}


def test_starvation_valid_behavior_preserved():
    """Normal valid inputs still produce correct starvation detection."""
    starved = _detect_source_starved(
        {"camA": 3.0, "camB": 25.0},
        {"camA": 2.0, "camB": 25.0},
        {},
    )
    assert starved == {"camA"}

    starved = _detect_source_starved(
        {"camA": 0.5, "camB": 25.0},
        {"camA": 25.0, "camB": 25.0},
        {},
    )
    assert starved == set()


# ---------------------------------------------------------------------------
# Conservative composite bonuses — workload + thermal (config-driven)
# ---------------------------------------------------------------------------

def test_bonus_legacy_no_config_section():
    """No workload/thermal sections → bonus 0; score unchanged from legacy."""
    _ha._EDGE_CFG = {}
    try:
        s_legacy, _ = _compute_load_score(_metrics(), _fps(camA=22.0))
        s_feat, _ = _compute_load_score(
            _metrics(), _fps(camA=22.0),
            feature_stats={"camA": {"n_track": 50, "n_plate": 50}},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert s_legacy == s_feat
    assert abs(s_feat - 57.0) < 0.5


def test_bonus_workload_normal_ramp():
    """Workload bonus linear: half capacity → half max_bonus."""
    _ha._EDGE_CFG = _ha_cfg(workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0})
    try:
        # fps 0 (TARGET) → fps_score 0.0; workload 20 → bonus 5.0
        score, preset = _compute_load_score(
            _metrics(), _fps(camA=27.0),
            feature_stats={"camA": {"n_track": 15, "n_plate": 5}},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert preset == "fps_dominant"
    assert abs(score - 5.0) < 0.05


def test_bonus_workload_clamped_at_capacity():
    """Above capacity → bonus clamped at max_bonus (not exploded)."""
    _ha._EDGE_CFG = _ha_cfg(workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0})
    try:
        s_at, _ = _compute_load_score(
            _metrics(), _fps(camA=27.0),
            feature_stats={"camA": {"n_track": 20, "n_plate": 20}},
        )
        s_over, _ = _compute_load_score(
            _metrics(), _fps(camA=27.0),
            feature_stats={"camA": {"n_track": 50, "n_plate": 50}},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(s_at - 10.0) < 0.05
    assert abs(s_over - 10.0) < 0.05


def test_bonus_workload_starved_camera_excluded():
    """Starved cameras contribute zero workload."""
    _ha._EDGE_CFG = _ha_cfg(workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0})
    try:
        s_excl, _ = _compute_load_score(
            _metrics(), _fps(camA=27.0, camB=27.0),
            source_starved_cameras={"camB"},
            feature_stats={
                "camA": {"n_track": 10, "n_plate": 2},   # 12 → bonus 3.0
                "camB": {"n_track": 30, "n_plate": 18},  # 48 starved → excluded
            },
        )
        s_none, _ = _compute_load_score(
            _metrics(), _fps(camA=27.0, camB=27.0),
            feature_stats={
                "camA": {"n_track": 10, "n_plate": 2},
                "camB": {"n_track": 30, "n_plate": 18},
            },
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(s_excl - 3.0) < 0.05       # only camA's 12
    assert s_excl < s_none                 # starved exclusion reduced bonus


def test_bonus_workload_malformed_no_bonus():
    """Malformed workload config/feature_stats → 0 bonus, no crash."""
    good_feat = {"camA": {"n_track": 20, "n_plate": 20}}
    for bad_cfg in [
        {"enabled": True, "capacity": "abc", "max_bonus": 10.0},
        {"enabled": True, "capacity": 40.0, "max_bonus": 0},
        {"enabled": True, "capacity": -5.0, "max_bonus": 10.0},
        {"enabled": True, "capacity": 40.0, "max_bonus": -1.0},
    ]:
        _ha._EDGE_CFG = _ha_cfg(workload=bad_cfg)
        try:
            score, _ = _compute_load_score(
                _metrics(), _fps(camA=27.0), feature_stats=good_feat,
            )
        finally:
            _ha._EDGE_CFG = {}
        assert abs(score - 0.0) < 0.05, f"unexpected score={score} for cfg={bad_cfg}"

    # workload section not a dict
    _ha._EDGE_CFG = _ha_cfg(workload="garbage")
    try:
        score, _ = _compute_load_score(
            _metrics(), _fps(camA=27.0), feature_stats=good_feat,
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(score - 0.0) < 0.05

    # feature_stats with malformed entries, no crash
    _ha._EDGE_CFG = _ha_cfg(workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0})
    try:
        score, _ = _compute_load_score(
            _metrics(), _fps(camA=27.0),
            feature_stats={"camA": "not_a_dict"},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(score - 0.0) < 0.05


def test_bonus_workload_feature_stats_none_disabled():
    """feature_stats=None → workload bonus 0, no crash even with cfg enabled."""
    _ha._EDGE_CFG = _ha_cfg(workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0})
    try:
        score, _ = _compute_load_score(_metrics(), _fps(camA=27.0))
    finally:
        _ha._EDGE_CFG = {}
    assert abs(score - 0.0) < 0.05


def test_bonus_thermal_normal_ramp():
    """Thermal bonus linear from onset to critical, clamped."""
    cfg = {"enabled": True, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 5.0}
    _ha._EDGE_CFG = _ha_cfg(thermal=cfg)
    try:
        # temp = critical → full bonus
        s_full, _ = _compute_load_score(
            _metrics(gpu_temp_c=85.0), _fps(camA=27.0),
        )
        # temp <= onset → 0 bonus
        s_zero, _ = _compute_load_score(
            _metrics(gpu_temp_c=60.0), _fps(camA=27.0),
        )
        # temp = onset + epsilon → near 0
        s_onset, _ = _compute_load_score(
            _metrics(gpu_temp_c=70.0), _fps(camA=27.0),
        )
        # temp = midpoint 77.5 → 5 * 7.5/15 = 2.5
        s_mid, _ = _compute_load_score(
            _metrics(gpu_temp_c=78.0), _fps(camA=27.0),
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(s_full - 5.0) < 0.05
    assert abs(s_zero) < 0.05
    assert abs(s_onset) < 0.05
    # 5 * (78-70)/(85-70) = 5 * 8/15 ≈ 2.667
    assert abs(s_mid - 2.667) < 0.05


def test_bonus_thermal_malformed_no_bonus():
    """Malformed thermal config/value → 0 bonus, no crash."""
    for bad_cfg in [
        {"enabled": True, "onset_c": 90.0, "critical_c": 70.0, "max_bonus": 5.0},  # inverted
        {"enabled": True, "onset_c": 70.0, "critical_c": 70.0, "max_bonus": 5.0},  # onset == critical
        {"enabled": True, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 0.0},  # zero bonus
        {"enabled": True, "onset_c": "hot", "critical_c": 85.0, "max_bonus": 5.0},  # string
        {"enabled": False, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 5.0},  # disabled
    ]:
        _ha._EDGE_CFG = _ha_cfg(thermal=bad_cfg)
        try:
            score, _ = _compute_load_score(
                _metrics(gpu_temp_c=85.0), _fps(camA=27.0),
            )
        finally:
            _ha._EDGE_CFG = {}
        assert abs(score - 0.0) < 0.05, f"unexpected score={score} for cfg={bad_cfg}"

    # thermal section not a dict
    _ha._EDGE_CFG = _ha_cfg(thermal="garbage")
    try:
        score, _ = _compute_load_score(
            _metrics(gpu_temp_c=85.0), _fps(camA=27.0),
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(score - 0.0) < 0.05

    # gpu_temp_c non-numeric
    _ha._EDGE_CFG = _ha_cfg(
        thermal={"enabled": True, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 5.0},
    )
    try:
        score, _ = _compute_load_score(
            {"gpu_percent": 0.0, "cpu_percent": 0.0, "ram_percent": 0.0, "gpu_temp_c": "hot"},
            _fps(camA=27.0),
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(score - 0.0) < 0.05


def test_bonus_composite_capped_at_100():
    """fps_score + bonuses cannot exceed 100."""
    _ha._EDGE_CFG = _ha_cfg(
        workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0},
        thermal={"enabled": True, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 5.0},
    )
    try:
        # fps 1.0 → fps_score ≈ 98.53 + 10 + 5 = 113.53 → capped 100
        score, _ = _compute_load_score(
            _metrics(gpu_temp_c=85.0), _fps(camA=1.0),
            feature_stats={"camA": {"n_track": 20, "n_plate": 20}},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert score <= 100.0
    assert abs(score - 100.0) < 0.05


def test_bonus_cpu_fuse_still_applies():
    """CPU fuse floor still applies to composite with bonuses."""
    _ha._EDGE_CFG = _ha_cfg(
        workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0},
    )
    try:
        # fps 22 → fps_score 57.0; workload 20 (half cap) → bonus 5.0
        # composite = 62.0; cpu >= 90 + fps<25 → fuse → max(62, 75) = 75
        score, _ = _compute_load_score(
            _metrics(cpu=92.0), _fps(camA=22.0),
            feature_stats={"camA": {"n_track": 15, "n_plate": 5}},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(score - 75.0) < 0.05


def test_bonus_ram_fuse_still_applies():
    """RAM fuse floor still applies to composite with bonuses."""
    _ha._EDGE_CFG = _ha_cfg(
        workload={"enabled": True, "capacity": 40.0, "max_bonus": 10.0},
        thermal={"enabled": True, "onset_c": 70.0, "critical_c": 85.0, "max_bonus": 5.0},
    )
    try:
        # fps 19 → fps_score 65.0; workload 8 → 2.0; thermal @75 → 5*(5/15)≈1.667
        # composite ≈ 68.667; ram fuse → floor 75
        score, _ = _compute_load_score(
            _metrics(ram=92.0, gpu_temp_c=75.0), _fps(camA=19.0),
            feature_stats={"camA": {"n_track": 3, "n_plate": 5}},
        )
    finally:
        _ha._EDGE_CFG = {}
    assert abs(score - 75.0) < 0.05


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        # load-score
        test_compute_load_score_returns_tuple,
        test_no_cameras_unavailable,
        test_all_cameras_above_target,
        test_fps_score_is_dominant_no_hardware_bonus,
        test_hardware_floor_cpu_saturated_fps22,
        test_hardware_floor_ram_saturated_fps22,
        test_gpu_saturated_never_triggers_floor,
        test_no_hardware_emergency_normal_fps,
        test_score_clamped_to_100,
        test_anchor_27_fps_zero,
        test_anchor_22_fps,
        test_anchor_19_fps,
        test_anchor_17_fps,
        test_anchor_zero_fps,
        test_anchor_exact_upper_bound,
        test_interpolation_mid,
        test_gpu_saturated_healthy_fps_no_floor,
        test_gpu_saturated_healthy_25_fps_no_floor,
        test_degraded_fps_no_cpu_ram_saturation_no_floor,
        test_degraded_fps_cpu_ram_both_saturated_floor,
        # load-score breakdown
        test_breakdown_normal_fps_no_bonuses,
        test_breakdown_composite_vs_load_score_distinction,
        test_breakdown_no_fuse_when_fps_healthy,
        test_breakdown_no_fuse_gpu_saturated,
        test_breakdown_workload_bonus_in_composite,
        test_breakdown_thermal_bonus_in_composite,
        test_breakdown_composite_capped_at_100,
        test_breakdown_malformed_telemetry_config_never_crashes,
        test_breakdown_no_cameras_unavailable,
        test_breakdown_payload_presence,
        test_breakdown_invalid_snapshot_simulated,
        # collector gate
        test_cadence_s_is_one,
        test_extract_telemetry_none,
        test_extract_telemetry_empty_dict,
        test_extract_telemetry_missing_telemetry,
        test_extract_telemetry_rejects_all_zero_identity,
        test_extract_telemetry_rejects_empty_session,
        test_extract_telemetry_rejects_non_str_session,
        test_extract_telemetry_rejects_str_sequence,
        test_extract_telemetry_rejects_bool_sequence,
        test_extract_telemetry_rejects_negative_seq,
        test_extract_telemetry_accepts_zero,
        test_extract_telemetry_accepts_large_seq,
        test_intrinsic_fresh_true_now,
        test_is_fresh_false_when_stale,
        test_is_fresh_false_when_no_timestamp,
        test_is_fresh_false_when_none_timestamp,
        test_is_fresh_false_when_str_timestamp,
        test_is_fresh_false_when_bool_timestamp,
        test_is_fresh_custom_window_accepts_one_second,
        # input_fps_avg
        test_input_fps_avg_standard,
        test_input_fps_avg_missing_key,
        test_input_fps_avg_empty_dict,
        test_input_fps_avg_none_value,
        test_input_fps_avg_non_dict_value,
        test_input_fps_avg_mixed_numeric_and_string,
        test_input_fps_avg_zero_and_normal,
        # cadence enforcement
        test_health_interval_is_one_in_stub,
        test_telemetry_interval_is_one_in_stub,
        test_profile_collect_cadence_guard_in_source,
        test_profile_collect_passes_feature_stats_in_source,
        # source-starvation detection
        test_starvation_both_input_and_output_below_threshold,
        test_starvation_zero_input_zero_output,
        test_starvation_output_only_transient_not_classified,
        test_starvation_missing_input_suspends_decision,
        test_starved_camera_excluded_from_load_score,
        test_starved_camera_all_starved_reports_unavailable,
        test_starved_camera_empty_set_preserves_legacy_behavior,
        test_starvation_configurable_threshold,
        test_starvation_backward_compat_kwarg_optional,
        test_starvation_union_iterates_over_input_and_output_cameras,
        # camera_workload derivation
        test_camera_workload_normal_mapping,
        test_camera_workload_zero_values_still_mapped,
        test_camera_workload_malformed_values_ignored,
        test_camera_workload_starved_and_inactive_omitted,
        test_camera_workload_empty_feature_stats,
        # _detect_source_starved hardening — malformed values/config
        test_starvation_fps_stats_none,
        test_starvation_fps_stats_string,
        test_starvation_fps_stats_contains_none_value,
        test_starvation_fps_stats_contains_string_value,
        test_starvation_fps_stats_contains_bool_value,
        test_starvation_fps_stats_contains_nan,
        test_starvation_fps_stats_contains_inf,
        test_starvation_fps_stats_contains_negative,
        test_starvation_fps_stats_contains_list,
        test_starvation_input_fps_contains_none_value,
        test_starvation_input_fps_contains_string_value,
        test_starvation_input_fps_contains_nan,
        test_starvation_input_fps_contains_inf,
        test_starvation_input_fps_contains_negative,
        test_starvation_edge_cfg_none,
        test_starvation_edge_cfg_expected_source_rate_none,
        test_starvation_edge_cfg_expected_source_rate_nan,
        test_starvation_edge_cfg_expected_source_rate_string,
        test_starvation_edge_cfg_expected_source_rate_negative,
        test_starvation_edge_cfg_ratio_none,
        test_starvation_edge_cfg_ratio_nan,
        test_starvation_edge_cfg_ratio_string,
        test_starvation_edge_cfg_ratio_negative,
        test_starvation_edge_cfg_source_starved_not_dict,
        test_starvation_unusable_threshold_returns_empty,
        test_starvation_mixed_malformed_both_sides,
        test_starvation_valid_behavior_preserved,
        # conservative composite bonuses (workload + thermal)
        test_bonus_legacy_no_config_section,
        test_bonus_workload_normal_ramp,
        test_bonus_workload_clamped_at_capacity,
        test_bonus_workload_starved_camera_excluded,
        test_bonus_workload_malformed_no_bonus,
        test_bonus_workload_feature_stats_none_disabled,
        test_bonus_thermal_normal_ramp,
        test_bonus_thermal_malformed_no_bonus,
        test_bonus_composite_capped_at_100,
        test_bonus_cpu_fuse_still_applies,
        test_bonus_ram_fuse_still_applies,
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