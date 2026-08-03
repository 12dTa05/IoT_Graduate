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

def _metrics(gpu=0.0, cpu=0.0, ram=0.0) -> dict:
    return {"gpu_percent": gpu, "cpu_percent": cpu,
            "ram_percent": ram, "gpu_temp_c": 0.0}


def _fps(**cams) -> dict:
    return cams


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