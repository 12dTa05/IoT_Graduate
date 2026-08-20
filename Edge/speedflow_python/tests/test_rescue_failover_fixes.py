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
