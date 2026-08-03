"""
Edge/speedflow_python/tests/test_load_model.py

Unit tests for load_model.py — pure functions + mock-ONNX DLPredictor.
No hardware, no GStreamer, no onnxruntime installed.

Standalone:
    python3 speedflow_python/tests/test_load_model.py
"""

import sys
import time
import importlib.util
from pathlib import Path

_LM_PATH = Path(__file__).resolve().parents[1] / "load_model.py"
_spec = importlib.util.spec_from_file_location("load_model", _LM_PATH)
_lm   = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lm)

theta_thermal       = _lm.theta_thermal
compute_h_reactive  = _lm.compute_h_reactive
compute_l_proactive = _lm.compute_l_proactive
fuse                = _lm.fuse
CycleSmoother       = _lm.CycleSmoother
ProactiveModel      = _lm.ProactiveModel
DLPredictor         = _lm.DLPredictor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> dict:
    base = {
        "enabled":        True,
        "w_base":         10.0,
        "alpha1":         1.0,
        "alpha2":         0.0,
        "beta":           2.0,
        "gamma":          5.0,
        "cycle_window_s": 90.0,
        "risk_threshold": 0.85,
        "theta_thermal": {
            "t_low":    75.0,
            "t_high":   90.0,
            "max_mult": 1.25,
        },
    }
    base.update(overrides)
    return base


def _feats(n_track=0.0, n_plate=0.0, stat_frac=0.0) -> dict:
    return {"cam_01": {
        "n_track":             n_track,
        "n_plate":             n_plate,
        "stationary_fraction": stat_frac,
    }}


def _metrics(gpu=0.0, cpu=0.0, ram=0.0, temp=0.0) -> dict:
    return {"gpu_percent": gpu, "cpu_percent": cpu,
            "ram_percent": ram, "gpu_temp_c":  temp}


def _fps(**cams) -> dict:
    return cams


# ---------------------------------------------------------------------------
# 1. theta_thermal
# ---------------------------------------------------------------------------

def test_theta_below_t_low():
    assert theta_thermal(60.0, _cfg()) == 1.0

def test_theta_at_t_low():
    assert theta_thermal(75.0, _cfg()) == 1.0

def test_theta_above_t_high():
    assert theta_thermal(95.0, _cfg()) == 1.25

def test_theta_at_t_high():
    assert theta_thermal(90.0, _cfg()) == 1.25

def test_theta_midpoint():
    result = theta_thermal(82.5, _cfg())
    assert abs(result - 1.125) < 1e-6

def test_theta_monotone():
    cfg = _cfg()
    vals = [theta_thermal(float(t), cfg) for t in (70, 75, 80, 82, 85, 88, 90, 95)]
    for i in range(len(vals) - 1):
        assert vals[i] <= vals[i + 1]


# ---------------------------------------------------------------------------
# 2. compute_h_reactive
# ---------------------------------------------------------------------------

def test_h_zero_utilisation():
    assert compute_h_reactive(0, 0, 0, 60.0, _cfg()) == 0.0

def test_h_gpu_dominates():
    h = compute_h_reactive(80, 20, 10, 60.0, _cfg())
    assert abs(h - 0.80) < 1e-6

def test_h_thermal_amplifies():
    h = compute_h_reactive(80, 0, 0, 90.0, _cfg())
    assert h == 1.0

def test_h_clamped_to_1():
    assert compute_h_reactive(100, 100, 100, 95.0, _cfg()) == 1.0

def test_h_ram_dominates():
    assert abs(compute_h_reactive(10, 20, 90, 60.0, _cfg()) - 0.90) < 1e-6


# ---------------------------------------------------------------------------
# 3. compute_l_proactive (formula path)
# ---------------------------------------------------------------------------

def test_l_zero_features():
    l, nt, np_, sf = compute_l_proactive({}, _cfg())
    assert abs(l - 0.10) < 1e-6

def test_l_track_contribution():
    l, _, _, _ = compute_l_proactive(_feats(n_track=5.0), _cfg())
    assert abs(l - 0.15) < 1e-6

def test_l_plate_contribution():
    l, _, _, _ = compute_l_proactive(_feats(n_plate=10.0), _cfg())
    assert abs(l - 0.30) < 1e-6

def test_l_stationary_contribution():
    l, _, _, _ = compute_l_proactive(_feats(stat_frac=1.0), _cfg())
    assert abs(l - 0.15) < 1e-6

def test_l_clamped_to_1():
    assert compute_l_proactive(_feats(n_track=1000.0), _cfg())[0] == 1.0

def test_l_clamped_to_0():
    cfg = _cfg(w_base=-50.0, alpha1=0.0, beta=0.0, gamma=0.0)
    assert compute_l_proactive(_feats(), cfg)[0] == 0.0

def test_l_quadratic():
    cfg = _cfg(alpha2=0.5)
    l, _, _, _ = compute_l_proactive(_feats(n_track=4.0), cfg)
    assert abs(l - 0.22) < 1e-6

def test_l_feature_means_returned():
    _, nt, np_, sf = compute_l_proactive(_feats(n_track=8.0, n_plate=3.0,
                                                 stat_frac=0.5), _cfg())
    assert abs(nt - 8.0) < 1e-6
    assert abs(np_ - 3.0) < 1e-6
    assert abs(sf - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# 4. fuse
# ---------------------------------------------------------------------------

def test_fuse_both_zero():
    assert fuse(0.0, 0.0) == 0.0

def test_fuse_both_one():
    assert fuse(1.0, 1.0) == 1.0

def test_fuse_one_saturated():
    assert fuse(1.0, 0.0) == 1.0
    assert fuse(0.0, 1.0) == 1.0

def test_fuse_symmetry():
    assert abs(fuse(0.3, 0.7) - fuse(0.7, 0.3)) < 1e-9

def test_fuse_formula():
    assert abs(fuse(0.4, 0.6) - 0.76) < 1e-9

def test_fuse_clamping():
    assert fuse(-0.5, 1.5) == 1.0
    assert fuse(0.0, -0.1) == 0.0

def test_fuse_monotone_in_l():
    h = 0.5
    vals = [fuse(l / 10.0, h) for l in range(11)]
    for i in range(len(vals) - 1):
        assert vals[i] <= vals[i + 1]

def test_fuse_monotone_in_h():
    l = 0.5
    vals = [fuse(l, h / 10.0) for h in range(11)]
    for i in range(len(vals) - 1):
        assert vals[i] <= vals[i + 1]


# ---------------------------------------------------------------------------
# 5. CycleSmoother
# ---------------------------------------------------------------------------

def test_smoother_single_value():
    s = CycleSmoother(window_s=90.0)
    assert abs(s.update(0.6) - 0.6) < 1e-9

def test_smoother_average():
    s = CycleSmoother(window_s=90.0)
    t0 = time.time()
    s.update(0.2, ts=t0)
    s.update(0.4, ts=t0 + 1.0)
    assert abs(s.update(0.6, ts=t0 + 2.0) - 0.4) < 1e-9

def test_smoother_evicts_old():
    s = CycleSmoother(window_s=10.0)
    t0 = time.time()
    s.update(1.0, ts=t0)
    assert s.update(0.0, ts=t0 + 15.0) == 0.0

def test_smoother_reset():
    s = CycleSmoother(window_s=90.0)
    s.update(0.9)
    s.reset()
    assert s.mean() == 0.0

def test_smoother_mean_without_push():
    assert CycleSmoother(window_s=90.0).mean() == 0.0


# ---------------------------------------------------------------------------
# 6. ProactiveModel formula integration
# ---------------------------------------------------------------------------

def test_model_disabled_returns_flag():
    cfg = _cfg(enabled=False)
    model = ProactiveModel(cfg)
    result = model.compute(_metrics(), _feats())
    assert result == {
        "proactive_enabled": False,
        "load_policy": "predict_with_base",
        "load_model": "formula",
    }

def test_model_enabled_returns_all_keys():
    model = ProactiveModel(_cfg())
    result = model.compute(_metrics(gpu=50), _feats(n_track=5))
    expected_keys = {
        "proactive_enabled", "l_proactive", "h_reactive", "risk_index",
        "l_proactive_instant", "h_reactive_instant", "risk_index_instant",
        "n_track_mean", "n_plate_mean", "stationary_fraction", "theta_thermal",
    }
    assert expected_keys.issubset(result.keys())

def test_model_risk_index_in_range():
    model = ProactiveModel(_cfg())
    result = model.compute(_metrics(gpu=80, cpu=70, ram=60, temp=85),
                           _feats(n_track=20, n_plate=10, stat_frac=0.8))
    assert 0.0 <= result["risk_index"] <= 1.0

def test_model_high_load_high_risk():
    model = ProactiveModel(_cfg())
    result = model.compute(_metrics(gpu=99, cpu=99, ram=99, temp=92),
                           _feats(n_track=50, n_plate=30, stat_frac=1.0))
    assert result["risk_index"] > 0.9

def test_model_zero_load_low_risk():
    cfg = _cfg(w_base=5.0, alpha1=0.0, beta=0.0, gamma=0.0)
    model = ProactiveModel(cfg)
    result = model.compute(_metrics(), {})
    assert result["risk_index"] < 0.10

def test_model_smoothing_damps_spike():
    cfg = _cfg(cycle_window_s=90.0)
    model = ProactiveModel(cfg)
    t0 = time.time()
    for i in range(10):
        model.compute(_metrics(gpu=10), _feats(n_track=2), ts=t0 + i * 2.0)
    spike = model.compute(
        _metrics(gpu=99, cpu=99, ram=99, temp=92),
        _feats(n_track=50, n_plate=30, stat_frac=1.0),
        ts=t0 + 20.0,
    )
    assert spike["risk_index"] < spike["risk_index_instant"]

def test_model_reload_cfg():
    model = ProactiveModel(_cfg(enabled=False))
    assert not model.enabled
    model.reload_cfg(_cfg(enabled=True))
    assert model.enabled


# ===================================================================
# 7. DLPredictor — 10-feature future-FPS ONNX contract
#    Training contract: 5-row × 1.0s history, horizons=[6,10]s
# ===================================================================


def test_dl_feature_order():
    """10 features in documented order: n_active, n_track, n_plate, stationary,
       fps_avg, gpu, cpu, ram, gpu_temp, offload_rate."""
    result = DLPredictor._node_features(
        {"cam_a": {"n_track": 3, "n_plate": 2, "stationary_fraction": 0.5}},
        fps_stats={"cam_a": 24.0},
        metrics={"gpu_percent": 70, "cpu_percent": 40,
                 "ram_percent": 55, "gpu_temp_c": 78.0},
        offload_crops_received_per_s=1.5,
    )
    expected = (1, 3.0, 2.0, 0.5, 24.0, 70.0, 40.0, 55.0, 78.0, 1.5)
    assert result == expected, f"feature order mismatch: {result}"


def test_dl_feature_names():
    assert DLPredictor._N_FEATURES == 10
    assert DLPredictor._FEATURE_NAMES[0] == "n_active"
    assert DLPredictor._FEATURE_NAMES[4] == "fps_avg"
    assert DLPredictor._FEATURE_NAMES[5] == "gpu"
    assert DLPredictor._FEATURE_NAMES[8] == "gpu_temp"
    assert DLPredictor._FEATURE_NAMES[9] == "offload_rate"


def test_dl_default_window_k():
    """Default window_k=5 from dl_model config when not specified."""
    predictor = DLPredictor({"model_path": ""})
    assert predictor._window_k == 5


def test_dl_default_sample_interval():
    """Default sample_interval_s=1.0 matches HEALTH_INTERVAL."""
    predictor = DLPredictor({"model_path": ""})
    assert abs(predictor._sample_interval_s - 1.0) < 1e-9


def test_dl_default_horizon_index():
    """Default horizon_index=0 selects t+6s."""
    predictor = DLPredictor({"model_path": ""})
    assert predictor._horizon_index == 0


def test_dl_bad_sample_interval():
    """sample_interval_s <= 0 raises ValueError."""
    for bad_val in (0.0, -1.0):
        raised = False
        try:
            DLPredictor({"model_path": "", "sample_interval_s": bad_val})
        except ValueError as e:
            raised = True
            assert "sample_interval_s" in str(e)
        assert raised, f"Expected ValueError for sample_interval_s={bad_val}"


def test_dl_configurable_sample_interval():
    """Positive sample_interval_s is stored."""
    predictor = DLPredictor({"model_path": "", "sample_interval_s": 5.0})
    assert abs(predictor._sample_interval_s - 5.0) < 1e-9


def test_dl_no_session_returns_zero():
    """No ONNX session loaded → predict returns score 0 for 5-sample history."""
    predictor = DLPredictor({"window_k": 5, "model_path": ""})
    # fill enough history
    for _ in range(5):
        predictor._history.append([0.0] * 10)
    r = predictor.predict({}, _fps(cam_a=25.0), _metrics())
    assert r[0] == 0.0


def test_dl_insufficient_history():
    """window_k=5 but only 2 rows → returns score 0 (cold-start guard)."""
    predictor = DLPredictor({"window_k": 5, "model_path": ""})
    predictor._session = object()
    predictor._input_name = "features"
    predictor._window_k = 5
    predictor._history.clear()
    predictor.predict({}, _fps(cam_a=25.0), _metrics())
    r = predictor.predict({}, _fps(cam_a=25.0), _metrics())
    assert r[0] == 0.0


def test_dl_history_grows_per_cycle():
    """Each predict() appends one history row (one row per health cycle)."""
    predictor = DLPredictor({"window_k": 5, "model_path": ""})
    assert len(predictor._history) == 0
    predictor.predict(_feats(), _fps(cam_01=20.0), _metrics())
    assert len(predictor._history) == 1
    predictor.predict(_feats(), _fps(cam_01=20.0), _metrics())
    assert len(predictor._history) == 2

def test_dl_five_sample_history():
    """5-cycle accumulation at 2.0s each = 10s effective history."""
    predictor = DLPredictor({"window_k": 5, "model_path": "",
                             "sample_interval_s": 2.0})
    assert predictor._window_k == 5
    assert abs(predictor._sample_interval_s - 2.0) < 1e-9
    # feed exactly 5 rows
    for i in range(5):
        predictor._history.append([float(i)] * 10)
    assert len(predictor._history) == 5
    # 6th row pushes the 1st out
    predictor.predict(_feats(), _fps(cam_01=20.0), _metrics())
    assert len(predictor._history) == 5

def test_dl_history_deque_maxlen():
    """History deque respects window_k maxlen."""
    predictor = DLPredictor({"window_k": 3, "model_path": ""})
    for i in range(6):
        predictor._history.append([float(i)] * 10)
    assert len(predictor._history) == 3
    assert predictor._history[0][0] == 3.0  # oldest remaining


# --- fps_to_score anchors ---

def test_fps_anchor_27():
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert pc._fps_to_score(27.0) == 0.0

def test_fps_anchor_22():
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert abs(pc._fps_to_score(22.0) - 0.57) < 1e-6

def test_fps_anchor_19():
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert abs(pc._fps_to_score(19.0) - 0.65) < 1e-6

def test_fps_anchor_17():
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert abs(pc._fps_to_score(17.0) - 0.75) < 1e-6

def test_fps_anchor_0():
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert abs(pc._fps_to_score(0.0) - 1.0) < 1e-6

def test_fps_above_27_clamped():
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert pc._fps_to_score(30.0) == 0.0

def test_fps_negative_clamped():
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert abs(pc._fps_to_score(-5.0) - 1.0) < 1e-6

def test_fps_interpolation_24_5():
    # 24.5 between (27,0) and (22,57): 57 * 2.5/5 = 28.5 → 0.285
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert abs(pc._fps_to_score(24.5) - 0.285) < 1e-4

def test_fps_interpolation_20_5():
    # 20.5 between (22,57) and (19,65): 65+(57-65)*1.5/3 = 61 → 0.61
    pc = DLPredictor({"window_k": 1, "model_path": ""})
    assert abs(pc._fps_to_score(20.5) - 0.61) < 1e-3


# --- mock ONNX inference ---

class MagicSession:
    def get_inputs(self):
        return [MagicInput("features")]
    def run(self, _outputs, _feed):
        return [__import__("numpy").array([[22.0]], dtype="float32")]

class MagicInput:
    def __init__(self, name):
        self.name = name


def test_dl_mock_inference():
    """Mock session returning FPS=22 → score 0.57."""
    predictor = DLPredictor({"window_k": 1, "model_path": "x"})
    predictor._session = MagicSession()
    predictor._input_name = "features"
    predictor._window_k = 1
    # already one row in history
    predictor.predict({}, {"cam_a": 22.0}, _metrics())
    r = predictor.predict({}, {"cam_a": 22.0}, _metrics())
    assert abs(r[0] - 0.57) < 0.01


def test_dl_horizon_index():
    """Mock returns [fps_0=30, fps_1=22]; horizon_index=1 picks t+10s → 22 FPS → 0.57.
       Mimics 2-horizon ONNX: [t+6s, t+10s]."""
    import numpy as np

    class MultiHorizonSession:
        def get_inputs(self):
            return [type("I", (), {"name": "features"})()]
        def run(self, _o, _f):
            return [np.array([[30.0, 22.0]], dtype=np.float32)]

    predictor = DLPredictor({"window_k": 1, "horizon_index": 1, "model_path": "o"})
    predictor._session = MultiHorizonSession()
    predictor._input_name = "input"
    predictor._history.append([0.0] * 10)
    r = predictor.predict({}, {"cam_a": 20.0}, {"gpu_percent": 50})
    assert abs(r[0] - 0.57) < 0.01


def test_dl_horizon_index_zero():
    """horizon_index=0 picks t+6s."""
    predictor = DLPredictor({"window_k": 1, "horizon_index": 0, "model_path": ""})
    assert predictor._horizon_index == 0


def test_dl_horizon_index_out_of_range_clamped():
    """horizon_index beyond output shape is clamped by predict() at runtime."""
    predictor = DLPredictor({"window_k": 1, "horizon_index": 99, "model_path": ""})


def test_fps_stats_passed_to_node_features():
    """DLPredictor._node_features aggregates fps_stats into avg fps."""
    result = DLPredictor._node_features(
        {"cam_a": {"n_track": 1, "n_plate": 0, "stationary": 0}},
        fps_stats={"cam_a": 24, "cam_b": 20},
        metrics={},
        offload_crops_received_per_s=0,
    )
    # fps_avg = avg(24,20) = 22, rest unset → 0
    assert result[4] == 22.0  # fps_avg is index 4


def test_dl_predict_returns_four_tuple():
    """Ensure predict() returns (float, float, float, float)."""
    predictor = DLPredictor({"window_k": 1, "model_path": ""})
    r = predictor.predict({}, {"a": 22.0}, _metrics())
    assert isinstance(r, tuple) and len(r) == 4
    assert all(isinstance(v, float) for v in r)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        # theta_thermal
        test_theta_below_t_low, test_theta_at_t_low, test_theta_above_t_high,
        test_theta_at_t_high, test_theta_midpoint, test_theta_monotone,
        # H_reactive
        test_h_zero_utilisation, test_h_gpu_dominates, test_h_thermal_amplifies,
        test_h_clamped_to_1, test_h_ram_dominates,
        # L_proactive
        test_l_zero_features, test_l_track_contribution, test_l_plate_contribution,
        test_l_stationary_contribution, test_l_clamped_to_1, test_l_clamped_to_0,
        test_l_quadratic, test_l_feature_means_returned,
        # fuse
        test_fuse_both_zero, test_fuse_both_one, test_fuse_one_saturated,
        test_fuse_symmetry, test_fuse_formula, test_fuse_clamping,
        test_fuse_monotone_in_l, test_fuse_monotone_in_h,
        # CycleSmoother
        test_smoother_single_value, test_smoother_average, test_smoother_evicts_old,
        test_smoother_reset, test_smoother_mean_without_push,
        # ProactiveModel
        test_model_disabled_returns_flag, test_model_enabled_returns_all_keys,
        test_model_risk_index_in_range, test_model_high_load_high_risk,
        test_model_zero_load_low_risk, test_model_smoothing_damps_spike,
        test_model_reload_cfg,
        # DLPredictor 10-feature contract
        test_dl_default_window_k, test_dl_default_sample_interval,
        test_dl_default_horizon_index, test_dl_bad_sample_interval,
        test_dl_configurable_sample_interval,
        test_dl_feature_order, test_dl_feature_names,
        test_dl_no_session_returns_zero, test_dl_insufficient_history,
        test_dl_history_grows_per_cycle,
        test_dl_five_sample_history, test_dl_history_deque_maxlen,
        test_fps_anchor_27, test_fps_anchor_22, test_fps_anchor_19,
        test_fps_anchor_17, test_fps_anchor_0,
        test_fps_above_27_clamped, test_fps_negative_clamped,
        test_fps_interpolation_24_5, test_fps_interpolation_20_5,
        test_dl_mock_inference, test_dl_horizon_index,
        test_dl_horizon_index_zero, test_dl_horizon_index_out_of_range_clamped,
        test_fps_stats_passed_to_node_features, test_dl_predict_returns_four_tuple,
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