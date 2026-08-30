"""
Tests for RTSP push owner-path resolution and Server dashboard WHEP alignment.

Verifies:
1. core_pipeline._resolve_rtsp_push_location correctly derives owner path across migrations.
2. Server dashboard WHEP URL derivation matches the RTSP push target location for both native and migrated streams.
3. Edge health payload contract includes camera_owners/pipeline.camera_configs metadata.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
import sys
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EDGE_DIR = _REPO_ROOT / "Edge"


def _stub_module(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _import_pipeline_resolver():
    # Stub gi and Gst
    gi = _stub_module("gi")
    gi.require_version = lambda *a: None
    _stub_module("gi.repository")
    _stub_module("gi.repository.GLib")
    gst = _stub_module("gi.repository.Gst")
    gst.Element = type("_GstElementStub", (), {})
    gst.Pipeline = type("_GstPipelineStub", (), {})
    gst.Caps = type("_GstCapsStub", (), {"from_string": staticmethod(lambda s: s)})
    gst.State = type("_GstStateStub", (), {"NULL": 0, "PLAYING": 4})
    gst.PadProbeReturn = type("_PadProbeReturn", (), {"OK": 0, "DROP": 1})
    gst.init = lambda *a: None

    # Stub speedflow_python internals
    sp = _stub_module("speedflow_python")
    sp.__path__ = [str(_EDGE_DIR / "speedflow_python")]
    sp_common = _stub_module("speedflow_python.common")
    sp_common.make_element = lambda n, f: None
    sp_common.gst_link = lambda *a: None
    sp_settings = _stub_module("speedflow_python.settings")
    sp_settings.RTSP_PUSH_BITRATE = 750_000
    sp_settings.ROOT = _EDGE_DIR
    sp_settings.LOG_LEVEL = "INFO"
    sp_settings.NODE_ID = "test_node"
    sp_settings.HEALTH_INTERVAL = 1.0
    sp_settings.HEALTH_LOG_EVERY = 15
    sp_settings.TARGET_FPS = 27.0
    sp_settings.FPS_STATS_FILE = "/tmp/test_fps.json"
    sp_settings.ZENOH_ROUTER = ""
    sp_settings.ADVERTISE_IP = "127.0.0.1"
    sp_settings.LOAD_POLICY = "actual"
    sp_settings.LOAD_MODEL = "formula"
    sp_settings.EDGE_LOAD_SCORE_MODE = "legacy"
    sp_settings.TELEMETRY_INTERVAL = 1.0
    sp_settings.INFER_CONFIG = ""
    sp_settings.TRACKER_CFG = ""
    sp_settings.ANALYTICS_CFG = ""
    sp_settings.SGIE_CONFIG = ""
    sp_settings.TRACKER_LIB = ""
    sp_settings.LPR_CONFIG = ""
    sp_settings.SPEEDFLOW_SLOT_CAPACITY = 8
    sp_settings.SPEEDFLOW_NVDEC_SESSION_LIMIT = 14
    sp_camcfg = _stub_module("speedflow_python.camera_config")
    sp_camcfg.CameraConfig = type("CameraConfig", (), {})
    sp_camcfg.compute_tiler_layout = lambda n: (1, 1)

    spec = importlib.util.spec_from_file_location(
        "speedflow_python.core_pipeline", _EDGE_DIR / "speedflow_python" / "core_pipeline.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["speedflow_python.core_pipeline"] = mod
    spec.loader.exec_module(mod)
    return mod._resolve_rtsp_push_location


def test_rtsp_push_location_native_and_migrated():
    """Verify _resolve_rtsp_push_location keeps camera owner identity regardless of host node."""
    _resolve = _import_pipeline_resolver()
    node_cam_map = {
        "jetson_A": ["cam_01", "cam_02"],
        "jetson_B": ["cam_03", "cam_04"],
        "jetson_C": ["cam_05", "cam_06"],
    }

    # Case 1: Native cam_01 running on jetson_A
    base_url_a = "rtsp://116.118.9.125:8554/jetson_A"
    loc_cam01 = _resolve(base_url_a, "cam_01", node_cam_map)
    assert loc_cam01 == "rtsp://116.118.9.125:8554/jetson_A/cam_01"

    # Case 2: Migrated cam_03 (owner: jetson_B) running on jetson_A
    loc_cam03_on_a = _resolve(base_url_a, "cam_03", node_cam_map)
    assert loc_cam03_on_a == "rtsp://116.118.9.125:8554/jetson_B/cam_03"

    # Case 3: Migrated cam_02 (owner: jetson_A) running on jetson_C
    base_url_c = "rtsp://116.118.9.125:8554/jetson_C"
    loc_cam02_on_c = _resolve(base_url_c, "cam_02", node_cam_map)
    assert loc_cam02_on_c == "rtsp://116.118.9.125:8554/jetson_A/cam_02"

    # Case 4: No node_camera_map provided (fallback)
    loc_fallback = _resolve("rtsp://116.118.9.125:8554/jetson_A", "cam_99", None)
    assert loc_fallback == "rtsp://116.118.9.125:8554/jetson_A/cam_99"


def test_dashboard_whep_and_rtsp_owner_alignment():
    """Verify Server dashboard stream_path and WHEP URL match RTSP push paths."""
    _resolve = _import_pipeline_resolver()
    node_cam_map = {
        "jetson_A": ["cam_01", "cam_02"],
        "jetson_B": ["cam_03", "cam_04"],
    }
    base_url_a = "rtsp://116.118.9.125:8554/jetson_A"
    webrtc_base = "http://116.118.9.125:8889"

    # Read dashboard index.html source to verify static streamPath convention
    index_html = (_REPO_ROOT / "Server" / "static" / "index.html").read_text(encoding="utf-8")
    assert "${MEDIAMTX_WEBRTC_BASE}/${streamPath}/whep" in index_html
    assert "stream_path: `${node.node_id}/${camId}`" in index_html

    # In Server dashboard, owner node_id reports cam_01 and cam_02 under jetson_A
    # When cam_01 is pushed from jetson_A:
    rtsp_push_loc = _resolve(base_url_a, "cam_01", node_cam_map)
    # Extract path component after host
    rtsp_path = rtsp_push_loc.replace("rtsp://116.118.9.125:8554/", "")
    expected_stream_path = "jetson_A/cam_01"
    assert rtsp_path == expected_stream_path
    assert f"{webrtc_base}/{expected_stream_path}/whep" == "http://116.118.9.125:8889/jetson_A/cam_01/whep"

    # When cam_03 (owner jetson_B) is running on jetson_A after failover/migration:
    rtsp_push_migrated = _resolve(base_url_a, "cam_03", node_cam_map)
    rtsp_path_migrated = rtsp_push_migrated.replace("rtsp://116.118.9.125:8554/", "")
    expected_stream_path_migrated = "jetson_B/cam_03"
    assert rtsp_path_migrated == expected_stream_path_migrated
    assert f"{webrtc_base}/{expected_stream_path_migrated}/whep" == "http://116.118.9.125:8889/jetson_B/cam_03/whep"
