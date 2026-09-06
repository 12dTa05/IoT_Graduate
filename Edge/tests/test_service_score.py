import pytest
import sys
from pathlib import Path

# Add Edge directory to path
edge_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(edge_dir))

# Ensure settings and health_agent use service mode for these tests
import speedflow_python.settings as settings
settings.EDGE_LOAD_SCORE_MODE = "service"
import health_agent
health_agent.EDGE_LOAD_SCORE_MODE = "service"

from health_agent import (
    _compute_load_score,
    _compute_load_score_breakdown,
    _update_service_ema_state,
)


def test_service_score_perfect_completion():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # Perfect completion (service_ema=1.0) still carries stream-concurrency
    # pressure: 2 active streams -> rho_s ~0.133 -> ~11.8 (asymptotic kernel).
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=1.0,
    )
    assert mode == "service_primary"
    assert 11.0 <= score <= 12.5


def test_service_score_moderate_completion():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # service_ema=0.90 -> rho_v=(1-0.9)/0.9=0.111 ; rho_s~0.133 -> ~19.6
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.90,
    )
    assert mode == "service_primary"
    assert 19.0 <= score <= 20.5


def test_service_score_l3_level_trigger():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # Asymptotic kernel: service_ema=0.70 -> bounded rho_v=0.556 (deficit 0.25/0.45);
    # rho_s=0.133 -> score ~40.8 (degraded, not critical).
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats={"cam_01": 20.0, "cam_02": 20.0},
        service_ema=0.70,
    )
    assert mode == "service_primary"
    assert 39.5 <= score <= 42.0


def test_service_score_l3_veto_with_healthy_fps():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0, "gpu_percent": 35.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.70,
    )
    assert mode == "service_primary"
    # Asymptotic kernel: healthy GPU, service_ema=0.70 -> ~40.8 (no CPU fuse floor)
    assert 39.5 <= score <= 42.0


def test_service_score_l1_critical_trigger():
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}
    
    # service_ema=0.60 -> bounded rho_v=0.778 (deficit 0.35/0.45) ; rho_s=0.133 -> ~47.7
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.60,
    )
    assert mode == "service_primary"
    assert 46.5 <= score <= 49.0


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
    # CPU saturated >= 90%: rho_r = (0.95-0.60)/(1.05-0.95) = 3.5 -> ~77.9.
    # Asymptotic kernel no longer applies a hard 80 floor for CPU>=90 (FPS fuse only).
    metrics = {"cpu_percent": 95.0, "ram_percent": 40.0}
    fps_stats = {"cam_01": 20.0}
    
    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.98,
    )
    assert mode == "service_primary"
    assert score >= 70.0


def test_service_score_breakdown():
    metrics = {"cpu_percent": 25.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 20.0}
    
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
    assert bd["qos_state"] == "moderate"
    assert 30.0 <= bd["load_score"] <= 40.0
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
    raises the score well above the healthy baseline even when completion is 100%.
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
    # Heavy workload + low fps must push the score clearly above the healthy
    # baseline (perfect completion with light load ~11.8) despite 100% completion.
    assert score >= 55.0


def test_service_score_moderate_workload_with_healthy_fps_stays_calm():
    """
    Moderate vehicle count with 30 FPS and 100% completion stays calm (well below
    the degraded/critical bands), though stream concurrency places it above the
    bare healthy floor.
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
    assert score < 45.0


def test_service_ema_alpha_and_stale_validation():
    s0 = {"service_ema": 0.5, "prev_fin": 10, "prev_miss": 10, "last_busy_ts": 100.0, "last_update_ts": 100.0}
    # Non-finite alpha (NaN, inf) or <=0 / >1 must safely fall back to 0.30 default
    s_nan = _update_service_ema_state(
        {"plates_finalized": 20, "tracks_missed": 10},
        s0,
        now_mono=101.0,
        s_alpha=float('nan'),
        s_stale=30.0,
    )
    # Expected fallback alpha 0.30: 0.30 * (10 / 10) + 0.70 * 0.5 = 0.30 + 0.35 = 0.65
    assert s_nan["service_ema"] == pytest.approx(0.65, 0.01)

    s_zero = _update_service_ema_state(
        {"plates_finalized": 20, "tracks_missed": 10},
        s0,
        now_mono=101.0,
        s_alpha=0.0,
        s_stale=-5.0,
    )
    assert s_zero["service_ema"] == pytest.approx(0.65, 0.01)


def test_service_ema_pending_tracks_blocks_recovery_and_elapsed_time_step():
    s_pending = {"service_ema": 0.4, "prev_fin": 20, "prev_miss": 10, "last_busy_ts": 100.0, "last_update_ts": 100.0}
    stats_pending = {"plates_finalized": 20, "tracks_missed": 10, "tracks_born": 50, "tracks_expired": 40}
    # pending_tracks = 10 > 0 -> blocks recovery even after 100s idle
    s_res = _update_service_ema_state(stats_pending, s_pending, now_mono=200.0, s_alpha=0.3, s_stale=10.0)
    assert s_res["pending_tracks"] == 10
    assert s_res["service_ema"] == 0.4
    assert s_res["last_busy_ts"] == 200.0

    # Once cleared (pending=0) and idle >= stale (10s), recovery occurs by 0.10 * elapsed_since_update
    stats_idle = {"plates_finalized": 20, "tracks_missed": 10, "tracks_born": 50, "tracks_expired": 50}
    # First step at 201.0: idle_s = 1.0 < 10.0 -> no recovery yet
    s_idle1 = _update_service_ema_state(stats_idle, s_res, now_mono=201.0, s_alpha=0.3, s_stale=10.0)
    assert s_idle1["service_ema"] == 0.4
    assert s_idle1["idle_s"] == 1.0

    # Step at 215.0: idle_s = 15.0 >= 10.0, elapsed = 15.0s -> recovers by min(0.6, 0.10 * 15.0) = 0.6 -> reaches 1.0
    s_idle2 = _update_service_ema_state(stats_idle, s_idle1, now_mono=215.0, s_alpha=0.3, s_stale=10.0)
    assert s_idle2["service_ema"] == pytest.approx(1.0, 0.01)


def test_service_score_breakdown_comprehensive_fields():
    """
    ponytail: ensure all required breakdown fields for diagnosis are present and exact types.
    """
    metrics = {"cpu_percent": 15.0, "ram_percent": 25.0}
    fps_stats = {"cam_01": 25.0, "cam_02": 25.0}

    bd = _compute_load_score_breakdown(
        metrics=metrics,
        fps_stats=fps_stats,
        service_ema=0.85,
        service_delta_fin=12,
        service_delta_miss=3,
        service_pending_tracks=4,
        service_idle_s=0.0,
        service_cold_start=False,
    )

    expected_keys = [
        "mode",
        "service_c_ema",
        "service_score",
        "workload_pressure",
        "fps_score",
        "hw_floor",
        "composite_score",
        "load_score",
        "qos_state",
        "workload_ema",
        "fps_ema",
        "raw_workload",
        "raw_fps",
        "service_delta_fin",
        "service_delta_miss",
        "service_pending_tracks",
        "service_idle_s",
        "service_cold_start",
    ]
    for k in expected_keys:
        assert k in bd, f"Missing key in load_score_breakdown: {k}"

    assert bd["service_delta_fin"] == 12
    assert bd["service_delta_miss"] == 3
    assert bd["service_pending_tracks"] == 4
    assert bd["service_c_ema"] == 0.85
    assert bd["fps_score"] == 0.0
    assert bd["hw_floor"] == 0.0
    assert bd["qos_state"] == "healthy"


def test_low_service_alone_cannot_trigger_overload():
    """
    Regression for the rho_v false-overload bug: a total service collapse
    (service_ema=0.228) on an otherwise IDLE 2-camera node (low CPU/RAM,
    no workload demand, healthy FPS) must NOT reach the overload threshold
    (>= 55.0). The bounded deficit caps rho_v at rho_v_max=1.0, so even
    worst-case completion collapse tops out around ~53 (< 55).
    """
    metrics = {"cpu_percent": 20.0, "ram_percent": 30.0}
    fps_stats = {"cam_01": 27.0, "cam_02": 27.0}

    score, mode = _compute_load_score(
        metrics=metrics,
        fps_stats=fps_stats,
        workload_ema=0.0,
        fps_ema=27.0,
        service_ema=0.228,  # ~total service collapse
    )
    assert mode == "service_primary"
    assert score < 55.0
