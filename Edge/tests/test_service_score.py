import pytest
import sys
from pathlib import Path

# Add Edge directory to path
edge_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(edge_dir))

from health_agent import (
    _compute_load_score,
    _compute_load_score_breakdown,
    _update_service_ema_state,
)
import speedflow_python.settings as settings


def test_service_score_perfect_completion():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # 100% completion (c = 1.0 >= 0.95 target) -> score 0.0
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=1.0,
    )
    assert mode == "service_primary"
    assert score == 0.0


def test_service_score_moderate_completion():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # c = 0.90 -> (0.95 - 0.90) / (0.95 - 0.50) * 100 = 0.05 / 0.45 * 100 = 11.1
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.90,
    )
    assert mode == "service_primary"
    assert 11.0 <= score <= 12.0


def test_service_score_l3_level_trigger():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # c = 0.70 -> (0.95 - 0.70) / 0.45 * 100 = 0.25 / 0.45 * 100 = 55.5 (Crosses L3 >= 55.0)
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.70,
    )
    assert mode == "service_primary"
    assert 55.0 <= score <= 56.0


def test_service_score_l1_critical_trigger():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # c = 0.60 -> (0.95 - 0.60) / 0.45 * 100 = 0.35 / 0.45 * 100 = 77.8 (Crosses L1 >= 72.0)
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.60,
    )
    assert mode == "service_primary"
    assert 77.0 <= score <= 78.5


def test_service_score_fps_emergency_floor():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    # FPS collapses to 10.0 (< 12.0 fps_emergency) even with high completion
    fps_stats = {"cam_01": 10.0}
    
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.98,
    )
    assert mode == "service_primary"
    assert score >= 80.0


def test_service_score_hardware_fuse_floor():
    # CPU saturated >= 90% and FPS < 25.0
    metrics = {"cpu_percent": 95.0, "ram_percent": 40.0}
    fps_stats = {"cam_01": 20.0}
    
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.98,
    )
    assert mode == "service_primary"
    assert score >= 80.0


def test_service_score_breakdown():
    metrics = {"cpu_percent": 25.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0}
    
    bd = _compute_load_score_breakdown(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.70,
        service_delta_fin=10,
        service_delta_miss=2,
        service_pending_tracks=3,
        service_idle_s=1.5,
        service_cold_start=False,
    )
    assert bd["mode"] == "service"
    assert bd["qos_state"] == "degraded"
    assert 55.0 <= bd["load_score"] <= 56.0
    assert bd["service_delta_fin"] == 10
    assert bd["service_delta_miss"] == 2
    assert bd["service_pending_tracks"] == 3
    assert bd["service_idle_s"] == 1.5
    assert bd["service_cold_start"] is False


def test_update_service_ema_pure_helper():
    # 1. First sample initializes state with historical baseline seed
    s0 = {}
    stats1 = {"plates_finalized": 100, "tracks_missed": 50, "tracks_born": 150, "tracks_expired": 150}
    s1 = _update_service_ema_state(stats1, s0, now_mono=100.0, s_alpha=0.30, s_stale=5.0)
    assert s1["service_ema"] == pytest.approx(100.0 / 150.0, 0.01)  # Seeded from historical 100/(100+50) = 0.667
    assert s1["prev_fin"] == 100
    assert s1["prev_miss"] == 50
    assert s1["pending_tracks"] == 0
    assert s1["cold_start"] is True

    # First sample with empty history starts at 1.0 (healthy)
    s_empty = _update_service_ema_state({"plates_finalized": 0, "tracks_missed": 0}, {}, now_mono=100.0)
    assert s_empty["service_ema"] == 1.0
    assert s_empty["cold_start"] is True

    # 2. Delta with high misses: 0 fin, 10 miss -> inst_c = 0.0 -> EMA drops from 0.667
    stats2 = {"plates_finalized": 100, "tracks_missed": 60, "tracks_born": 160, "tracks_expired": 160}
    s2 = _update_service_ema_state(stats2, s1, now_mono=101.0, s_alpha=0.30, s_stale=5.0)
    assert s2["delta_fin"] == 0
    assert s2["delta_miss"] == 10
    assert s2["service_ema"] == pytest.approx(0.30 * 0.0 + 0.70 * (100.0 / 150.0), 0.01)  # 0.467
    assert s2["last_busy_ts"] == 101.0

    # 3. Pending tracks in flight (born=170, exp=160 -> pending=10):
    # Even after time >= s_stale (110.0 - 101.0 = 9s >= 5s), NO recovery because pending_tracks > 0
    stats3 = {"plates_finalized": 100, "tracks_missed": 60, "tracks_born": 170, "tracks_expired": 160}
    s3 = _update_service_ema_state(stats3, s2, now_mono=110.0, s_alpha=0.30, s_stale=5.0)
    assert s3["pending_tracks"] == 10
    assert s3["service_ema"] == pytest.approx(0.467, 0.01)  # Stays unchanged because tracks are in flight
    assert s3["last_busy_ts"] == 110.0

    # 4. Pending tracks just cleared at t=111.0 (born=170, exp=170 -> pending=0).
    # Since last_busy_ts was 110.0, idle_time is 1.0s (< s_stale 5.0s) -> NO recovery yet!
    stats4_just_cleared = {"plates_finalized": 100, "tracks_missed": 60, "tracks_born": 170, "tracks_expired": 170}
    s4 = _update_service_ema_state(stats4_just_cleared, s3, now_mono=111.0, s_alpha=0.30, s_stale=5.0)
    assert s4["pending_tracks"] == 0
    assert s4["service_ema"] == pytest.approx(0.467, 0.01)
    assert s4["idle_s"] == pytest.approx(1.0, 0.1)

    # 5. Idle time >= s_stale after pending cleared (t=116.0 -> idle_s = 6.0s >= 5.0s): Recover smoothly
    stats5 = {"plates_finalized": 100, "tracks_missed": 60, "tracks_born": 170, "tracks_expired": 170}
    s5 = _update_service_ema_state(stats5, s4, now_mono=116.0, s_alpha=0.30, s_stale=5.0)
    assert s5["service_ema"] > 0.467
    assert s5["pending_tracks"] == 0
    assert s5["idle_s"] == pytest.approx(6.0, 0.1)


def test_service_score_heavy_workload_triggers_l1_even_with_good_completion():
    """
    ponytail: test unified service score where heavy workload (high vehicle count)
    pushes score into L1 critical territory (>= 72.0) even when completion is 100%.
    Addresses the 'xe rất nhiều nhưng load_score rất thấp' paradox.
    """
    metrics = {"cpu_percent": 30.0, "ram_percent": 40.0}
    # 2 cameras, total 14 vehicles/tracks (eff_wl = 14.0 >= w_high=10.0), FPS chớm tụt xuống 14.0 (< fps_critical=15.0)
    fps_stats = {"cam_01": 14.0, "cam_02": 14.0}
    
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        workload_ema=14.0,
        fps_ema=14.0,
        service_ema=1.0,  # 100% completion
    )
    assert mode == "service_primary"
    # Heavy workload + low fps must push score into L1 range (>= 72.0) despite 100% completion
    assert score >= 72.0


def test_service_score_moderate_workload_with_healthy_fps_stays_calm():
    """
    Moderate vehicle count with 30 FPS and 100% completion stays healthy (< 30.0).
    """
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 30.0, "cam_02": 30.0}
    
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        workload_ema=4.0,  # < w_low=6.0
        fps_ema=30.0,
        service_ema=1.0,
    )
    assert mode == "service_primary"
    assert score < 30.0


