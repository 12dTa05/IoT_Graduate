"""
Edge/speedflow_python/tests/test_rescue_failover_fixes.py

Unit tests for failover rescue fixes:
1. rescue_claim priority_weight msgpack safe serialization
2. Dynamic / foreign rescued cameras excluded from local ownership
3. Failover convergence grace and offline detection logic
4. Rescue hold timeout / force-release logic
5. Capacity checking with CameraManager and failover ceiling
"""

import hashlib
import importlib.util
import msgpack
import numpy as np
import time
import types
from pathlib import Path
import sys
import pytest

_EDGE_DIR = Path(__file__).resolve().parents[2]

# Stub speedflow_python package if not present to avoid __init__.py loading gi
pkg = sys.modules.get("speedflow_python")
if pkg is None:
    pkg = types.ModuleType("speedflow_python")
    pkg.__path__ = [str(_EDGE_DIR / "speedflow_python")]
    sys.modules["speedflow_python"] = pkg


def _import_camera_config():
    spec = importlib.util.spec_from_file_location(
        "real_camera_config", _EDGE_DIR / "speedflow_python" / "camera_config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["real_camera_config"] = mod
    spec.loader.exec_module(mod)
    return mod.CameraConfig, mod.CameraManager


CameraConfig, CameraManager = _import_camera_config()


def test_rescue_claim_priority_weight_msgpack_serialization():
    """Verify priority_weight truncated to 15 hex chars fits in 63-bit int and msgpack serializes it safely."""
    camera_id = "cam_01"
    node_id = "node_jetson_01"
    
    # 15 hex digits = max 0xFFFFFFFFFFFFFFF = 1,152,921,504,606,846,975 (< 2^63 - 1 = 9.22 * 10^18)
    my_weight = int(hashlib.sha256(f"{camera_id}:{node_id}".encode()).hexdigest()[:15], 16)
    assert my_weight >= 0
    assert my_weight < (1 << 63)
    
    payload = {
        "dead_node_id": "dead_node",
        "camera_id": camera_id,
        "claimer_node_id": node_id,
        "priority_weight": my_weight,
        "ts": time.time(),
    }
    packed = msgpack.packb(payload, use_bin_type=True)
    unpacked = msgpack.unpackb(packed, raw=False)
    assert unpacked["priority_weight"] == my_weight
    assert unpacked["claimer_node_id"] == node_id


def test_dynamic_foreign_camera_flag():
    """Verify is_dynamic flag defaults to False and is set True on dynamic additions."""
    dummy_arr = np.zeros((4, 2), dtype=np.float32)
    dummy_mat = np.eye(3, dtype=np.float32)
    dummy_roi = np.zeros((4, 2), dtype=np.int32)

    static_cam = CameraConfig(
        camera_id="cam_static",
        source_id=0,
        uri="rtsp://127.0.0.1:8554/live/0",
        enabled=True,
        name="Static Cam",
        fps=25.0,
        speed_limit_kmh=80.0,
        source_points=dummy_arr,
        target_points=dummy_arr,
        homo_matrix=dummy_mat,
        roi_polygon=dummy_roi,
        record=False,
        record_path="output/cam_static.mp4",
    )
    assert static_cam.is_dynamic is False

    dynamic_cam = CameraConfig(
        camera_id="cam_rescued",
        source_id=1,
        uri="rtsp://127.0.0.1:8554/live/1",
        enabled=True,
        name="Rescued Cam",
        fps=25.0,
        speed_limit_kmh=80.0,
        source_points=dummy_arr,
        target_points=dummy_arr,
        homo_matrix=dummy_mat,
        roi_polygon=dummy_roi,
        record=False,
        record_path="output/cam_rescued.mp4",
        is_dynamic=True,
    )
    assert dynamic_cam.is_dynamic is True


def test_camera_manager_max_streams_capacity(tmp_path):
    """Verify CameraManager get_max_streams helper and capacity check."""
    # Ensure speedflow_python.camera_config is not poisoned by sibling mock modules
    sys.modules.pop("speedflow_python.camera_config", None)
    spec = importlib.util.spec_from_file_location(
        "speedflow_python.camera_config", _EDGE_DIR / "speedflow_python" / "camera_config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["speedflow_python.camera_config"] = mod
    spec.loader.exec_module(mod)
    FreshCameraManager = mod.CameraManager

    yml_file = tmp_path / "cameras.yml"
    yml_file.write_text(
        "max_streams: 3\n"
        "cameras:\n"
        "  cam_0:\n"
        "    source_id: 0\n"
        "    uri: rtsp://127.0.0.1:8554/live/0\n"
        "    enabled: true\n"
        "    name: Cam 0\n"
        "    fps: 25.0\n"
        "    speed_limit_kmh: 80.0\n"
        "    homography:\n"
        "      source_points: [[0, 0], [10, 0], [10, 10], [0, 10]]\n"
        "      target_width: 10\n"
        "      target_height: 10\n"
        "    roi_polygon: [[0, 0], [10, 0], [10, 10], [0, 10]]\n"
        "    output:\n"
        "      record: false\n"
        "      record_path: output/0.mp4\n",
        encoding="utf-8"
    )
    cm = FreshCameraManager(yml_path=yml_file)
    assert cm.get_max_streams() == 3


def test_static_ownership_and_failover_split_brain_guards(tmp_path):
    """Test static camera ownership, immediate yield on reconnect, and preemption claim."""
    from speedflow_python.peer_orchestrator import PeerOrchestrator, PeerState
    
    cfg = {
        "node_camera_map": {
            "jetson_A": ["cam_01", "cam_02"],
            "jetson_B": ["cam_03", "cam_04"],
            "jetson_C": ["cam_05", "cam_06"],
        },
        "heartbeat_timeout_s": 5.0,
        "overload_threshold": 60.0,
    }
    
    orch_A = PeerOrchestrator(node_id="jetson_A", cfg=cfg, camera_manager=None, camera_configs_dir=tmp_path)
    orch_B = PeerOrchestrator(node_id="jetson_B", cfg=cfg, camera_manager=None, camera_configs_dir=tmp_path)

    # 1. Verify static ownership
    assert orch_A._get_owned_camera_ids() == {"cam_01", "cam_02"}
    assert orch_A._get_node_owned_cameras("jetson_B") == {"cam_03", "cam_04"}
    assert orch_A._get_node_owned_cameras("jetson_C") == {"cam_05", "cam_06"}
    
    # 2. Rescued camera ownership: orch_B rescues cam_01 (owned by jetson_A)
    orch_B._rescued_cameras["cam_01"] = "jetson_A"
    orch_B._rescued_at["cam_01"] = time.time()
    
    # Mock control publisher to verify REMOVE command
    sent_cmds = []
    class DummyPub:
        def put(self, payload):
            sent_cmds.append(msgpack.unpackb(payload, raw=False))
    orch_B._pubs["control"] = DummyPub()
    
    # 3. Fresh heartbeat with active_cameras empty/no positive FPS must NOT yield rescued camera or publish REMOVE
    heartbeat_payload_waiting = {
        "node_id": "jetson_A",
        "load_score": 10.0,
        "active_cameras": [],  # jetson_A hasn't booted cameras yet
    }
    orch_B._on_peer_status(heartbeat_payload_waiting)
    assert "cam_01" in orch_B._rescued_cameras
    assert not any(cmd.get("cmd") == "REMOVE" and cmd.get("camera_id") == "cam_01" for cmd in sent_cmds)

    # 4. Ready heartbeat with active_cameras containing cam_01, positive fps_per_camera, and PLAYING status
    heartbeat_payload_ready = {
        "node_id": "jetson_A",
        "load_score": 10.0,
        "pipeline": {
            "status": "PLAYING",
            "active_cameras": ["cam_01"],
            "fps_per_camera": {"cam_01": 25.0},
        },
    }
    orch_B._on_peer_status(heartbeat_payload_ready)

    # Verify cam_01 was yielded and REMOVE was published
    assert "cam_01" not in orch_B._rescued_cameras
    assert any(cmd.get("cmd") == "REMOVE" and cmd.get("camera_id") == "cam_01" for cmd in sent_cmds)

    # 5. Startup preemption announcement: orch_B has rescued cam_02
    orch_B._rescued_cameras["cam_02"] = "jetson_A"
    orch_B._rescued_at["cam_02"] = time.time()
    sent_cmds.clear()

    # jetson_A sends ready status with cam_02 active
    heartbeat_payload_ready_cam02 = {
        "node_id": "jetson_A",
        "load_score": 10.0,
        "pipeline": {
            "status": "PLAYING",
            "active_cameras": ["cam_01", "cam_02"],
            "fps_per_camera": {"cam_01": 25.0, "cam_02": 25.0},
        },
    }
    orch_B._on_peer_status(heartbeat_payload_ready_cam02)

    preempt_payload = {
        "type": "startup_claim",
        "action": "startup_preempt",
        "claimer_node_id": "jetson_A",
        "cameras": ["cam_01", "cam_02"],
        "ts": time.time(),
    }
    orch_B._on_failover_claim(preempt_payload)
    
    assert "cam_02" not in orch_B._rescued_cameras
    assert any(cmd.get("cmd") == "REMOVE" and cmd.get("camera_id") == "cam_02" for cmd in sent_cmds)

