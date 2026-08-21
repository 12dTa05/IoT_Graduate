"""
Edge/speedflow_python/tests/test_stabilization_fixes.py

Unit tests for offload stabilization fixes:
1. Reclaim hysteresis & local stream capacity guard
2. Per-camera migration bounce dampening (bounce_max / bounce_window_s)
3. Target peer migration timeout consecutive degradation and reset
4. Escalating timeout penalty calculation
"""

import importlib.util
from pathlib import Path
import sys
import time
import types
from unittest.mock import MagicMock
import pytest

_EDGE_DIR = Path(__file__).resolve().parents[2]

# Ensure speedflow_python package is loaded
pkg = sys.modules.get("speedflow_python")
if pkg is None:
    pkg = types.ModuleType("speedflow_python")
    pkg.__path__ = [str(_EDGE_DIR / "speedflow_python")]
    sys.modules["speedflow_python"] = pkg

def _ensure_settings():
    if "speedflow_python.settings" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "speedflow_python.settings", _EDGE_DIR / "speedflow_python" / "settings.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["speedflow_python.settings"] = mod
        spec.loader.exec_module(mod)

def _import_peer_orch():
    _ensure_settings()
    spec = importlib.util.spec_from_file_location(
        "speedflow_python.peer_orchestrator", _EDGE_DIR / "speedflow_python" / "peer_orchestrator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["speedflow_python.peer_orchestrator"] = mod
    spec.loader.exec_module(mod)
    return mod.PeerOrchestrator, mod.PeerState

PeerOrchestrator, PeerState = _import_peer_orch()


def _make_orchestrator(cfg=None):
    custom_cfg = {
        "overload_threshold": 60.0,
        "reclaim_margin": 25.0,
        "reclaim_stable_s": 30.0,
        "cooldown_s": 15.0,
        "heartbeat_timeout_s": 5.0,
        "migration_timeout_s": 12.0,
        "bounce_max": 3,
        "bounce_window_s": 300.0,
        "zombie_timeout_count": 3,
        "eps_streams_max": 4,
    }
    if cfg:
        custom_cfg.update(cfg)
    orch = PeerOrchestrator(
        node_id="node_local",
        cfg=custom_cfg,
        camera_manager=None,
    )
    return orch


def test_reclaim_capacity_guard():
    """Verify reclaim is blocked when active_cameras + 1 reaches or exceeds local stream capacity."""
    orch = _make_orchestrator({"eps_streams_max": 4})
    orch._migrated_out["cam_01"] = "node_peer"

    peer = PeerState(node_id="node_peer")
    peer.active_cameras = ["cam_01"]
    peer.last_seen = time.time()
    orch._peers["node_peer"] = peer

    # Setup self state: load is low, but already has 3 active cameras (capacity = 4)
    # 3 + 1 >= 4 -> blocked from reaching configured ceiling
    orch._self_state.load_score = 30.0
    orch._self_state.overload_since = None
    orch._self_state.active_cameras = ["cam_02", "cam_03", "cam_04"]
    orch._self_state.max_streams = 4
    orch._reclaim_eligible_since = time.time() - 35.0  # > reclaim_stable_s (30s)

    orch._pubs["control"] = MagicMock()

    orch._check_reclaim()
    # Control pub should NOT have been called because 3 + 1 >= 4
    orch._pubs["control"].put.assert_not_called()

    # Now decrease active cameras to 2 (2 + 1 < 4)
    orch._self_state.active_cameras = ["cam_02", "cam_03"]
    orch._get_camera_config = MagicMock(return_value={"camera_id": "cam_01"})
    orch._wait_and_remove_reclaim = MagicMock()

    orch._check_reclaim()
    # Control pub SHOULD be called for reclaim
    orch._pubs["control"].put.assert_called_once()


def test_reclaim_eligible_since_cleared_on_load_spike():
    """Verify _reclaim_eligible_since is reset whenever load >= reclaim_threshold."""
    orch = _make_orchestrator({"overload_threshold": 60.0, "reclaim_margin": 25.0})
    orch._migrated_out["cam_01"] = "node_peer"

    peer = PeerState(node_id="node_peer")
    peer.active_cameras = ["cam_01"]
    peer.last_seen = time.time()
    orch._peers["node_peer"] = peer

    orch._self_state.active_cameras = ["cam_02"]
    orch._self_state.max_streams = 4
    orch._self_state.overload_since = None

    # Load is above reclaim_threshold (60 - 25 = 35.0) -> e.g. 40.0
    orch._self_state.load_score = 40.0
    orch._reclaim_eligible_since = time.time() - 40.0

    orch._check_reclaim()
    assert orch._reclaim_eligible_since is None


def test_bounce_dampening():
    """Verify camera is excluded from L1 candidates after reaching bounce_max within bounce_window_s."""
    orch = _make_orchestrator({"bounce_max": 3, "bounce_window_s": 300.0})
    orch._owned_camera_ids = {"cam_01", "cam_02", "cam_03"}

    state = PeerState(node_id="node_local")
    state.active_cameras = ["cam_01", "cam_02", "cam_03"]
    state.camera_workload = {"cam_01": 10.0, "cam_02": 20.0, "cam_03": 30.0}

    # Before reaching bounce_max: cam_01 (lightest workload 10.0) is picked
    orch._cam_migration_history["cam_01"] = [time.time() - 100.0, time.time() - 50.0]
    picked = orch._pick_camera_to_offload(state, level=1)
    assert picked == "cam_01"

    # Add 3rd migration to history -> reaches bounce_max (3)
    orch._cam_migration_history["cam_01"].append(time.time())
    picked = orch._pick_camera_to_offload(state, level=1)
    # cam_01 is excluded; cam_02 (next lightest workload 20.0) should be picked
    assert picked == "cam_02"

    # Expire the old timestamps outside the window
    orch._cam_migration_history["cam_01"] = [
        time.time() - 400.0,
        time.time() - 350.0,
        time.time() - 310.0,
    ]
    picked = orch._pick_camera_to_offload(state, level=1)
    # Timestamps expired -> cam_01 eligible again
    assert picked == "cam_01"


def test_zombie_peer_degradation_and_reset():
    """Verify peer with consecutive timeouts >= zombie_timeout_count is excluded and reset on success."""
    orch = _make_orchestrator({"zombie_timeout_count": 3})

    peer1 = PeerState(node_id="peer_1")
    peer1.load_score = 20.0
    peer1.last_seen = time.time()
    peer1.max_streams = 4
    peer1.active_cameras = []

    peer2 = PeerState(node_id="peer_2")
    peer2.load_score = 30.0
    peer2.last_seen = time.time()
    peer2.max_streams = 4
    peer2.active_cameras = []

    orch._peers = {"peer_1": peer1, "peer_2": peer2}

    # Normal selection picks peer_1 (load 20 < 30)
    best = orch._pick_best_peer(for_offload_level=1)
    assert best == "peer_1"

    # Record 3 timeouts for peer_1 -> reaches zombie_timeout_count
    orch._peer_consecutive_timeouts["peer_1"] = 3
    best = orch._pick_best_peer(for_offload_level=1)
    # peer_1 is excluded; peer_2 selected
    assert best == "peer_2"

    # Simulate successful authenticated ACK from peer_1
    orch._pending_winner["cam_test"] = "peer_1"
    orch._on_vote_ack({"camera_id": "cam_test", "node_id": "peer_1", "event": "PLAYING"})

    # Consecutive timeouts for peer_1 must be cleared
    assert "peer_1" not in orch._peer_consecutive_timeouts
    best = orch._pick_best_peer(for_offload_level=1)
    assert best == "peer_1"


def test_escalating_timeout_penalty():
    """Verify timeout penalty escalates with consecutive timeouts and stays bounded."""
    orch = _make_orchestrator({"cooldown_s": 10.0})
    orch._session = MagicMock()
    orch._migration_log = MagicMock()

    peer = PeerState(node_id="peer_timeout")
    peer.last_seen = time.time()
    orch._peers["peer_timeout"] = peer

    # 1st timeout: multiplier = min(2^0, 8) = 1 -> penalty = max(20, 10*1) = 20s
    orch._cfg["migration_timeout_s"] = 0.01
    orch._pending_winner["cam_01"] = "peer_timeout"
    orch._peer_inflight["peer_timeout"] = 1
    t0 = time.time()
    orch._wait_and_remove("cam_01", "peer_timeout")

    assert orch._peer_consecutive_timeouts["peer_timeout"] == 1
    assert peer.penalty_until >= t0 + 19.0
    assert peer.penalty_until <= t0 + 22.0

    # 2nd timeout: multiplier = min(2^1, 8) = 2 -> penalty = max(20, 10*2) = 20s
    orch._pending_winner["cam_01"] = "peer_timeout"
    orch._peer_inflight["peer_timeout"] = 1
    t1 = time.time()
    orch._wait_and_remove("cam_01", "peer_timeout")
    assert orch._peer_consecutive_timeouts["peer_timeout"] == 2
    assert peer.penalty_until >= t1 + 19.0

    # 3rd timeout: multiplier = min(2^2, 8) = 4 -> penalty = max(20, 10*4) = 40s
    orch._pending_winner["cam_01"] = "peer_timeout"
    orch._peer_inflight["peer_timeout"] = 1
    t2 = time.time()
    orch._wait_and_remove("cam_01", "peer_timeout")
    assert orch._peer_consecutive_timeouts["peer_timeout"] == 3
    assert peer.penalty_until >= t2 + 39.0
    assert peer.penalty_until <= t2 + 42.0

    # 4th timeout: multiplier = min(2^3, 8) = 8 -> penalty = max(20, 10*8) = 80s
    orch._pending_winner["cam_01"] = "peer_timeout"
    orch._peer_inflight["peer_timeout"] = 1
    t3 = time.time()
    orch._wait_and_remove("cam_01", "peer_timeout")
    assert orch._peer_consecutive_timeouts["peer_timeout"] == 4
    assert peer.penalty_until >= t3 + 79.0
    assert peer.penalty_until <= t3 + 82.0


def test_dead_peer_migrated_camera_reclaim():
    """Verify cameras in _migrated_out whose holder dies are re-added locally and preserved until ACK."""
    orch = _make_orchestrator()
    orch._pubs["control"] = MagicMock()
    orch._session = MagicMock()
    orch._get_camera_config = MagicMock(return_value={"camera_id": "cam_owned_01", "uri": "rtsp://..."})

    # cam_owned_01 was migrated out to node_dead
    orch._migrated_out["cam_owned_01"] = "node_dead"

    peer = PeerState(node_id="node_dead")
    peer.active_cameras = ["cam_owned_01", "cam_dead_own"]
    peer.camera_configs = {"cam_dead_own": {"camera_id": "cam_dead_own"}}
    peer.last_seen = time.time() - 15.0  # Silent > timeout + grace (10s)
    orch._peers["node_dead"] = peer

    orch._check_offline_peers()

    # Control pub should have received ADD for cam_owned_01
    orch._pubs["control"].put.assert_called_once()
    assert "cam_owned_01" in orch._reclaim_in_progress
    # Mapping preserved until ACK
    assert orch._migrated_out.get("cam_owned_01") == "node_dead"

    # Simulate ACK from self
    orch._on_vote_ack({"camera_id": "cam_owned_01", "event": "PLAYING", "node_id": "node_local"})
    time.sleep(0.05)
    # After ACK, _migrated_out entry is cleaned up and reclaim completes
    assert "cam_owned_01" not in orch._migrated_out
    assert "cam_owned_01" not in orch._reclaim_in_progress
