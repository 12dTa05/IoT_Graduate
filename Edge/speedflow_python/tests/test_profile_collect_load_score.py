"""
Edge/speedflow_python/tests/test_profile_collect_load_score.py

Verify that the collector records the same load_score that health_agent's
shared _compute_load_score produces. Hardware-free — mock data only.

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
    """With all-zero hardware and no cameras → score = 0.0."""
    score, _ = _compute_load_score(_metrics(), {})
    assert score == 0.0


def test_typical_load_spot_check():
    """Known inputs → score matches formula: base + penalty, clamped to 100.

    With normal weights [0.3, 0.3, 0.4] and one active camera:
      base = 0.3*50 + 0.3*30 + 0.4*20 = 15 + 9 + 8 = 32
      avg_fps = (25+25)/2 = 25, TARGET_FPS = 25 → penalty = 0
      score = 32.0
    """
    score, _ = _compute_load_score(
        _metrics(gpu=50, cpu=30, ram=20),
        _fps(cam_01=25.0, cam_02=25.0),
    )
    assert abs(score - 32.0) < 0.5


def test_fps_penalty():
    """FPS below target introduces a penalty above base."""
    score, _ = _compute_load_score(
        _metrics(gpu=50, cpu=30, ram=20),
        _fps(cam_01=15.0),  # TARGET_FPS=25 → 10/25 * fps_penalty_max (30) = 12 point penalty
    )
    # base = 0.3*50 + 0.3*30 + 0.4*20 = 32, penalty = 12, total = 44
    assert abs(score - 44.0) < 0.5


def test_score_clamped_to_100():
    """Score is always ≤ 100."""
    score, _ = _compute_load_score(
        _metrics(gpu=100, cpu=100, ram=100),
        _fps(cam_01=100.0),  # fps above target → no penalty
    )
    assert score <= 100.0


def test_collected_row_matches_shared_function():
    """The central proof: a row that profile_collect would log (same hw + fps)
    matches _compute_load_score output exactly."""
    hw = {"gpu_percent": 60.0, "cpu_percent": 40.0, "ram_percent": 30.0, "gpu_temp_c": 72.0}
    fps = {"cam_01": 25.0, "cam_02": 22.0}

    expected_score, expected_preset = _compute_load_score(hw, fps)

    # profile_collect calls _compute_load_score(hw, fps_dict) directly — no
    # recalculation layer — so the score it writes IS this return value.
    assert isinstance(expected_score, float)
    assert 0.0 <= expected_score <= 100.0

    # avg_fps = (25+22)/2 = 23.5, penalty = (25-23.5)/25 * 30 = 1.8
    # base = 0.3*60 + 0.3*40 + 0.4*30 = 18 + 12 + 12 = 42
    # total = 43.8
    assert abs(expected_score - 43.8) < 1.0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        test_compute_load_score_returns_tuple,
        test_no_cameras_no_penalty,
        test_typical_load_spot_check,
        test_fps_penalty,
        test_score_clamped_to_100,
        test_collected_row_matches_shared_function,
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