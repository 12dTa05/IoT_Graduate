"""
Edge/tests/test_load_model.py

Unit tests for speedflow_python/load_model.py.
No hardware dependencies — pure function tests only.

Run from Edge/:
    python3 -m pytest tests/test_load_model.py -v
or standalone:
    python3 tests/test_load_model.py
"""

import sys
import time
import importlib.util
from pathlib import Path

# Import load_model directly from its file so this test has zero hardware
# dependencies (no GStreamer, no DeepStream, no dotenv, no jtop).
# We bypass speedflow_python/__init__.py which eagerly imports core_pipeline.
_LM_PATH = Path(__file__).resolve().parents[1] / "speedflow_python" / "load_model.py"
_spec = importlib.util.spec_from_file_location("load_model", _LM_PATH)
_lm   = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lm)

theta_thermal       = _lm.theta_thermal
compute_h_reactive  = _lm.compute_h_reactive
compute_l_proactive = _lm.compute_l_proactive
fuse                = _lm.fuse
CycleSmoother       = _lm.CycleSmoother
ProactiveModel      = _lm.ProactiveModel
_DEFAULT_CFG        = _lm._DEFAULT_CFG

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> dict:
    """Build a minimal proactive config dict with optional overrides."""
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


# ---------------------------------------------------------------------------
# 1. theta_thermal
# ---------------------------------------------------------------------------

def test_theta_below_t_low():
    cfg = _cfg()
    assert theta_thermal(60.0, cfg) == 1.0

def test_theta_at_t_low():
    cfg = _cfg()
    assert theta_thermal(75.0, cfg) == 1.0

def test_theta_above_t_high():
    cfg = _cfg()
    assert theta_thermal(95.0, cfg) == 1.25

def test_theta_at_t_high():
    cfg = _cfg()
    assert theta_thermal(90.0, cfg) == 1.25

def test_theta_midpoint():
    cfg = _cfg()
    # At 82.5°C (midway between 75 and 90) → multiplier = 1.125
    result = theta_thermal(82.5, cfg)
    assert abs(result - 1.125) < 1e-6

def test_theta_monotone():
    cfg = _cfg()
    temps = [70, 75, 80, 82, 85, 88, 90, 95]
    values = [theta_thermal(float(t), cfg) for t in temps]
    for i in range(len(values) - 1):
        assert values[i] <= values[i + 1], f"Not monotone at index {i}"


# ---------------------------------------------------------------------------
# 2. compute_h_reactive
# ---------------------------------------------------------------------------

def test_h_zero_utilisation():
    h = compute_h_reactive(0, 0, 0, 60.0, _cfg())
    assert h == 0.0

def test_h_gpu_dominates():
    # GPU=80%, CPU=20%, RAM=10%, no thermal
    h = compute_h_reactive(80, 20, 10, 60.0, _cfg())
    assert abs(h - 0.80) < 1e-6

def test_h_thermal_amplifies():
    # GPU=80% at 90°C → 0.80 × 1.25 = 1.0 (clamped)
    h = compute_h_reactive(80, 0, 0, 90.0, _cfg())
    assert h == 1.0

def test_h_clamped_to_1():
    h = compute_h_reactive(100, 100, 100, 95.0, _cfg())
    assert h == 1.0

def test_h_ram_dominates():
    h = compute_h_reactive(10, 20, 90, 60.0, _cfg())
    assert abs(h - 0.90) < 1e-6


# ---------------------------------------------------------------------------
# 3. compute_l_proactive
# ---------------------------------------------------------------------------

def test_l_zero_features():
    # W_base=10 / 100 = 0.10
    l, nt, np_, sf = compute_l_proactive({}, _cfg())
    assert abs(l - 0.10) < 1e-6

def test_l_track_contribution():
    # W_base=10, alpha1=1.0, N_track=5 → (10 + 5) / 100 = 0.15
    l, _, _, _ = compute_l_proactive(_feats(n_track=5.0), _cfg())
    assert abs(l - 0.15) < 1e-6

def test_l_plate_contribution():
    # W_base=10, beta=2.0, N_plate=10 → (10 + 20) / 100 = 0.30
    l, _, _, _ = compute_l_proactive(_feats(n_plate=10.0), _cfg())
    assert abs(l - 0.30) < 1e-6

def test_l_stationary_contribution():
    # W_base=10, gamma=5.0, S=1.0 → (10 + 5) / 100 = 0.15
    l, _, _, _ = compute_l_proactive(_feats(stat_frac=1.0), _cfg())
    assert abs(l - 0.15) < 1e-6

def test_l_clamped_to_1():
    # Very high counts push L above 1 → must be clamped
    l, _, _, _ = compute_l_proactive(_feats(n_track=1000.0), _cfg())
    assert l == 1.0

def test_l_clamped_to_0():
    # Negative w_base should not produce negative L
    cfg = _cfg(w_base=-50.0, alpha1=0.0, beta=0.0, gamma=0.0)
    l, _, _, _ = compute_l_proactive(_feats(), cfg)
    assert l == 0.0

def test_l_quadratic():
    # alpha2=0.5, N_track=4 → W_base + 1×4 + 0.5×16 = 10+4+8=22 → 0.22
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
# 4. fuse (noisy-OR)
# ---------------------------------------------------------------------------

def test_fuse_both_zero():
    assert fuse(0.0, 0.0) == 0.0

def test_fuse_both_one():
    assert fuse(1.0, 1.0) == 1.0

def test_fuse_one_saturated():
    # If either tier saturates → U → 1
    assert fuse(1.0, 0.0) == 1.0
    assert fuse(0.0, 1.0) == 1.0

def test_fuse_symmetry():
    assert abs(fuse(0.3, 0.7) - fuse(0.7, 0.3)) < 1e-9

def test_fuse_formula():
    # U = 1 - (1-0.4)(1-0.6) = 1 - 0.6×0.4 = 1 - 0.24 = 0.76
    assert abs(fuse(0.4, 0.6) - 0.76) < 1e-9

def test_fuse_clamping():
    # Inputs outside [0,1] should be clamped
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
    result = s.update(0.6)
    assert abs(result - 0.6) < 1e-9

def test_smoother_average():
    s = CycleSmoother(window_s=90.0)
    t0 = time.time()
    s.update(0.2, ts=t0)
    s.update(0.4, ts=t0 + 1.0)
    result = s.update(0.6, ts=t0 + 2.0)
    assert abs(result - 0.4) < 1e-9

def test_smoother_evicts_old():
    s = CycleSmoother(window_s=10.0)
    t0 = time.time()
    s.update(1.0, ts=t0)
    # Push a value 15 s later — old one is outside the 10-s window
    result = s.update(0.0, ts=t0 + 15.0)
    assert result == 0.0

def test_smoother_reset():
    s = CycleSmoother(window_s=90.0)
    s.update(0.9)
    s.reset()
    assert s.mean() == 0.0

def test_smoother_mean_without_push():
    s = CycleSmoother(window_s=90.0)
    assert s.mean() == 0.0


# ---------------------------------------------------------------------------
# 6. ProactiveModel integration
# ---------------------------------------------------------------------------

def test_model_disabled_returns_flag():
    cfg = _cfg(enabled=False)
    model = ProactiveModel(cfg)
    result = model.compute(_metrics(), _feats())
    assert result == {"proactive_enabled": False}

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
    # Only W_base contributes; hardware all zero; risk should be modest
    cfg = _cfg(w_base=5.0, alpha1=0.0, beta=0.0, gamma=0.0)
    model = ProactiveModel(cfg)
    result = model.compute(_metrics(), {})
    # L = 5/100 = 0.05, H = 0.0, U = 1-(0.95)(1.0) = 0.05
    assert result["risk_index"] < 0.10

def test_model_smoothing_damps_spike():
    """A single spike in L should not immediately push U to its peak."""
    cfg = _cfg(cycle_window_s=90.0)
    model = ProactiveModel(cfg)
    t0 = time.time()
    # 10 cycles at low load
    for i in range(10):
        model.compute(_metrics(gpu=10), _feats(n_track=2), ts=t0 + i * 2.0)
    # One spike
    spike = model.compute(
        _metrics(gpu=99, cpu=99, ram=99, temp=92),
        _feats(n_track=50, n_plate=30, stat_frac=1.0),
        ts=t0 + 20.0,
    )
    # Smoothed U should be lower than instantaneous U
    assert spike["risk_index"] < spike["risk_index_instant"]

def test_model_reload_cfg():
    model = ProactiveModel(_cfg(enabled=False))
    assert not model.enabled
    model.reload_cfg(_cfg(enabled=True))
    assert model.enabled


# ---------------------------------------------------------------------------
# Runner
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
