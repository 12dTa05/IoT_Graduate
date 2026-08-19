"""
Unit tests for the Base + Additive Bonus load_score formula.

Formula:
  load_score = min(100, fps_score + workload_bonus + thermal_bonus + recv_bonus + trend_bonus)
  Thresholds: L3 = 60.0, L2 = 67.0, L1 = 80.0.
"""

from __future__ import annotations
import importlib
import sys
import time
import types
from pathlib import Path

EDGE = Path(__file__).resolve().parents[1]
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))

def _install_stubs():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = dotenv

    package = sys.modules.get("speedflow_python")
    if package is None:
        package = types.ModuleType("speedflow_python")
        package.__path__ = [str(EDGE / "speedflow_python")]
        sys.modules["speedflow_python"] = package

    settings = sys.modules.get("speedflow_python.settings")
    if settings is None:
        settings = types.ModuleType("speedflow_python.settings")
        sys.modules["speedflow_python.settings"] = settings
    for key, value in {
        "ROOT": EDGE,
        "NODE_ID": "host-test",
        "HEALTH_INTERVAL": 1.0,
        "HEALTH_LOG_EVERY": 30,
        "TARGET_FPS": 27.0,
        "CAMERAS_FILE": "cameras.yml",
        "EDGE_NODE_FILE": "edge_node.yml",
        "ZENOH_CONNECT": "",
        "ZENOH_MODE": "peer",
        "SOURCE_FPS_OVERRIDE": -1.0,
        "LOAD_POLICY": "predict_with_base",
        "LOAD_MODEL": "",
        "TELEMETRY_INTERVAL": 1.0,
        "FPS_STATS_FILE": "fps_stats.json",
        "HARDWARE_METRICS_FILE": "hardware_metrics.json",
        "MONITOR_URL": "",
        "ADVERTISE_IP": "127.0.0.1",
    }.items():
        setattr(settings, key, value)

    session = types.ModuleType("speedflow_python.zenoh_session")
    session.make_session = lambda: None
    sys.modules["speedflow_python.zenoh_session"] = session

_install_stubs()

import health_agent
from health_agent import (
    _compute_load_score,
    _compute_load_score_breakdown,
    _FPS_HISTORY,
)


def _metrics(gpu=0.0, cpu=0.0, ram=0.0, gpu_temp_c=0.0, offload_crops_received_per_s=0.0) -> dict:
    return {
        "gpu_percent": gpu,
        "cpu_percent": cpu,
        "ram_percent": ram,
        "gpu_temp_c": gpu_temp_c,
        "offload_crops_received_per_s": offload_crops_received_per_s,
    }


def _fps(**cams) -> dict:
    return cams


def test_pure_fps_scaling():
    """At TARGET_FPS (27), fps_score is 0; at 22 FPS it is 57.0; at 19 FPS it is 65.0; at 17 FPS it is 75.0."""
    s_27, _ = _compute_load_score(_metrics(), _fps(cam1=27.0))
    assert s_27 == 0.0

    s_25, _ = _compute_load_score(_metrics(), _fps(cam1=25.0))
    # 57.0 * (27 - 25) / 5 = 22.8
    assert abs(s_25 - 22.8) < 0.1

    s_22, _ = _compute_load_score(_metrics(), _fps(cam1=22.0))
    assert abs(s_22 - 57.0) < 0.1

    s_19, _ = _compute_load_score(_metrics(), _fps(cam1=19.0))
    assert abs(s_19 - 65.0) < 0.1

    s_17, _ = _compute_load_score(_metrics(), _fps(cam1=17.0))
    assert abs(s_17 - 75.0) < 0.1


def test_workload_bonus_capacity_40():
    """Workload bonus adds up to 15.0 points with capacity 40.0."""
    # 20 tracks on cam1 -> 20 / 40 * 15 = 7.5 points
    feat = {"cam1": {"n_track": 15.0, "n_plate": 5.0, "stationary_fraction": 0.0}}
    s_27, _ = _compute_load_score(_metrics(), _fps(cam1=27.0), feature_stats=feat)
    assert abs(s_27 - 7.5) < 0.1

    # 40 tracks on cam1 -> 15.0 points (max)
    feat_max = {"cam1": {"n_track": 30.0, "n_plate": 10.0, "stationary_fraction": 0.0}}
    s_max, _ = _compute_load_score(_metrics(), _fps(cam1=27.0), feature_stats=feat_max)
    assert abs(s_max - 15.0) < 0.1

    # Over 40 tracks -> clamped to 15.0
    feat_over = {"cam1": {"n_track": 50.0, "n_plate": 20.0, "stationary_fraction": 0.0}}
    s_over, _ = _compute_load_score(_metrics(), _fps(cam1=27.0), feature_stats=feat_over)
    assert abs(s_over - 15.0) < 0.1


def test_thermal_bonus_ramp():
    """Thermal bonus ramps from 70C (0.0) to 85C (5.0 max)."""
    s_65, _ = _compute_load_score(_metrics(gpu_temp_c=65.0), _fps(cam1=27.0))
    assert s_65 == 0.0

    s_77_5, _ = _compute_load_score(_metrics(gpu_temp_c=77.5), _fps(cam1=27.0))
    assert abs(s_77_5 - 2.5) < 0.1

    s_85, _ = _compute_load_score(_metrics(gpu_temp_c=85.0), _fps(cam1=27.0))
    assert abs(s_85 - 5.0) < 0.1

    s_90, _ = _compute_load_score(_metrics(gpu_temp_c=90.0), _fps(cam1=27.0))
    assert abs(s_90 - 5.0) < 0.1


def test_recv_crops_bonus():
    """Received crops scale up to 5.0 points at capacity 10.0."""
    s_5, _ = _compute_load_score(_metrics(offload_crops_received_per_s=5.0), _fps(cam1=27.0))
    assert abs(s_5 - 2.5) < 0.1

    s_10, _ = _compute_load_score(_metrics(offload_crops_received_per_s=10.0), _fps(cam1=27.0))
    assert abs(s_10 - 5.0) < 0.1


def test_fps_trend_bonus():
    """Declining FPS adds up to 5.0 points for decline rate >= 2.0 FPS/s."""
    _FPS_HISTORY.clear()
    now = time.monotonic()
    _FPS_HISTORY.append((now - 1.0, 27.0))

    # Current fps = 25.0 -> slope = -2.0 FPS/s over 1s -> trend_bonus = 5.0
    s_trend, _ = _compute_load_score(_metrics(), _fps(cam1=25.0))
    # fps_score (22.8) + trend_bonus (5.0) = 27.8
    assert abs(s_trend - 27.8) < 0.2


def test_early_warning_trigger():
    """At 22 FPS (57.0) + moderate traffic (20 objects -> 7.5), score = 64.5 -> triggers L3 (60.0)."""
    feat = {"cam1": {"n_track": 15.0, "n_plate": 5.0, "stationary_fraction": 0.0}}
    score, _ = _compute_load_score(_metrics(), _fps(cam1=22.0), feature_stats=feat)
    assert score >= 60.0  # L3 threshold passed


def test_breakdown_schema_and_values():
    """Breakdown returns all 7 audit keys with accurate values."""
    _FPS_HISTORY.clear()
    feat = {"cam1": {"n_track": 20.0, "n_plate": 20.0, "stationary_fraction": 0.0}}
    metrics = _metrics(gpu_temp_c=85.0, offload_crops_received_per_s=10.0)
    bd = _compute_load_score_breakdown(metrics, _fps(cam1=22.0), feature_stats=feat)

    assert "fps_score" in bd
    assert "workload_bonus" in bd
    assert "thermal_bonus" in bd
    assert "recv_bonus" in bd
    assert "trend_bonus" in bd
    assert "composite_score" in bd
    assert "load_score" in bd

    assert bd["fps_score"] == 57.0
    assert bd["workload_bonus"] == 15.0  # 40 / 40 * 15
    assert bd["thermal_bonus"] == 5.0    # 85C -> max 5
    assert bd["recv_bonus"] == 5.0       # 10 / 10 * 5
    assert bd["trend_bonus"] == 0.0
    assert bd["composite_score"] == 82.0
    assert bd["load_score"] == 82.0
