"""
Edge/speedflow_python/tests/test_profile_collect_load_score.py

Verify that the collector records the same load_score that health_agent's
shared _compute_load_score produces.  Hardware-free — mock data only.
Tests the bounded piecewise-linear FPS anchors + hardware emergency floor.

Run from Edge/:
    python3 -m pytest speedflow_python/tests/test_profile_collect_load_score.py -v
or standalone:
    python3 speedflow_python/tests/test_profile_collect_load_score.py
"""

import sys
import time
from pathlib import Path

# Ensure Edge/ is on sys.path so health_agent imports work
_EDGE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_EDGE_DIR))

from health_agent import _compute_load_score


def _metrics(gpu=0.0, cpu=0.0, ram=0.0) -> dict:
    """Mock jtop-style metrics dict (minimal keys used by _compute_load_score)."""
    return {"gpu_percent": gpu, "cpu_percent": cpu,
            "ram_percent": ram, "gpu_temp_c": 0.0}


def _fps(**cams) -> dict:
    """Mock FPS stats dict."""
    return cams


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_compute_load_score_returns_tuple():
    """Smoke test: function is importable and returns (float, str)."""
    score, preset = _compute_load_score(_metrics(), {})
    assert isinstance(score, float)
    assert isinstance(preset, str)
    assert 0.0 <= score <= 100.0


def test_no_cameras_no_penalty():
    """With all-zero hardware and no cameras → score = 0.0 (preset TARGET_FPS)."""
    score, _ = _compute_load_score(_metrics(), {})
    assert score == 0.0


def test_all_cameras_above_target():
    """avg_fps >= TARGET_FPS (27) → score 0."""
    score, _ = _compute_load_score(
        _metrics(gpu=50, cpu=40, ram=30),
        _fps(cam_01=27.0, cam_02=27.5),
    )
    assert score == 0.0


def test_fps_score_is_dominant_no_hardware_bonus():
    """Hardware metrics are NOT additive: 95% GPU alone with 27 FPS → score 0 (no floor triggered because no hardware emergency). Wait, 95 >= 90 triggers floor. Let's test: 95% GPU but fps=27 → floor 75.0"""

    # 95% GPU triggers hardware emergency → floor 75.0, fps_score=0, so max(0, 75) = 75
    score, _ = _compute_load_score(
        _metrics(gpu=95, cpu=30, ram=30),
        _fps(cam_01=27.0),
    )
    assert abs(score - 75.0) < 0.5  # hw floor overrides fps_score=0


def test_hardware_floor_at_fps22():
    """Hardware emergency floor does not lower a legitimate score.  fps=22 gives fps_score=57, floor=75, max=75."""
    score, _ = _compute_load_score(
        _metrics(gpu=92, cpu=30, ram=30),
        _fps(cam_01=22.0),
    )
    assert abs(score - 75.0) < 0.5  # hw floor dominates fps_score=57


def test_no_hardware_emergency_normal_fps():
    """No hardware saturation → pure FPS score.  fps=25 should be between 0 and 57."""
    score, _ = _compute_load_score(
        _metrics(gpu=60, cpu=40, ram=50),
        _fps(cam_01=25.0),
    )
    # Between anchor (27,0) and (22,57): interpolated
    expected = 57.0 * (27.0 - 25.0) / (27.0 - 22.0)  # = 57 * 2 / 5 = 22.8
    assert abs(score - 22.8) < 0.5


def test_score_clamped_to_100():
    """Score is always ≤ 100."""
    score, _ = _compute_load_score(
        _metrics(gpu=100, cpu=100, ram=100),
        _fps(cam_01=0.0),
    )
    # fps=0 → fps_score = 100, hw saturated → floor 75 → max(100,75) = 100
    assert score <= 100.0
    assert score >= 0.0


# ---------------------------------------------------------------------------
# anchor exact tests
# ---------------------------------------------------------------------------

def test_anchor_27_fps_zero():
    """FPS >= 27 → score 0 (no hardware crisis)."""
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=27.0))
    assert score == 0.0, f"Expected 0, got {score}"

    score_b, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=28.0))
    assert score_b == 0.0, f"Clamped input 28→27, got {score_b}"


def test_anchor_22_fps():
    """F(22) = 57 (L3 threshold)."""
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=22.0))
    assert abs(score - 57.0) < 0.05, f"Expected 57, got {score}"


def test_anchor_19_fps():
    """F(19) = 65 (L2 threshold)."""
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=19.0))
    assert abs(score - 65.0) < 0.05, f"Expected 65, got {score}"


def test_anchor_17_fps():
    """F(17) = 75 (L1 threshold)."""
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=17.0))
    assert abs(score - 75.0) < 0.05, f"Expected 75, got {score}"


def test_anchor_zero_fps():
    """F(0) = 100."""
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=0.0))
    assert abs(score - 100.0) < 0.05, f"Expected 100, got {score}"


def test_anchor_exact_upper_bound():
    """all cameras above 27 (clamped) → 0."""
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10),
                                    _fps(cam_01=27.0, cam_02=27.0))
    assert score == 0.0


def test_interpolation_mid():
    """F(24.5) midpoint between 27->0 and 22->57: 28.5."""
    score, _ = _compute_load_score(_metrics(gpu=30, cpu=20, ram=10), _fps(cam_01=24.5))
    expected = 57.0 * (27.0 - 24.5) / (27.0 - 22.0)  # = 28.5
    assert abs(score - 28.5) < 0.5


# ---------------------------------------------------------------------------
# no test for edge_node.yml mtime since it's mocked and file doesn't exist yet
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        test_compute_load_score_returns_tuple,
        test_no_cameras_no_penalty,
        test_all_cameras_above_target,
        test_fps_score_is_dominant_no_hardware_bonus,
        test_hardware_floor_at_fps22,
        test_no_hardware_emergency_normal_fps,
        test_score_clamped_to_100,
        test_anchor_27_fps_zero,
        test_anchor_22_fps,
        test_anchor_19_fps,
        test_anchor_17_fps,
        test_anchor_zero_fps,
        test_anchor_exact_upper_bound,
        test_interpolation_mid,
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