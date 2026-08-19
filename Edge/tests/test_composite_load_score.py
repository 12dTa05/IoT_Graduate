"""
Unit tests for the weighted multi-signal composite load_score formula.

Formula:
  load_score = min(100, w_fps * fps_comp + w_work * workload_comp + w_therm * thermal_comp + w_recv * recv_comp + w_trend * trend_comp)
  Weights default: 50/25/10/5/10 (sum = 100).
  Ladder defaults: L3 = 42.0, L2 = 50.0, L1 = 60.0.
"""

from __future__ import annotations
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
        "FPS_STATS_FILE": "/tmp/fps_stats.json",
        "MONITOR_URL": "",
        "ADVERTISE_IP": "",
        "LOAD_POLICY": "fps_dominant",
        "LOAD_MODEL": "",
        "TELEMETRY_INTERVAL": 1.0,
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


def test_pure_fps_component():
    """At TARGET_FPS (27), fps_score is 0; at 22 FPS, fps_score is 28.5 (50 * 0.57)."""
    s_27, _ = _compute_load_score(_metrics(), _fps(cam1=27.0))
    assert s_27 == 0.0

    s_22, _ = _compute_load_score(_metrics(), _fps(cam1=22.0))
    assert abs(s_22 - 28.5) < 0.05

    s_19, _ = _compute_load_score(_metrics(), _fps(cam1=19.0))
    assert abs(s_19 - 32.5) < 0.05

    s_17, _ = _compute_load_score(_metrics(), _fps(cam1=17.0))
    assert abs(s_17 - 37.5) < 0.05


def test_workload_component_early_warning():
    """
    Early warning property: High traffic (30 tracks + 30 plates = 60 total)
    at 25 FPS (healthy) raises score from 11.4 to 36.4 (11.4 + 25.0).
    """
    feat = {"cam1": {"n_track": 30, "n_plate": 30}}
    s_healthy_busy, _ = _compute_load_score(
        _metrics(), _fps(cam1=25.0), feature_stats=feat,
    )
    # fps_score = 50 * (0.57 * 2/5) = 11.4
    # workload_comp = 60 / 60 = 1.0 -> 25.0
    # total = 36.4
    assert abs(s_healthy_busy - 36.4) < 0.1


def test_thermal_component_contribution():
    """Thermal ramp from 70C to 85C adds up to 10.0."""
    s_onset, _ = _compute_load_score(
        _metrics(gpu_temp_c=70.0), _fps(cam1=27.0),
    )
    assert s_onset == 0.0

    s_mid, _ = _compute_load_score(
        _metrics(gpu_temp_c=77.5), _fps(cam1=27.0),
    )
    assert abs(s_mid - 5.0) < 0.05

    s_crit, _ = _compute_load_score(
        _metrics(gpu_temp_c=85.0), _fps(cam1=27.0),
    )
    assert abs(s_crit - 10.0) < 0.05


def test_recv_crops_component():
    """Incoming offloaded crops (10 crops/s with capacity 10) add 5.0."""
    s_recv, _ = _compute_load_score(
        _metrics(offload_crops_received_per_s=10.0), _fps(cam1=27.0),
    )
    assert abs(s_recv - 5.0) < 0.05


def test_fps_trend_decline_component():
    """Falling FPS slope over window contributes trend score."""
    _FPS_HISTORY.clear()
    t0 = time.monotonic()
    _FPS_HISTORY.append((t0 - 3.0, 27.0))
    # FPS drops from 27 to 21 in 3s (rate = 2.0 FPS/s -> max_decline = 2.0 -> trend_comp = 1.0 -> 10.0)
    s_falling, _ = _compute_load_score(
        _metrics(), _fps(cam1=21.0),
    )
    # fps 21: fps_comp = 0.57 + 0.08 * (1/3) = 0.5967 -> 50 * 0.5967 = 29.83
    # trend = 10.0 -> composite ≈ 39.83
    assert s_falling > 35.0


def test_breakdown_full_dictionary():
    """_compute_load_score_breakdown exposes all individual signals."""
    feat = {"cam1": {"n_track": 15, "n_plate": 15}}
    br = _compute_load_score_breakdown(
        _metrics(gpu_temp_c=77.5, offload_crops_received_per_s=5.0),
        _fps(cam1=22.0),
        feature_stats=feat,
    )
    assert "fps_score" in br
    assert "workload_bonus" in br
    assert "thermal_bonus" in br
    assert "recv_bonus" in br
    assert "trend_bonus" in br
    assert "composite_score" in br
    assert "load_score" in br

    assert abs(br["fps_score"] - 28.5) < 0.05
    assert abs(br["workload_bonus"] - 12.5) < 0.05  # 30/60 * 25
    assert abs(br["thermal_bonus"] - 5.0) < 0.05    # (77.5-70)/15 * 10
    assert abs(br["recv_bonus"] - 2.5) < 0.05       # 5/10 * 5
