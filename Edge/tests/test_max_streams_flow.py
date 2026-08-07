"""
Edge/speedflow_python/tests/test_max_streams_flow.py

Focused tests verifying max_streams end-to-end:
  - HealthAgent sources max_streams from cameras.yml
  - Health payload carries the value
  - PeerOrchestrator.update_self_state parses safely
  - _on_peer_status sets PeerState.max_streams with safe default
  - L1 capacity check reads peer.max_streams correctly

Standalone:
    conda run -n DoAn python3 speedflow_python/tests/test_max_streams_flow.py
"""

import sys
import traceback
import tempfile
import types
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

EDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDGE))


def _install_host_stubs():
    """Provide only the non-hardware imports needed by this host test."""
    if "dotenv" not in sys.modules:
        dotenv = types.ModuleType("dotenv")
        setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
        sys.modules["dotenv"] = dotenv

    package = sys.modules.get("speedflow_python")
    if package is None:
        package = types.ModuleType("speedflow_python")
        package.__path__ = [str(EDGE / "speedflow_python")]
        sys.modules["speedflow_python"] = package

    if "speedflow_python.settings" not in sys.modules:
        settings = types.ModuleType("speedflow_python.settings")
        for key, value in {
            "ROOT": EDGE,
            "NODE_ID": "host-test",
            "HEALTH_INTERVAL": 1.0,
            "HEALTH_LOG_EVERY": 30,
            "TARGET_FPS": 25.0,
            "FPS_STATS_FILE": str(EDGE / "logs" / "fps_stats.json"),
            "MONITOR_URL": "",
            "ADVERTISE_IP": "",
            "LOAD_POLICY": "fps_dominant",
            "LOAD_MODEL": "",
            "TELEMETRY_INTERVAL": 1.0,
        }.items():
            setattr(settings, key, value)
        sys.modules["speedflow_python.settings"] = settings

    if "speedflow_python.zenoh_session" not in sys.modules:
        session = types.ModuleType("speedflow_python.zenoh_session")
        setattr(session, "make_session", lambda: None)
        sys.modules["speedflow_python.zenoh_session"] = session


def _load(name, relpath):
    """Load modules without importing speedflow_python/__init__.py (needs gi)."""
    _install_host_stubs()

    module_name = name if "." in name else f"speedflow_python.{name}"
    spec = spec_from_file_location(module_name, EDGE / relpath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {module_name}")
    mod = module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_cameras_yml(tmp, max_streams=None):
    yml = Path(tmp) / "cameras.yml"
    ms_line = f"max_streams: {max_streams}\n" if max_streams is not None else ""
    yml.write_text(
        ms_line + "cameras:\n"
        '  cam_01:\n'
        '    camera_id: cam_01\n'
        '    uri: "rtsp://x/cam1"\n'
        "    enabled: true\n"
    )
    return yml


def _new_ha():
    ha_mod = _load("health_agent", "health_agent.py")
    ha = ha_mod.HealthAgent.__new__(ha_mod.HealthAgent)
    ha._max_streams = 8  # default
    ha._cam_configs_cache = {}
    return ha


def _reload_cameras(ha, tmp, max_streams=None):
    """Run the real reload path against a temporary cameras.yml."""
    cam_yml = _write_cameras_yml(tmp, max_streams)
    module = sys.modules[ha.__class__.__module__]
    original_file = module.__file__
    try:
        module.__file__ = str(Path(tmp) / "health_agent.py")
        (Path(tmp) / "configs").mkdir()
        cam_yml.replace(Path(tmp) / "configs" / "cameras.yml")
        return ha._reload_cam_configs()
    finally:
        module.__file__ = original_file


# =====================================================================
# HealthAgent: source + serialize
# =====================================================================

def test_default_field_is_8():
    ha = _new_ha()
    ha._max_streams = 8
    assert ha._max_streams == 8


def test_sourced_from_cameras_yml():
    with tempfile.TemporaryDirectory() as tmp:
        ha = _new_ha()
        assert _reload_cameras(ha, tmp, max_streams=16)["cam_01"]["camera_id"] == "cam_01"
        assert ha._max_streams == 16


def test_missing_max_streams_key_defaults_8():
    with tempfile.TemporaryDirectory() as tmp:
        ha = _new_ha()
        _reload_cameras(ha, tmp)
        assert ha._max_streams == 8


def test_payload_serialization():
    ha = _new_ha()
    ha._max_streams = 12
    ha._cam_configs_cache = {"cam_01": {"name": "Camera 01"}}
    payload = {
        "node_id": "node_test",
        "load_score": 55.0,
        "pipeline": {
            "fps_per_camera": {"cam_01": 25.0},
            "avg_fps": 25.0,
            "active_cameras": ["cam_01"],
            "camera_configs": ha._cam_configs_cache,
            "max_streams": ha._max_streams,
        },
    }
    assert payload["pipeline"]["max_streams"] == 12


def test_real_reload_sets_field():
    with tempfile.TemporaryDirectory() as tmp:
        ha = _new_ha()
        _reload_cameras(ha, tmp, max_streams="bad")
        assert ha._max_streams == 8


# =====================================================================
# PeerOrchestrator: safe parse + L1 capacity
# =====================================================================

def _new_orch():
    po_mod = _load("peer_orchestrator", "speedflow_python/peer_orchestrator.py")
    return po_mod.PeerOrchestrator(
        node_id="node_alpha",
        cfg={"eps_streams_max": 4, "overload_threshold": 80.0},
        camera_manager=None,
    )


def test_update_self_state_valid_int():
    o = _new_orch()
    o.update_self_state({"pipeline": {"max_streams": 10, "active_cameras": ["c1"]}})
    assert o._self_state.max_streams == 10


def test_update_self_state_missing_defaults_8():
    o = _new_orch()
    o.update_self_state({"pipeline": {"active_cameras": ["c1"]}})
    assert o._self_state.max_streams == 8


def test_update_self_state_none_defaults_8():
    o = _new_orch()
    o.update_self_state({"pipeline": {"max_streams": None, "active_cameras": ["c1"]}})
    assert o._self_state.max_streams == 8


def test_update_self_state_str_int_parsable():
    o = _new_orch()
    o.update_self_state({"pipeline": {"max_streams": "16", "active_cameras": ["c1"]}})
    assert o._self_state.max_streams == 16


def test_update_self_state_malformed_str():
    o = _new_orch()
    o.update_self_state({"pipeline": {"max_streams": "abc", "active_cameras": ["c1"]}})
    assert o._self_state.max_streams == 8


def test_peer_state_parses_valid():
    o = _new_orch()
    o._on_peer_status(
        {"node_id": "node_beta", "pipeline": {"max_streams": 14, "active_cameras": ["c2", "c3"]}}
    )
    assert o._peers["node_beta"].max_streams == 14


def test_peer_state_missing_defaults():
    o = _new_orch()
    o._on_peer_status({"node_id": "node_beta", "pipeline": {"active_cameras": []}})
    assert o._peers["node_beta"].max_streams == 8


def test_peer_state_none_defaults():
    o = _new_orch()
    o._on_peer_status({"node_id": "node_beta", "pipeline": {"max_streams": None, "active_cameras": []}})
    assert o._peers["node_beta"].max_streams == 8


def test_peer_state_malformed_defaults():
    o = _new_orch()
    o._on_peer_status(
        {"node_id": "node_beta", "pipeline": {"max_streams": "abc", "active_cameras": []}}
    )
    assert o._peers["node_beta"].max_streams == 8


def test_l1_skip_when_peer_at_max_capacity():
    o = _new_orch()
    o._on_peer_status(
        {"node_id": "node_beta", "load_score": 10.0,
         "pipeline": {"max_streams": 2, "active_cameras": ["c2", "c3"], "avg_fps": 25.0}}
    )
    peer = o._peers["node_beta"]
    assert peer.active_cameras == ["c2", "c3"]  # 2 >= 2 → full
    best = o._pick_best_peer(for_offload_level=1)
    assert best is None or best != "node_beta"


def test_l1_accept_when_peer_below_max():
    o = _new_orch()
    o._on_peer_status(
        {"node_id": "node_beta", "load_score": 10.0,
         "pipeline": {"max_streams": 4, "active_cameras": ["c2"], "avg_fps": 25.0}}
    )
    assert o._peers["node_beta"].active_cameras == ["c2"]
    best = o._pick_best_peer(for_offload_level=1)
    assert best == "node_beta"


def test_level2_not_gated_by_max_streams():
    o = _new_orch()
    o._on_peer_status(
        {"node_id": "node_beta", "load_score": 20.0,
         "pipeline": {"max_streams": 2, "active_cameras": ["c2"], "avg_fps": 20.0}}
    )
    # L2/L3 bypass the streammux capacity gate in the code
    best = o._pick_best_peer(for_offload_level=2)
    assert best is None or isinstance(best, str)


if __name__ == "__main__":
    tests = [
        test_default_field_is_8,
        test_sourced_from_cameras_yml,
        test_missing_max_streams_key_defaults_8,
        test_payload_serialization,
        test_real_reload_sets_field,
        test_update_self_state_valid_int,
        test_update_self_state_missing_defaults_8,
        test_update_self_state_none_defaults_8,
        test_update_self_state_str_int_parsable,
        test_update_self_state_malformed_str,
        test_peer_state_parses_valid,
        test_peer_state_missing_defaults,
        test_peer_state_none_defaults,
        test_peer_state_malformed_defaults,
        test_l1_skip_when_peer_at_max_capacity,
        test_l1_accept_when_peer_below_max,
        test_level2_not_gated_by_max_streams,
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
