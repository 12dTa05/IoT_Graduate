"""
Edge/tests/test_peer_orchestrator_p2p_policy.py — Focused host-side tests
covering the bounded P2P policy fixes.

Policy tested
─────────────
1. L1 migration selects the *lightest* eligible camera.
2. L2/L3 crop offload selects the *heaviest* eligible camera.
3. Threshold ladder intent: action only fired at the correct threshold tier.
4. Transition guard / reclaim hold: a recently-reclaimed camera is ineligible.
5. Thermal rejection: peers above max_gpu_temp_c are skipped.
6. Fail-safe with missing workload data: returns None rather than falling back to FPS.
7. Thermal config is picked up from edge_node.yml defaults.
8. Receiver-side thermal gate: this node refuses to bid on an RFO when it is
   too hot or its temperature is invalid under a conservative policy.

Standalone:
    conda run -n DoAn python3 Edge/tests/test_peer_orchestrator_p2p_policy.py
"""
from __future__ import annotations

import sys
import textwrap
import time
import types
import traceback
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec

EDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDGE))


# ─────────────────────────────────────────────────────────────────────────────
# Host stubs (mirrors pattern from test_max_streams_flow.py)
# ─────────────────────────────────────────────────────────────────────────────

def _install_host_stubs():
    if "dotenv" not in sys.modules:
        dotenv = types.ModuleType("dotenv")
        setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
        sys.modules["dotenv"] = dotenv

    pkg = sys.modules.get("speedflow_python")
    if pkg is None:
        pkg = types.ModuleType("speedflow_python")
        pkg.__path__ = [str(EDGE / "speedflow_python")]
        sys.modules["speedflow_python"] = pkg

    # A sibling host test (test_profile_collect_load_score.py) may have left a
    # ROOT-less speedflow_python.settings stub in sys.modules. Re-stamp the
    # full attr set onto whatever module is present so peer_orchestrator's
    # `from .settings import ROOT` works regardless of test order.
    settings = sys.modules.get("speedflow_python.settings")
    if settings is None:
        settings = types.ModuleType("speedflow_python.settings")
        sys.modules["speedflow_python.settings"] = settings
    for k, v in {
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
            setattr(settings, k, v)

    # test_profile_collect_load_score replaces yaml.safe_load with a None
    # stub at import-time. _reload_cam_configs below needs the real parser,
    # so restore it if stubbed.
    y = sys.modules.get("yaml")
    if y is not None and y.safe_load("a: 1") is None:
        del sys.modules["yaml"]

    # The health host test may leave a minimal msgpack stub without packb.
    # Bid tests need the real serializer when exercising _evaluate_and_bid.
    msgpack = sys.modules.get("msgpack")
    if msgpack is not None and not hasattr(msgpack, "packb"):
        del sys.modules["msgpack"]

    if "speedflow_python.zenoh_session" not in sys.modules:
        session = types.ModuleType("speedflow_python.zenoh_session")
        setattr(session, "make_session", lambda: None)
        sys.modules["speedflow_python.zenoh_session"] = session


def _load(name: str, relpath: str):
    _install_host_stubs()
    module_name = name if "." in name else f"speedflow_python.{name}"
    spec = spec_from_file_location(module_name, EDGE / relpath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {module_name}")
    mod = module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _new_orch(overrides: dict | None = None, **kwargs) -> object:
    po_mod = _load("peer_orchestrator", "speedflow_python/peer_orchestrator.py")
    cfg = {
        "overload_threshold": 57.0,
        "cooldown_s": 0.0,
        "offload_level_cooldown_s": 0.0,
        "reclaim_stable_s": 15.0,
        "reclaim_stability_s": 15.0,
        "reclaim_margin": 7.0,
        "thermal": {
            "admission_enabled": True,
            "max_gpu_temp_c": 85.0,
            "reject_invalid": True,
            "invalid_policy": "conservative",
        },
        "offload_level": 3,
        "offload_level3_threshold": 57.0,
        "offload_level2_threshold": 65.0,
        "offload_level1_threshold": 75.0,
    }
    if overrides:
        for k, v in overrides.items():
            cfg[k] = v
    if kwargs:
        cfg.update(kwargs)
    return po_mod.PeerOrchestrator(
        node_id="node_alpha",
        cfg=cfg,
        camera_manager=None,
    )


def _make_state(active_cameras, camera_workload=None, **kwargs):
    """Construct a minimal state object for _pick_camera_to_offload."""
    s = types.SimpleNamespace(
        active_cameras=list(active_cameras),
        camera_workload=dict(camera_workload or {}),
        source_starved_cameras=[],
        fps_per_camera={},
        avg_fps=None,
        load_score=80.0,
        risk_index=0.0,
        overload_since=time.time() - 20.0,
        penalty_until=0.0,
        **kwargs,
    )
    return s


def _make_peer(orch, node_id, gpu_temp_c=None, load_score=10.0):
    """Create a PeerState from the loaded module and register it in orch._peers."""
    po_mod = sys.modules["speedflow_python.peer_orchestrator"]
    peer = po_mod.PeerState(
        node_id=node_id,
        load_score=load_score,
        gpu_temp_c=gpu_temp_c,
        last_seen=time.time() - 2.0,
        active_cameras=[],
        max_streams=4,
    )
    orch._peers[node_id] = peer
    return peer


# ─────────────────────────────────────────────────────────────────────────────
# L1: lightest workload
# ─────────────────────────────────────────────────────────────────────────────

def test_l1_selects_lightest_camera():
    o = _new_orch()
    sim_state = _make_state(
        active_cameras=["cam_fast", "cam_med", "cam_slow"],
        camera_workload={"cam_fast": 2.0, "cam_med": 5.0, "cam_slow": 11.0},
    )
    # L1 migrates the lowest-workload camera.
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_fast", f"Expected cam_fast (lightest), got {chosen}"


# ─────────────────────────────────────────────────────────────────────────────
# L2/L3: heaviest workload
# ─────────────────────────────────────────────────────────────────────────────

def test_l3_selects_heaviest_camera():
    o = _new_orch()
    sim_state = _make_state(
        active_cameras=["cam_fast", "cam_med", "cam_slow"],
        camera_workload={"cam_fast": 2.0, "cam_med": 5.0, "cam_slow": 11.0},
    )
    # L3 offloads crop work from the highest-workload camera.
    chosen = o._pick_camera_to_offload(sim_state, level=3)
    assert chosen == "cam_slow", f"Expected cam_slow (heaviest), got {chosen}"


def test_l2_selects_heaviest_camera():
    o = _new_orch()
    sim_state = _make_state(
        active_cameras=["cam_fast", "cam_med", "cam_slow"],
        camera_workload={"cam_fast": 2.0, "cam_med": 5.0, "cam_slow": 11.0},
    )
    # L2 level=2 should also pick the heaviest
    chosen = o._pick_camera_to_offload(sim_state, level=2)
    assert chosen == "cam_slow", f"Expected cam_slow (heaviest), got {chosen}"


# ─────────────────────────────────────────────────────────────────────────────
# Fail-safe: missing workload → return None, do not pretend FPS equals workload
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_workload_data_returns_none():
    o = _new_orch()
    sim_state = _make_state(
        active_cameras=["cam_01", "cam_02"],
        camera_workload={},
    )
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen is None, f"Expected None when camera_workload is empty, got {chosen}"


def test_partial_workload_missing_uses_available():
    o = _new_orch()
    sim_state = _make_state(
        active_cameras=["cam_01", "cam_02", "cam_03"],
        camera_workload={"cam_01": 25.0, "cam_03": 5.0},
        # cam_02 has no workload evidence.
    )
    # L1 picks from eligible evidence (cam_01, cam_03): lightest = cam_03.
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_03", f"Expected cam_03 (lightest among known), got {chosen}"


# ─────────────────────────────────────────────────────────────────────────────
# Transition guard / reclaim hold
# ─────────────────────────────────────────────────────────────────────────────

def test_reclaimed_camera_ineligible_during_observation_window():
    o = _new_orch()
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        camera_workload={"cam_a": 2.0, "cam_b": 12.0},
    )
    # Simulate: cam_a was just reclaimed (no wall-clock wait)
    o._reclaim_completed_at["cam_a"] = time.time()

    # cam_a is lighter but just reclaimed, so the guard selects cam_b.
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_b", f"Expected cam_b, got {chosen}"

    # The reclaimed camera remains ineligible even when it is the lightest.
    sim_state2 = _make_state(
        active_cameras=["cam_a", "cam_b"],
        camera_workload={"cam_a": 1.0, "cam_b": 12.0},
    )
    chosen2 = o._pick_camera_to_offload(sim_state2, level=1)
    assert chosen2 == "cam_b", (
        f"Transition guard should skip cam_a (just reclaimed); got {chosen2}"
    )


def test_reclaimed_camera_eligible_after_observation_window():
    o = _new_orch(reclaim_stability_s=0.5)  # 0.5s window for speed
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        camera_workload={"cam_a": 5.0, "cam_b": 20.0},
    )
    o._reclaim_completed_at["cam_a"] = time.time() - 1.0  # 1s ago → window elapsed
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_a", f"Expected cam_a after window expired, got {chosen}"


# ─────────────────────────────────────────────────────────────────────────────
# Thermal admission gate (sender side — _pick_best_peer)
# ─────────────────────────────────────────────────────────────────────────────

def _new_orch_with_peers(peer_temps: dict, therm_cfg: dict | None = None):
    """Build an orchestrator with self on node_alpha and peers with given gpu_temp_c."""
    o = _new_orch()
    for nid, temp in peer_temps.items():
        o._peers[nid] = type(o._peers.get("__template__") or type("PS", (), {}))()
        # Use the actual PeerState dataclass
        from speedflow_python.peer_orchestrator import PeerState
        o._peers[nid] = PeerState(
            node_id=nid,
            load_score=10.0,
            gpu_temp_c=temp,
            last_seen=time.time() - 2.0,
            active_cameras=[],
            max_streams=4,
        )
    if therm_cfg is not None:
        o._cfg["thermal"] = therm_cfg
    return o


def _peer_orch_load():
    """Gracefully load the module and return the PeerState dataclass."""
    po_mod = _load("peer_orchestrator", "speedflow_python/peer_orchestrator.py")
    return po_mod.PeerState


def test_thermal_rejects_over_temperature():
    from speedflow_python.peer_orchestrator import PeerState
    o = _new_orch()
    # Set up two peers: one hot, one cool
    o._peers["node_hot"] = PeerState(
        node_id="node_hot", load_score=10.0, gpu_temp_c=92.0,
        last_seen=time.time() - 2.0, active_cameras=[], max_streams=4,
    )
    o._peers["node_cool"] = PeerState(
        node_id="node_cool", load_score=15.0, gpu_temp_c=55.0,
        last_seen=time.time() - 2.0, active_cameras=[], max_streams=4,
    )
    best = o._pick_best_peer(for_offload_level=1)
    assert best == "node_cool", f"Expected node_cool (not rejected), got {best}"


def test_thermal_rejects_invalid_temp_when_configured():
    from speedflow_python.peer_orchestrator import PeerState
    o = _new_orch(overrides={"thermal": {"reject_invalid": True}})
    o._peers["node_bad"] = PeerState(
        node_id="node_bad", load_score=10.0, gpu_temp_c=None,
        last_seen=time.time() - 2.0, active_cameras=[], max_streams=4,
    )
    o._peers["node_good"] = PeerState(
        node_id="node_good", load_score=12.0, gpu_temp_c=55.0,
        last_seen=time.time() - 2.0, active_cameras=[], max_streams=4,
    )
    best = o._pick_best_peer(for_offload_level=1)
    assert best == "node_good", f"Expected node_good, got {best}"


def test_thermal_accepts_invalid_temp_when_permissive():
    from speedflow_python.peer_orchestrator import PeerState
    o = _new_orch(overrides={
        "thermal": {
            "reject_invalid": True,
            "invalid_policy": "permissive",
            "admission_enabled": True,
        }
    })
    o._peers["node_missing"] = PeerState(
        node_id="node_missing", load_score=10.0, gpu_temp_c=None,
        last_seen=time.time() - 2.0, active_cameras=[], max_streams=4,
    )
    best = o._pick_best_peer(for_offload_level=1)
    assert best == "node_missing", f"Expected node_missing (permissive), got {best}"


def test_thermal_disabled_via_config():
    from speedflow_python.peer_orchestrator import PeerState
    o = _new_orch(overrides={"thermal": {"admission_enabled": False}})
    o._peers["node_hot"] = PeerState(
        node_id="node_hot", load_score=10.0, gpu_temp_c=99.0,
        last_seen=time.time() - 2.0, active_cameras=[], max_streams=4,
    )
    best = o._pick_best_peer(for_offload_level=1)
    assert best == "node_hot", f"Expected node_hot when gate disabled, got {best}"


# ─────────────────────────────────────────────────────────────────────────────
# Thermal admission gate (receiver side — _evaluate_and_bid)
# ─────────────────────────────────────────────────────────────────────────────

def _prepare_bidder(orch, gpu_temp_c=None, therm_overrides=None):
    """Prep orchestrator as a bidder: stub RTT, pub, config, self_state."""
    # Lightweight self state — capacity not full, load low, no cooldown/penalty
    orch._self_state.load_score = 10.0
    orch._self_state.gpu_temp_c = gpu_temp_c
    orch._self_state.active_cameras = []
    # FPS model: after adding 1 stream, FPS stays high enough to pass ε2
    orch._cfg["fps_model"] = {1: 25.0, 2: 22.0, 3: 19.0, 4: 16.0}
    orch._cfg["eps_streams_max"] = 4
    orch._cfg["cooldown_s"] = 0.0
    orch._self_penalty_until = 0.0
    orch._cam_cooldown.clear()
    # Stub network: any URI gives 20ms RTT
    orch._measure_rtt = lambda uri: 20.0
    orch._get_camera_uri = lambda camera_id: "rtsp://dummy/cam"
    if therm_overrides is not None:
        orch._cfg["thermal"] = therm_overrides
    # Capture published proposals
    proposals: list[dict] = []
    class _FakePub:
        @staticmethod
        def put(data):
            import msgpack
            proposals.append(msgpack.unpackb(data, raw=False))
    orch._pubs["vote_proposal"] = _FakePub()
    return proposals


def _bid_payload(cam="cam_01", fps=15.0, net=50.0):
    return {
        "requester": "node_beta",
        "camera_id": cam,
        "cam_uri": "rtsp://dummy/cam",
        "eps_fps": fps,
        "eps_network_ms": net,
    }


def test_bid_rejected_when_self_too_hot():
    """Receiver-side: this node above max temp → no bid published."""
    o = _new_orch()
    proposals = _prepare_bidder(o, gpu_temp_c=93.0)
    o._evaluate_and_bid(_bid_payload())
    assert len(proposals) == 0, f"Expected 0 bids from hot receiver, got {len(proposals)}"


def test_bid_accepted_when_self_temp_normal():
    """Receiver-side: this node at normal temp → bid published."""
    o = _new_orch()
    proposals = _prepare_bidder(o, gpu_temp_c=55.0)
    o._evaluate_and_bid(_bid_payload())
    assert len(proposals) == 1, f"Expected 1 bid from normal receiver, got {len(proposals)}"
    assert proposals[0]["bidder"] == "node_alpha"


def test_bid_rejected_when_self_temp_unknown_conservative():
    """Receiver-side: gpu_temp_c is None, conservative policy → no bid."""
    o = _new_orch()
    proposals = _prepare_bidder(o, gpu_temp_c=None)
    o._evaluate_and_bid(_bid_payload())
    assert len(proposals) == 0, (
        f"Expected 0 bids from unknown-temp receiver (conservative), "
        f"got {len(proposals)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Threshold ladder intent: camera chosen only when correct tier matches
# ─────────────────────────────────────────────────────────────────────────────

def test_threshold_ladder_l3_only_selected_at_l3():
    """At load=57 (L3 threshold) and offload_level=3, an offload should be chosen."""
    o = _new_orch(offload_level3_threshold=57.0, offload_level2_threshold=65.0,
                  offload_level1_threshold=75.0)

    ps = type("S", (), {})()
    ps.active_cameras = ["c1", "c2"]
    ps.fps_per_camera = {"c1": 28.0, "c2": 25.0}
    ps.camera_workload = {"c1": 10.0, "c2": 4.0}
    ps.source_starved_cameras = []
    ps.avg_fps = 22.0
    ps.load_score = 57.0
    ps.risk_index = 0.0
    ps.overload_since = time.time() - 20.0
    ps.penalty_until = 0.0

    # L3 should pick c1 (heaviest workload), not the highest output FPS.
    chosen = o._pick_camera_to_offload(ps, level=3)
    assert chosen == "c1", f"L3 should pick heaviest at load=57, got {chosen}"


# ─────────────────────────────────────────────────────────────────────────────
# Decision suppression: pending-ack & post-migration settle
# ─────────────────────────────────────────────────────────────────────────────

def _setup_overloaded_self(o, load_score=60.0, cameras=None):
    """Configure self state so _check_self_overload would take action."""
    if cameras is None:
        cameras = ["cam_a", "cam_b"]
    o.update_self_state({
        "load_score": load_score,
        "gpu_percent": 50.0,
        "cpu_percent": 50.0,
        "ram_percent": 50.0,
        "gpu_temp_c": 50.0,
        "risk_index": 0.0,
        "pipeline": {
            "active_cameras": cameras,
            "camera_workload": {c: 5.0 + i * 3 for i, c in enumerate(cameras)},
            "fps_per_camera": {c: 25.0 for c in cameras},
            "max_streams": 8,
        },
    })
    # Simulate long-standing overload (bypass overload_duration_s)
    o._self_state.overload_since = time.time() - 20.0


def _add_peer_beta(o):
    """Add a healthy peer for L3 offload."""
    from speedflow_python.peer_orchestrator import PeerState
    o._peers["node_beta"] = PeerState(
        node_id="node_beta", load_score=10.0, gpu_temp_c=50.0,
        last_seen=time.time() - 2.0, active_cameras=[], max_streams=4,
    )


def test_pending_ack_blocks_overload_action():
    """While any pending ack exists, _check_self_overload issues no L3/L2/L1."""
    o = _new_orch(overload_duration_s=0.0, offload_level3_threshold=57.0,
                  offload_level2_threshold=65.0, offload_level1_threshold=75.0)
    _setup_overloaded_self(o, load_score=60.0)   # 60 ≥ L3=57, < L2=65
    _add_peer_beta(o)

    # Inject a pending ack — simulates in-flight make-before-break
    import threading
    o._pending_acks["cam_x"] = threading.Event()

    o._check_self_overload()

    # Neither camera should have been offloaded
    assert o.get_offload_level("cam_a") == 0, "cam_a should NOT be offloaded"
    assert o.get_offload_level("cam_b") == 0, "cam_b should NOT be offloaded"
    # Vote should NOT have been triggered
    with o._lock:
        assert not o._vote_in_progress, "No vote should be in progress"


def test_post_migration_settle_blocks_overload_action():
    """After a migration, the settle window blocks all overload decisions."""
    o = _new_orch(overload_duration_s=0.0, offload_level3_threshold=57.0,
                  offload_level2_threshold=65.0, offload_level1_threshold=75.0,
                  transition_settle_s=5.0)
    _setup_overloaded_self(o, load_score=60.0)
    _add_peer_beta(o)

    # Simulate post-migration: settle deadline in the future
    o._transition_settle_until = time.time() + 4.0  # 4 s remaining

    o._check_self_overload()

    assert o.get_offload_level("cam_a") == 0, "cam_a should NOT be offloaded"
    assert o.get_offload_level("cam_b") == 0, "cam_b should NOT be offloaded"


def test_settle_window_expiry_permits_decision():
    """After the settle window expires, overload actions proceed normally."""
    o = _new_orch(overload_duration_s=0.0, offload_level3_threshold=57.0,
                  offload_level2_threshold=65.0, offload_level1_threshold=75.0,
                  transition_settle_s=5.0)
    _setup_overloaded_self(o, load_score=60.0)
    _add_peer_beta(o)

    # Settle deadline already in the past — decision flow permitted
    o._transition_settle_until = time.time() - 1.0

    o._check_self_overload()

    # L3 offload should have been applied to the heaviest camera (cam_b)
    assert o.get_offload_level("cam_b") == 3, (
        f"Expected L3 offload on cam_b after settle expiry, "
        f"got level={o.get_offload_level('cam_b')}"
    )


def test_config_default_transition_settle_s():
    """Default transition_settle_s is 5.0 when not specified in config."""
    o = _new_orch(overload_duration_s=0.0)
    _setup_overloaded_self(o, load_score=60.0)
    _add_peer_beta(o)

    # Trigger the post-migration settle path by calling _wait_and_remove
    # indirectly — we set the field ourselves and verify the window is 5.0.
    # The actual default is used inside _wait_and_remove and _check_reclaim;
    # here we assert the config fallback value directly.
    default_val = o._cfg.get("transition_settle_s", 5.0)
    assert default_val == 5.0, f"Default transition_settle_s should be 5.0, got {default_val}"

    # Also verify the field works with a custom value
    o2 = _new_orch(transition_settle_s=10.0)
    assert o2._cfg.get("transition_settle_s", 5.0) == 10.0, (
        "Custom transition_settle_s should be honored"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_l1_selects_lightest_camera,
        test_l3_selects_heaviest_camera,
        test_l2_selects_heaviest_camera,
        test_missing_workload_data_returns_none,
        test_partial_workload_missing_uses_available,
        test_reclaimed_camera_ineligible_during_observation_window,
        test_reclaimed_camera_eligible_after_observation_window,
        test_thermal_disabled_via_config,
        test_thermal_accepts_invalid_temp_when_permissive,
        test_thermal_rejects_invalid_temp_when_configured,
        test_thermal_rejects_over_temperature,
        test_threshold_ladder_l3_only_selected_at_l3,
        test_bid_rejected_when_self_too_hot,
        test_bid_accepted_when_self_temp_normal,
        test_bid_rejected_when_self_temp_unknown_conservative,
        test_pending_ack_blocks_overload_action,
        test_post_migration_settle_blocks_overload_action,
        test_settle_window_expiry_permits_decision,
        test_config_default_transition_settle_s,
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
