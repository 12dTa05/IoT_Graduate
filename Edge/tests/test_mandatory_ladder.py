import pytest
import time
from pathlib import Path
from unittest.mock import MagicMock

# Add Edge directory to path
edge_dir = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(edge_dir))

from speedflow_python.peer_orchestrator import PeerOrchestrator, PeerState


def _create_orch(cfg=None):
    default_cfg = {
        "heartbeat_timeout_s": 5.0,
        "overload_threshold": 55.0,
        "overload_duration_s": 3.0,
        "overload_warmup_s": 10.0,
        "stream_pressure_threshold": 0.30,
        "ladder_l2_hold_s": 8.0,
        "cooldown_s": 6.0,
        "lpr_offload_cooldown_s": 6.0,
        "workload_saturation_point": 10.0,
        "node_camera_map": {
            "jetson_A": ["cam_01", "cam_02"],
            "jetson_B": ["cam_03", "cam_04"],
            "jetson_C": ["cam_05", "cam_06"],
        },
    }
    if cfg:
        default_cfg.update(cfg)
    orch = PeerOrchestrator(
        node_id="jetson_A",
        cfg=default_cfg,
        camera_manager=None,
    )
    return orch


def _setup_overload_state(orch, now):
    """Populate self state and timing gates so _check_self_overload is eligible to escalate."""
    orch._self_state.last_seen = now
    orch._self_state.load_score = 65.0
    orch._self_state.gpu_percent = 80.0
    orch._self_state.cpu_percent = 70.0
    orch._self_state.ram_percent = 60.0
    orch._self_state.avg_fps = 25.0
    orch._self_state.overload_since = now - 5.0  # sustained > overload_duration_s (3.0s)
    orch._self_first_valid_fps_at = now - 20.0  # past overload_warmup_s (10.0s)
    orch._transition_settle_until = 0.0
    orch._self_state.active_cameras = ["cam_01", "cam_02"]
    orch._self_state.held_cameras = ["cam_01", "cam_02"]
    orch._self_state.camera_workload = {"cam_01": 15.0, "cam_02": 10.0}


def test_l0_to_l2_first():
    """Node overloaded with peer available escalates to L2 first (offload_level 3), does not trigger L1 RFO."""
    orch = _create_orch()
    now = time.time()
    _setup_overload_state(orch, now)

    # Peer B is healthy and capable (active streaming with valid positive FPS)
    peer_b = PeerState(node_id="jetson_B")
    peer_b.last_seen = now
    peer_b.load_score = 30.0
    peer_b.held_cameras = ["cam_03"]
    peer_b.streaming_cameras = ["cam_03"]
    peer_b.active_cameras = ["cam_03"]
    peer_b.fps_per_camera = {"cam_03": 30.0}
    peer_b.offload_queue_full = False
    orch._peers = {"jetson_B": peer_b}

    orch._trigger_rfo = MagicMock()

    # Execute overload check
    orch._check_self_overload()

    # Must escalate to L2 on candidate camera (heaviest: cam_01)
    assert orch.get_offload_level("cam_01") == 3
    assert orch._ladder_l2_camera == "cam_01"
    assert orch._ladder_l2_since == pytest.approx(now, abs=1.0)

    # Must NOT trigger L1 RFO
    orch._trigger_rfo.assert_not_called()


def test_l2_hold_defers_l1():
    """While within ladder_l2_hold_s, subsequent calls do not trigger L1 RFO."""
    orch = _create_orch()
    now = time.time()
    _setup_overload_state(orch, now)

    peer_b = PeerState(node_id="jetson_B")
    peer_b.last_seen = now
    peer_b.load_score = 30.0
    peer_b.held_cameras = ["cam_03"]
    peer_b.streaming_cameras = ["cam_03"]
    peer_b.fps_per_camera = {"cam_03": 30.0}
    orch._peers = {"jetson_B": peer_b}

    # L2 already activated 4 seconds ago (hold_s = 8.0)
    orch.set_offload_level("cam_01", 3, "jetson_B")
    orch._ladder_l2_since = now - 4.0
    orch._ladder_l2_camera = "cam_01"

    orch._trigger_rfo = MagicMock()

    orch._check_self_overload()

    # Still in hold window; no L1 RFO
    orch._trigger_rfo.assert_not_called()


def test_l2_to_l1_after_hold():
    """After ladder_l2_hold_s has elapsed and node is still overloaded, L1 RFO is triggered."""
    orch = _create_orch()
    now = time.time()
    _setup_overload_state(orch, now)
    orch._self_state.overload_since = now - 15.0

    peer_b = PeerState(node_id="jetson_B")
    peer_b.last_seen = now
    peer_b.load_score = 30.0
    peer_b.held_cameras = ["cam_03"]
    peer_b.streaming_cameras = ["cam_03"]
    peer_b.fps_per_camera = {"cam_03": 30.0}
    orch._peers = {"jetson_B": peer_b}

    # L2 activated 10 seconds ago (hold_s = 8.0, expired)
    orch.set_offload_level("cam_01", 3, "jetson_B")
    orch._ladder_l2_since = now - 10.0
    orch._ladder_l2_camera = "cam_01"

    orch._trigger_rfo = MagicMock()

    orch._check_self_overload()

    # Hold expired -> L1 RFO triggered and L2 ladder camera cleared
    orch._trigger_rfo.assert_called_once()
    assert orch.get_offload_level("cam_01") == 0
    assert orch._ladder_l2_camera is None


def test_fast_escalate_no_peer():
    """If no peer available, _activate_ladder_l2 returns None and fast-escalates to L1."""
    orch = _create_orch()
    now = time.time()
    _setup_overload_state(orch, now)

    # No peers known
    orch._peers = {}

    orch._trigger_rfo = MagicMock()

    orch._check_self_overload()

    # L2 was unavailable -> fast-escalated to L1
    assert orch._ladder_l2_camera is None
    assert orch._ladder_l2_since == pytest.approx(now, abs=1.0)
    orch._trigger_rfo.assert_called_once()


def test_l1_clears_l2_same_camera():
    """If camera being L1 migrated was in L2, its offload_level is reset to 0 before RFO."""
    orch = _create_orch()
    now = time.time()

    orch._self_state.active_cameras = ["cam_01", "cam_02"]
    orch._self_state.held_cameras = ["cam_01", "cam_02"]
    orch._self_state.camera_configs = {"cam_01": {}, "cam_02": {}}
    orch._self_state.camera_workload = {"cam_01": 5.0, "cam_02": 15.0}  # cam_01 lighter -> picked for L1
    orch._self_state.load_score = 65.0

    # cam_01 is currently at L2 plate-crop
    orch.set_offload_level("cam_01", 3, "jetson_B")
    assert orch.get_offload_level("cam_01") == 3

    orch._trigger_rfo = MagicMock()

    orch._trigger_level1_if_due(orch._self_state, now, orch._cfg)

    # L2 level on cam_01 must be reset to 0 before RFO
    assert orch.get_offload_level("cam_01") == 0
    orch._trigger_rfo.assert_called_once_with("cam_01", relaxation_tier=0)


def test_ladder_reset_on_recovery():
    """When load recovers (< overload_threshold), ladder state (_ladder_l2_since and _ladder_l2_camera) resets to None."""
    orch = _create_orch()
    now = time.time()

    orch._self_state.active_cameras = ["cam_01", "cam_02"]
    orch._self_state.held_cameras = ["cam_01", "cam_02"]
    orch._ladder_l2_since = now - 5.0
    orch._ladder_l2_camera = "cam_01"
    orch._self_state.overload_since = now - 10.0

    # Self-state update reports load below threshold
    status_data = {
        "node_id": "jetson_A",
        "load_score": 40.0,  # Healthy (< 55.0)
        "active_cameras": ["cam_01", "cam_02"],
        "held_cameras": ["cam_01", "cam_02"],
    }

    orch.update_self_state(status_data)

    assert orch._self_state.overload_since is None
    assert orch._ladder_l2_since is None
    assert orch._ladder_l2_camera is None


def test_never_offload_rescued():
    """Rescued cameras are rejected by _activate_ladder_l2."""
    orch = _create_orch()
    now = time.time()

    orch._self_state.active_cameras = ["cam_03", "cam_01"]
    orch._self_state.held_cameras = ["cam_03", "cam_01"]
    orch._self_state.camera_configs = {"cam_03": {}, "cam_01": {}}
    orch._self_state.camera_workload = {"cam_03": 20.0, "cam_01": 5.0}
    orch._rescued_cameras["cam_03"] = "jetson_B"

    peer_b = PeerState(node_id="jetson_B")
    peer_b.last_seen = now
    peer_b.load_score = 30.0
    peer_b.held_cameras = ["cam_04"]
    peer_b.streaming_cameras = ["cam_04"]
    peer_b.fps_per_camera = {"cam_04": 30.0}
    orch._peers = {"jetson_B": peer_b}

    # cam_03 is heaviest but rescued, so _activate_ladder_l2 must reject it
    l2_cam = orch._activate_ladder_l2(now, orch._cfg)

    # Rescued camera rejected -> returns None
    assert l2_cam is None
    assert orch.get_offload_level("cam_03") == 0


def test_one_local_camera_invariant():
    """When only 1 local camera is held, it is not offloaded to L2."""
    orch = _create_orch()
    now = time.time()

    orch._self_state.active_cameras = ["cam_01"]
    orch._self_state.held_cameras = ["cam_01"]
    orch._self_state.camera_configs = {"cam_01": {}}
    orch._self_state.camera_workload = {"cam_01": 20.0}

    peer_b = PeerState(node_id="jetson_B")
    peer_b.last_seen = now
    peer_b.load_score = 30.0
    peer_b.held_cameras = ["cam_03"]
    peer_b.streaming_cameras = ["cam_03"]
    peer_b.fps_per_camera = {"cam_03": 30.0}
    orch._peers = {"jetson_B": peer_b}

    l2_cam = orch._activate_ladder_l2(now, orch._cfg)
    assert l2_cam is None
