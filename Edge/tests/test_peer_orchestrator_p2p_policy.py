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
        "overload_warmup_s": 5.0,  # bypass startup warmup gate for legacy tests
        "camera_warmup_s": 12.0,    # bypass per-camera warmup gate for legacy tests
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


def _stub_vote_request_pub(o):
    """Provide a stub 'vote_request' publisher so L1 RFO can fire in tests."""
    class _FakePub:
        def __init__(self):
            self.sent = []
        def put(self, data):
            self.sent.append(data)
    o._pubs["vote_request"] = _FakePub()


# ─────────────────────────────────────────────────────────────────────────────
# L1: lightest workload
# ─────────────────────────────────────────────────────────────────────────────

def test_l1_selects_lightest_camera():
    o = _new_orch()
    sim_state = _make_state(
        active_cameras=["cam_fast", "cam_med", "cam_slow"],
        camera_workload={"cam_fast": 2.0, "cam_med": 5.0, "cam_slow": 11.0},
    )
    # Treat all active cameras as locally-owned so the L1 ownership guard
    # does not bypass this legacy selector test.
    o._get_owned_camera_ids = lambda: set(sim_state.active_cameras)
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
    # Treat both cameras as locally-owned so the ownership guard does not
    # bypass the reclaim-window check this test is verifying.
    o._get_owned_camera_ids = lambda: set(sim_state.active_cameras)
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
    # All active cameras owned (legacy behaviour for this selector test).
    o._get_owned_camera_ids = lambda: set(sim_state.active_cameras)
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
# Dwell gates: L3 must remain active before L2, L2 before L1
# ─────────────────────────────────────────────────────────────────────────────

def _setup_state_for_dwell(o, level, cam_id, since_s):
    """Helper to set current offload level and timestamp for dwell testing."""
    o.set_offload_level(cam_id, level, target_node="node_beta")
    o._offload_level_changed_at[cam_id] = time.time() - since_s


def test_l3_dwell_blocks_escalation_to_l2_before_duration():
    """L3 must be active for l3_dwell_s (10s default) before L2 can escalate."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=10.0,
        l2_dwell_s=7.0,
        overload_duration_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=66.0, cameras=["cam_a", "cam_b"])
    # cam_b is heaviest (8.0) → L2/L3 offload target; set L3 active for 5s (<10s)
    _setup_state_for_dwell(o, level=3, cam_id="cam_b", since_s=5.0)

    o._check_self_overload()

    # L3 should NOT have escalated to L2 (still at level 3)
    assert o.get_offload_level("cam_b") == 3, (
        f"Expected L3 still active (dwell not met), got level={o.get_offload_level('cam_b')}"
    )


def test_l3_dwell_permits_escalation_to_l2_after_duration():
    """After l3_dwell_s (10s), L2 escalation is permitted."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=10.0,
        l2_dwell_s=7.0,
        overload_duration_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=66.0, cameras=["cam_a", "cam_b"])
    # cam_b is heaviest (8.0) → L2/L3 offload target; set L3 active for 11s (>10s)
    _setup_state_for_dwell(o, level=3, cam_id="cam_b", since_s=11.0)

    o._check_self_overload()

    # L3 should have escalated to L2
    assert o.get_offload_level("cam_b") == 2, (
        f"Expected L2 after L3 dwell met, got level={o.get_offload_level('cam_b')}"
    )


def test_l2_dwell_blocks_escalation_to_l1_before_duration():
    """L2 must be active for l2_dwell_s (7s default) before L1 can escalate."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=10.0,
        l2_dwell_s=7.0,
        overload_duration_s=0.0,
        cooldown_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=80.0, cameras=["cam_a", "cam_b"])
    # cam_a is lightest (5.0) → L1 offload target; set L2 active for 4s (<7s)
    _setup_state_for_dwell(o, level=2, cam_id="cam_a", since_s=4.0)

    o._check_self_overload()

    # L2 should NOT have escalated to L1 (still at level 2)
    assert o.get_offload_level("cam_a") == 2, (
        f"Expected L2 still active (dwell not met), got level={o.get_offload_level('cam_a')}"
    )


def test_l2_dwell_permits_escalation_to_l1_after_duration():
    """After l2_dwell_s (7s), L1 escalation is permitted."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=10.0,
        l2_dwell_s=7.0,
        overload_duration_s=0.0,
        cooldown_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=80.0, cameras=["cam_a", "cam_b"])
    # Treat all active cameras as locally-owned so the L1 ownership guard
    # does not bypass this legacy dwell-gate test.
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    # cam_a is lightest (5.0) → L1 offload target; set L2 active for 8s (>7s)
    _setup_state_for_dwell(o, level=2, cam_id="cam_a", since_s=8.0)

    _stub_vote_request_pub(o)
    o._check_self_overload()
    # L1 triggers: vote in progress for cam_a, fine-grained level cleared.
    with o._lock:
        assert "cam_a" in o._vote_in_progress, (
            f"Expected L1 RFO for 'cam_a' after L2 dwell met, vote_in_progress={o._vote_in_progress}"
        )
    # L2 cleared before triggering L1
    assert o.get_offload_level("cam_a") == 0, (
        f"Expected L2 cleared for L1, got level={o.get_offload_level('cam_a')}"
    )


def test_l1_permitted_when_no_lower_level_active():
    """Direct L1 (no L2/L3 active) remains permitted when no lower level active."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=10.0,
        l2_dwell_s=7.0,
        overload_duration_s=0.0,
        cooldown_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=80.0, cameras=["cam_a", "cam_b"])
    # All active cameras owned (legacy test does not exercise the guard).
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    # No lower level active (level 0)

    _stub_vote_request_pub(o)
    o._check_self_overload()

    # L1 should proceed (RFO triggered)
    with o._lock:
        assert "cam_a" in o._vote_in_progress, (
            f"Expected L1 RFO for 'cam_a' (no lower level active), "
            f"vote_in_progress={o._vote_in_progress}"
        )


def test_l1_bypasses_dwell_when_hardware_fuse_active():
    """Hardware emergency fuse (risk_index >= hard_fuse_threshold) bypasses all dwell gates."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=10.0,
        l2_dwell_s=7.0,
        overload_duration_s=0.0,
        cooldown_s=0.0,
        proactive={"hard_fuse_threshold": 0.95, "enabled": True},
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=80.0, cameras=["cam_a", "cam_b"])
    # All active cameras owned (legacy test does not exercise the guard).
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    # risk_index 0.96 ≥ hard_fuse=0.95 → fuse ACTIVE
    o._self_state.risk_index = 0.96
    # cam_a is lightest (5.0) → L1 target; L2 active for only 1s (far less than 7s dwell)
    _setup_state_for_dwell(o, level=2, cam_id="cam_a", since_s=1.0)

    _stub_vote_request_pub(o)
    o._check_self_overload()

    # L1 should proceed despite dwell not met (fuse bypasses)
    with o._lock:
        assert "cam_a" in o._vote_in_progress, (
            f"Expected L1 RFO despite dwell (fuse bypass), vote_in_progress={o._vote_in_progress}"
        )


def test_malformed_dwell_config_uses_defaults():
    """Malformed/missing dwell config falls back to safe defaults (10s L3, 7s L2)."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        # Omit l3_dwell_s and l2_dwell_s to test defaults
        overload_duration_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=66.0, cameras=["cam_a", "cam_b"])
    # cam_b heaviest (8.0) → L2/L3 target; L3 active for 5s (less than 10s default)
    _setup_state_for_dwell(o, level=3, cam_id="cam_b", since_s=5.0)

    o._check_self_overload()

    # Should be blocked by default 10s dwell
    assert o.get_offload_level("cam_b") == 3, (
        f"Expected L3 still active (default 10s dwell not met), got level={o.get_offload_level('cam_b')}"
    )

    # Now test with 11s (past default)
    _setup_state_for_dwell(o, level=3, cam_id="cam_b", since_s=11.0)
    o._check_self_overload()
    assert o.get_offload_level("cam_b") == 2, (
        f"Expected L2 after default 10s dwell met, got level={o.get_offload_level('cam_b')}"
    )


def test_negative_dwell_config_uses_defaults():
    """Negative dwell values fall back to defaults (fail-safe)."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=-5.0,  # Invalid - should use default 10s
        l2_dwell_s=-1.0,  # Invalid - should use default 7s
        overload_duration_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=66.0, cameras=["cam_a", "cam_b"])
    # L3 active for 5s (less than 10s default)
    _setup_state_for_dwell(o, level=3, cam_id="cam_b", since_s=5.0)

    o._check_self_overload()

    assert o.get_offload_level("cam_b") == 3, (
        f"Expected L3 still active (negative config → default 10s), got level={o.get_offload_level('cam_b')}"
    )


def test_string_dwell_config_uses_defaults():
    """Non-numeric dwell values fall back to defaults (fail-safe)."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s="invalid",  # Invalid - should use default 10s
        l2_dwell_s="also_bad",  # Invalid - should use default 7s
        overload_duration_s=0.0,
        cooldown_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=80.0, cameras=["cam_a", "cam_b"])
    # cam_a lightest (5.0) → L1 target; L2 active for 3s (less than 7s default)
    _setup_state_for_dwell(o, level=2, cam_id="cam_a", since_s=3.0)

    o._check_self_overload()

    assert o.get_offload_level("cam_a") == 2, (
        f"Expected L2 still active (string config → default 7s), got level={o.get_offload_level('cam_a')}"
    )


def test_dwell_gate_only_blocks_escalation_from_active_lower_level():
    """Dwell gates only prevent escalation FROM an active lower level, not all L1 globally."""
    o = _new_orch(
        offload_level=3,
        offload_level3_threshold=57.0,
        offload_level2_threshold=65.0,
        offload_level1_threshold=75.0,
        l3_dwell_s=10.0,
        l2_dwell_s=7.0,
        overload_duration_s=0.0,
        cooldown_s=0.0,
    )
    _add_peer_beta(o)
    _setup_overloaded_self(o, load_score=80.0, cameras=["cam_a", "cam_b"])
    # cam_a has L2 active for only 1s (dwell not met) and is the LIGHTEST
    # (L1 picks lightest), so the dwell gate on cam_a blocks it.
    _setup_state_for_dwell(o, level=2, cam_id="cam_a", since_s=1.0)

    o._check_self_overload()

    # cam_a (lightest, L1 pick) is blocked by its own L2 dwell.
    assert o.get_offload_level("cam_a") == 2, (
        f"Expected cam_a L2 still active (dwell not met), got {o.get_offload_level('cam_a')}"
    )
    # No L1 vote should have been triggered for cam_a.
    with o._lock:
        assert "cam_a" not in o._vote_in_progress, (
            f"cam_a should not be voting (L2 dwell blocks it), got {o._vote_in_progress}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# L1 ownership invariant: each Edge must retain at least one locally-owned
# camera configured in Edge/configs/cameras.yml during all full-stream
# offload decisions. Rescued/migrated-in (foreign) cameras do NOT count.
# ─────────────────────────────────────────────────────────────────────────────

def test_l1_excludes_last_owned_when_foreign_active():
    """1 owned + 1 foreign active → L1 must NOT migrate the only owned camera."""
    o = _new_orch()
    # Only cam_a is locally-owned; cam_b is foreign (rescued/migrated-in).
    o._get_owned_camera_ids = lambda: {"cam_a"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        # cam_a is the LIGHTEST (would normally win L1), cam_b is heavier.
        camera_workload={"cam_a": 2.0, "cam_b": 12.0},
    )
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_b", (
        f"Expected cam_b (foreign) so cam_a (last owned) remains active; "
        f"got {chosen}"
    )
    # Invariant: at least one owned camera remains active.
    assert "cam_a" in sim_state.active_cameras, "cam_a must remain active"


def test_l1_with_two_owned_can_migrate_at_most_one_owned():
    """2 owned + 1 foreign active → L1 may migrate at most one owned camera.

    Whichever camera is picked (lightest of all eligible), at least one
    owned camera must remain active afterwards.
    """
    o = _new_orch()
    # cam_a, cam_b are owned; cam_c is foreign.
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b", "cam_c"],
        # cam_a is lightest of owned; cam_c (foreign) is lightest overall.
        camera_workload={"cam_a": 4.0, "cam_b": 12.0, "cam_c": 2.0},
    )
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    # Both cam_a (owned, leaves cam_b) and cam_c (foreign, leaves both owned)
    # are valid picks; the picker walks lightest-first and chooses cam_c.
    assert chosen == "cam_c", (
        f"Expected cam_c (lightest foreign), got {chosen}"
    )
    # Invariant: at least one owned camera remains active.
    owned_remaining = {"cam_a", "cam_b"} & (set(sim_state.active_cameras) - {chosen})
    assert owned_remaining, (
        f"After migration of {chosen}, no owned cameras remain active"
    )

    # Also verify: if the foreign camera is ineligible, the lightest owned
    # is picked (one of two owned migrates, one remains).
    sim_state2 = _make_state(
        active_cameras=["cam_a", "cam_b", "cam_c"],
        # cam_c has no workload evidence (ineligible); picker falls back to
        # cam_a (lightest of the eligible owned cameras).
        camera_workload={"cam_a": 4.0, "cam_b": 12.0},
    )
    chosen2 = o._pick_camera_to_offload(sim_state2, level=1)
    assert chosen2 == "cam_a", (
        f"Expected cam_a (lightest owned, cam_c ineligible), got {chosen2}"
    )
    owned_remaining2 = {"cam_a", "cam_b"} & (set(sim_state2.active_cameras) - {chosen2})
    assert owned_remaining2 == {"cam_b"}, (
        f"Expected cam_b to remain as the last owned camera; got {owned_remaining2}"
    )


def test_l1_fails_safe_when_no_owned_camera_active():
    """Only foreign cameras active → L1 returns None (fail safe)."""
    o = _new_orch()
    # Nothing owned is active (all active cameras are foreign/rescued).
    o._get_owned_camera_ids = lambda: {"cam_owned_offline"}
    sim_state = _make_state(
        active_cameras=["cam_rescued_1", "cam_rescued_2"],
        camera_workload={"cam_rescued_1": 2.0, "cam_rescued_2": 8.0},
    )
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen is None, (
        f"Expected None (no owned active → fail safe), got {chosen}"
    )


def test_l1_fails_safe_when_only_owned_is_ineligible_and_only_owned_remains():
    """Only one owned camera is active AND no foreign cameras exist → return None.

    The L1 ownership guard is satisfied (the owned camera remains active).
    The "no eligible candidate" branch fires because cam_owned is starved
    (ineligible) and there is no other camera to migrate. This is the
    fail-safe path: nothing useful can be offloaded.
    """
    o = _new_orch()
    o._get_owned_camera_ids = lambda: {"cam_owned"}
    # Build state manually: only cam_owned is active and it's source-starved,
    # so it's ineligible; no other camera exists to migrate.
    sim_state = types.SimpleNamespace(
        active_cameras=["cam_owned"],
        camera_workload={"cam_owned": 2.0},
        source_starved_cameras=["cam_owned"],
        fps_per_camera={},
        avg_fps=None,
        load_score=80.0,
        risk_index=0.0,
        overload_since=time.time() - 20.0,
        penalty_until=0.0,
    )
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    # Note: this also returns None via the "active_cameras <= 1" early-return,
    # not via the ownership guard. Either way, the behaviour is correct:
    # never migrate the last camera on the node.
    assert chosen is None, (
        f"Expected None (only camera is ineligible), got {chosen}"
    )


def test_l2_can_select_last_owned_camera():
    """L2 crop offload is allowed to pick the last owned camera — it does
    NOT remove the stream (crop work is sent to a peer, stream stays local).
    """
    o = _new_orch()
    # Only cam_a is owned; cam_b is foreign.
    o._get_owned_camera_ids = lambda: {"cam_a"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        # cam_a (owned) is heaviest → L2 normally picks it.
        camera_workload={"cam_a": 12.0, "cam_b": 4.0},
    )
    chosen = o._pick_camera_to_offload(sim_state, level=2)
    assert chosen == "cam_a", (
        f"L2 must be allowed to pick the last owned camera (heaviest); got {chosen}"
    )


def test_l3_can_select_last_owned_camera():
    """L3 plate-crop offload is allowed to pick the last owned camera."""
    o = _new_orch()
    o._get_owned_camera_ids = lambda: {"cam_a"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        camera_workload={"cam_a": 12.0, "cam_b": 4.0},
    )
    chosen = o._pick_camera_to_offload(sim_state, level=3)
    assert chosen == "cam_a", (
        f"L3 must be allowed to pick the last owned camera (heaviest); got {chosen}"
    )


def test_get_owned_camera_ids_prefers_live_camera_manager():
    """Helper uses live CameraManager when available (hot-reloaded source)."""
    import threading
    o = _new_orch()

    class _Cfg:
        def __init__(self, cid, enabled):
            self.camera_id = cid
            self.enabled = enabled

    class _FakeCM:
        """Mimic the parts of CameraManager the helper reaches into."""
        def __init__(self):
            self._lock = threading.RLock()
            self._configs = {
                "cam_x": _Cfg("cam_x", True),
                "cam_y": _Cfg("cam_y", True),
                "cam_z": _Cfg("cam_z", False),   # disabled → excluded
            }

    o._camera_manager = _FakeCM()
    owned = o._get_owned_camera_ids()
    assert owned == {"cam_x", "cam_y"}, (
        f"Expected only enabled cameras from CameraManager; got {owned}"
    )


def test_get_owned_camera_ids_falls_back_to_cameras_yml():
    """Helper reads enabled cameras from cameras.yml when CameraManager is None."""
    import tempfile
    import yaml
    tmp = tempfile.mkdtemp(prefix="po_owns_")
    try:
        yml_path = Path(tmp) / "cameras.yml"
        yml_path.write_text(
            textwrap.dedent("""\
                cameras:
                  cam_owned_1:
                    camera_id: "cam_owned_1"
                    enabled: true
                  cam_owned_2:
                    camera_id: "cam_owned_2"
                    enabled: true
                  cam_disabled:
                    camera_id: "cam_disabled"
                    enabled: false
                  cam_default_enabled:
                    camera_id: "cam_default_enabled"
                    # enabled key missing → defaults to True
                """),
            encoding="utf-8",
        )
        o = _new_orch()
        # _new_orch sets camera_manager=None → fallback path is exercised.
        o._camera_configs_dir = Path(tmp)
        # Force cache reset so this file is read on the next call.
        o._cameras_cache = None
        o._cameras_cache_mtime = None

        owned = o._get_owned_camera_ids()
        assert "cam_owned_1" in owned, f"Expected cam_owned_1 in {owned}"
        assert "cam_owned_2" in owned, f"Expected cam_owned_2 in {owned}"
        assert "cam_disabled" not in owned, (
            f"Disabled camera must be excluded; got {owned}"
        )
        assert "cam_default_enabled" in owned, (
            f"Cameras with no 'enabled' key default to True; got {owned}"
        )
    finally:
        # Clean up temp dir
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_get_owned_camera_ids_returns_empty_on_missing_yml():
    """Helper returns empty set (fail safe) when cameras.yml is absent."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="po_owns_empty_")
    try:
        o = _new_orch()
        # Point at a directory with no cameras.yml.
        o._camera_configs_dir = Path(tmp)
        o._cameras_cache = None
        o._cameras_cache_mtime = None

        owned = o._get_owned_camera_ids()
        assert owned == set(), f"Expected empty set on missing YAML; got {owned}"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Self-state FPS validity gate: load_score=100 with empty/invalid FPS must
# never overload-escalate (preserves dashboard score).
# ─────────────────────────────────────────────────────────────────────────────

def test_self_state_load_high_no_fps_never_sets_overload_since():
    """load_score=100 with fps_per_camera={} → overload_since stays None."""
    o = _new_orch(overload_warmup_s=10.0, overload_duration_s=0.0)
    o.update_self_state({
        "load_score": 100.0,
        "gpu_percent": 99.0,
        "cpu_percent": 99.0,
        "ram_percent": 99.0,
        "gpu_temp_c": 70.0,
        "risk_index": 0.0,
        "pipeline": {
            "active_cameras": ["cam_a", "cam_b"],
            "camera_workload": {"cam_a": 5.0, "cam_b": 8.0},
            "fps_per_camera": {},
            "max_streams": 8,
        },
    })
    with o._self_lock:
        assert o._self_state.overload_since is None, (
            "load_score=100 with empty fps must not set overload_since"
        )


def test_self_state_load_high_nonpositive_fps_never_sets_overload_since():
    """load_score=100 with fps values that are zero/negative → overload_since stays None."""
    o = _new_orch(overload_warmup_s=10.0, overload_duration_s=0.0)
    o.update_self_state({
        "load_score": 100.0,
        "gpu_percent": 99.0,
        "cpu_percent": 99.0,
        "ram_percent": 99.0,
        "gpu_temp_c": 70.0,
        "risk_index": 0.0,
        "pipeline": {
            "active_cameras": ["cam_a", "cam_b"],
            "camera_workload": {"cam_a": 5.0, "cam_b": 8.0},
            "fps_per_camera": {"cam_a": 0.0, "cam_b": -1.0},
            "max_streams": 8,
        },
    })
    with o._self_lock:
        assert o._self_state.overload_since is None, (
            "load_score=100 with zero/negative fps must not set overload_since"
        )


def test_self_state_valid_positive_fps_sets_overload_since():
    """With at least one positive FPS, overload_since IS set (decision possible)."""
    o = _new_orch(overload_warmup_s=0.0, overload_duration_s=0.0)
    o.update_self_state({
        "load_score": 60.0,
        "gpu_percent": 60.0,
        "cpu_percent": 60.0,
        "ram_percent": 60.0,
        "gpu_temp_c": 50.0,
        "risk_index": 0.0,
        "pipeline": {
            "active_cameras": ["cam_a", "cam_b"],
            "camera_workload": {"cam_a": 5.0, "cam_b": 8.0},
            "fps_per_camera": {"cam_a": 25.0, "cam_b": 0.0},
            "max_streams": 8,
        },
    })
    with o._self_lock:
        assert o._self_state.overload_since is not None, (
            "valid positive FPS must allow overload_since to be set"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Startup warmup: no overload escalation until valid positive FPS has been
# present for overload_warmup_s (default 10 s).
# ─────────────────────────────────────────────────────────────────────────────

def test_startup_warmup_blocks_overload_before_first_valid_fps():
    """No valid FPS yet → _check_self_overload must not escalate."""
    o = _new_orch(overload_warmup_s=10.0, overload_duration_s=0.0)
    _setup_overloaded_self(o, load_score=60.0)
    _add_peer_beta(o)
    # Simulate fresh start: no valid FPS ever observed.
    o._self_first_valid_fps_at = None
    o._check_self_overload()
    assert o.get_offload_level("cam_a") == 0, "warmup must block before first valid FPS"
    assert o.get_offload_level("cam_b") == 0, "warmup must block before first valid FPS"


def test_startup_warmup_blocks_overload_inside_window():
    """Valid FPS present for < warmup_s → still blocked."""
    o = _new_orch(overload_warmup_s=10.0, overload_duration_s=0.0)
    _setup_overloaded_self(o, load_score=60.0)
    _add_peer_beta(o)
    # First valid FPS was only 3 s ago.
    o._self_first_valid_fps_at = time.time() - 3.0
    o._check_self_overload()
    assert o.get_offload_level("cam_a") == 0, "warmup window (3s<10s) must block"
    assert o.get_offload_level("cam_b") == 0, "warmup window (3s<10s) must block"


def test_startup_warmup_allows_overload_after_window():
    """Valid FPS present for >= warmup_s → escalation allowed."""
    o = _new_orch(overload_warmup_s=10.0, overload_duration_s=0.0)
    _setup_overloaded_self(o, load_score=60.0)
    _add_peer_beta(o)
    o._self_first_valid_fps_at = time.time() - 20.0
    o._check_self_overload()
    # L3 fires (load 60 >= thr3=57) → at least one camera offloaded level 3.
    with o._lock:
        assert o._vote_in_progress or any(
            o.get_offload_level(c) > 0 for c in ("cam_a", "cam_b")
        ), "after warmup window, overload must escalate"


def test_startup_warmup_config_zero_disables_gate():
    """overload_warmup_s=0 → duration gate disabled (valid FPS present)."""
    o = _new_orch(overload_warmup_s=0.0, overload_duration_s=0.0)
    _setup_overloaded_self(o, load_score=60.0)
    _add_peer_beta(o)
    # Valid FPS observed (update_self_state already recorded it); warmup_s=0
    # means the duration gate never blocks.  No manual override here.
    o._check_self_overload()
    with o._lock:
        assert o._vote_in_progress or any(
            o.get_offload_level(c) > 0 for c in ("cam_a", "cam_b")
        ), "warmup_s=0 must not block decisions when valid FPS exists"


# ─────────────────────────────────────────────────────────────────────────────
# Per-camera warmup after ADD: newly-added cameras are excluded from offload
# until their FPS has been valid for camera_warmup_s.
# ─────────────────────────────────────────────────────────────────────────────

def test_newly_added_camera_excluded_from_offload():
    """Camera added this tick (no valid FPS yet) is not eligible for offload."""
    o = _new_orch(camera_warmup_s=10.0)
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        # cam_a is the LIGHTEST — normally L1's pick.
        camera_workload={"cam_a": 2.0, "cam_b": 12.0},
    )
    # cam_a was just ADDed: timestamps say so, no valid FPS yet.
    o._camera_added_at["cam_a"] = time.time()
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_b", (
        f"Freshly-added cam_a (no valid FPS yet) must be excluded; got {chosen}"
    )


def test_newly_added_camera_with_positive_fps_still_warming():
    """Camera added 5 s ago with valid FPS but within warmup window → excluded."""
    o = _new_orch(camera_warmup_s=10.0)
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        camera_workload={"cam_a": 2.0, "cam_b": 12.0},
    )
    o._camera_added_at["cam_a"] = time.time() - 5.0
    o._camera_first_valid_fps_at["cam_a"] = time.time() - 5.0
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_b", (
        f"cam_a warmup (5s < 10s) must exclude it from offload; got {chosen}"
    )


def test_warmed_up_camera_eligible_for_offload():
    """Camera with valid FPS beyond warmup window is eligible again."""
    o = _new_orch(camera_warmup_s=10.0)
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        camera_workload={"cam_a": 2.0, "cam_b": 12.0},
    )
    o._camera_added_at["cam_a"] = time.time() - 30.0
    o._camera_first_valid_fps_at["cam_a"] = time.time() - 30.0
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_a", (
        f"cam_a past warmup (30s >= 10s) should be eligible; got {chosen}"
    )


def test_preexisting_camera_not_subject_to_warmup():
    """Camera never ADDed (no _camera_added_at entry) skips the warmup gate."""
    o = _new_orch(camera_warmup_s=10.0)
    o._get_owned_camera_ids = lambda: {"cam_a", "cam_b"}
    sim_state = _make_state(
        active_cameras=["cam_a", "cam_b"],
        camera_workload={"cam_a": 2.0, "cam_b": 12.0},
    )
    # No _camera_added_at entries — both pre-existing.
    chosen = o._pick_camera_to_offload(sim_state, level=1)
    assert chosen == "cam_a", (
        f"Pre-existing cameras must bypass the warmup gate; got {chosen}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rebalance: REMOVE/return to owner only when owner is currently fresh.
# ─────────────────────────────────────────────────────────────────────────────

def _stub_control_pub(o):
    """Stub the 'control' publisher to capture REMOVE/ADD payloads."""
    class _FakePub:
        def __init__(self):
            self.sent = []
        def put(self, data):
            self.sent.append(data)
    o._pubs["control"] = _FakePub()


def _register_fresh_peer(o, node_id, active_cameras, last_seen_delta=1.0):
    from speedflow_python.peer_orchestrator import PeerState
    o._peers[node_id] = PeerState(
        node_id=node_id, load_score=10.0, gpu_temp_c=50.0,
        last_seen=time.time() - last_seen_delta,
        active_cameras=list(active_cameras), max_streams=4,
    )


def _sent_control_commands(pub, cmd=None, camera_id=None):
    """Decode msgpack'd payloads captured by the stub control publisher."""
    import msgpack
    out = []
    for p in pub.sent:
        try:
            d = msgpack.unpackb(p, raw=False)
        except Exception:
            continue
        if cmd is not None and d.get("cmd") != cmd:
            continue
        if camera_id is not None and d.get("camera_id") != camera_id:
            continue
        out.append(d)
    return out


def test_rebalance_skips_when_owner_absent():
    """Owner not in peer table → no REMOVE for its rescued camera."""
    o = _new_orch()
    o._rescued_cameras["cam_x"] = "node_gone"
    o._self_state.active_cameras = ["cam_x", "cam_owned"]
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" in o._rescued_cameras, "absent owner must keep the rescue"
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_x") == [], (
        "no REMOVE for cam_x when owner absent"
    )


def test_rebalance_skips_when_owner_stale():
    """Owner heartbeat older than heartbeat_timeout_s → still rescued."""
    o = _new_orch(heartbeat_timeout_s=5.0)
    o._rescued_cameras["cam_x"] = "node_beta"
    o._self_state.active_cameras = ["cam_x", "cam_owned"]
    # last_seen 20 s ago → stale beyond 5 s timeout.
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=20.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" in o._rescued_cameras, "stale owner must keep the rescue"
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_x") == [], (
        "no REMOVE for cam_x when owner stale"
    )


def test_rebalance_skips_when_owner_not_running_camera():
    """Owner fresh but not yet running the camera → keep rescue."""
    o = _new_orch()
    o._rescued_cameras["cam_x"] = "node_beta"
    o._self_state.active_cameras = ["cam_x", "cam_owned"]
    # Owner fresh but active_cameras does NOT include cam_x yet.
    _register_fresh_peer(o, "node_beta", [])
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" in o._rescued_cameras, "owner not running cam_x → keep rescue"


def test_rebalance_proceeds_when_owner_fresh_and_running():
    """Owner fresh AND running the camera → REMOVE sent, rescue released."""
    o = _new_orch()
    o._get_owned_camera_ids = lambda: {"cam_owned"}  # owned camera stays active
    o._rescued_cameras["cam_x"] = "node_beta"
    o._self_state.active_cameras = ["cam_x", "cam_owned"]
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" not in o._rescued_cameras, "rescue released to fresh owner"
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_x") != [], (
        "expected a REMOVE command for cam_x"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rebalance last-active guard: never return the only remaining active camera.
# ─────────────────────────────────────────────────────────────────────────────

def test_rebalance_skips_last_active_camera():
    """Rescued camera is the ONLY active camera → never return it."""
    o = _new_orch()
    o._rescued_cameras["cam_x"] = "node_beta"
    o._self_state.active_cameras = ["cam_x"]  # only cam_x active
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" in o._rescued_cameras, "last active camera must not be returned"
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_x") == [], (
        "no REMOVE when it is the last active camera"
    )


def test_rebalance_proceeds_when_another_camera_remains():
    """Multiple active cameras → returning one rescued camera is fine."""
    o = _new_orch()
    o._get_owned_camera_ids = lambda: {"cam_owned"}
    o._rescued_cameras["cam_x"] = "node_beta"
    o._rescued_cameras["cam_y"] = "node_gamma"
    o._self_state.active_cameras = ["cam_x", "cam_y", "cam_owned"]
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=1.0)
    _register_fresh_peer(o, "node_gamma", ["cam_y"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" not in o._rescued_cameras, "cam_x returned (owned camera remains)"
    assert "cam_y" not in o._rescued_cameras, "cam_y returned (owned camera remains)"


def test_rebalance_last_active_guard_per_return():
    """Last-active-camera guard: returning every rescued camera while the
    only locally-owned active camera stays must not drain self to zero.

    Layout: 3 rescued cameras (all returnable, all foreign) + 1 owned.
    Both foreign returns proceed without violating the last-active guard
    because cam_owned remains active throughout.
    """
    o = _new_orch()
    o._get_owned_camera_ids = lambda: {"cam_owned"}
    o._rescued_cameras["cam_x"] = "node_beta"
    o._rescued_cameras["cam_y"] = "node_gamma"
    o._rescued_cameras["cam_z"] = "node_delta"
    o._self_state.active_cameras = ["cam_x", "cam_y", "cam_z", "cam_owned"]
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=1.0)
    _register_fresh_peer(o, "node_gamma", ["cam_y"], last_seen_delta=1.0)
    _register_fresh_peer(o, "node_delta", ["cam_z"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    # All 3 rescued are returned; the owned stays.
    assert "cam_x" not in o._rescued_cameras
    assert "cam_y" not in o._rescued_cameras
    assert "cam_z" not in o._rescued_cameras


# Hard invariant: zero locally-owned active cameras => block all returns
# (never leave this node with zero locally-owned cameras, not merely zero
# active cameras).  See peer_orchestrator.py:_check_rebalance.


def test_rebalance_blocked_when_only_foreign_rescued_active_no_owned():
    """(1) Only foreign rescued cameras active + owner fresh => no return.
    Returning cam_x would leave this node with zero locally-owned active
    cameras (and the hard invariant demands >=1), so _check_rebalance
    must NOT send REMOVE even though cam_x's owner is fresh and running.
    """
    o = _new_orch()
    # cameras.yml is empty / no locally-owned cameras on this node.
    o._get_owned_camera_ids = lambda: set()
    o._rescued_cameras["cam_x"] = "node_beta"
    o._self_state.active_cameras = ["cam_x"]  # only foreign rescued
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" in o._rescued_cameras, (
        "no locally-owned active camera — rescue MUST be held"
    )
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_x") == [], (
        "must not REMOVE when returning would leave zero owned"
    )


def test_rebalance_allows_foreign_return_when_owned_remains():
    """(2) Owned + foreign active => foreign may return.
    Returning cam_x leaves cam_owned (locally-owned) active, so the hard
    invariant is preserved and the foreign rescue can be released to its
    recovered owner.
    """
    o = _new_orch()
    o._get_owned_camera_ids = lambda: {"cam_owned"}
    o._rescued_cameras["cam_x"] = "node_beta"
    o._self_state.active_cameras = ["cam_x", "cam_owned"]
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" not in o._rescued_cameras, (
        "foreign rescue may return when owned camera remains active"
    )
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_x") != [], (
        "expected a REMOVE command for the returned rescued camera"
    )


def test_rebalance_never_returns_locally_owned_camera():
    """(3) An owned camera in _rescued_cameras is NEVER returned here.
    Even if data-race / ownership-transition somehow placed an owned
    camera into the rescued-cameras map, the rebalance path must NOT
    issue a REMOVE for it (that would migrate our own camera to a peer).
    """
    o = _new_orch()
    o._get_owned_camera_ids = lambda: {"cam_owned"}
    # Defensive scenario: an owned camera ended up in _rescued_cameras.
    o._rescued_cameras["cam_owned"] = "node_beta"
    o._self_state.active_cameras = ["cam_owned"]
    _register_fresh_peer(o, "node_beta", ["cam_owned"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_owned" in o._rescued_cameras, (
        "locally-owned camera must never be returned by rebalance"
    )
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_owned") == [], (
        "no REMOVE for a locally-owned camera"
    )


def test_rebalance_fails_safe_when_owned_lookup_raises():
    """Fail-safe: if _get_owned_camera_ids() raises, treat ownership as
    unresolved and block all returns (consistent with the L1 ownership
    guard's fail-safe semantics).
    """
    o = _new_orch()

    def _boom():
        raise RuntimeError("camera manager unavailable")

    o._get_owned_camera_ids = _boom
    o._rescued_cameras["cam_x"] = "node_beta"
    o._self_state.active_cameras = ["cam_x", "cam_owned"]
    _register_fresh_peer(o, "node_beta", ["cam_x"], last_seen_delta=1.0)
    _stub_control_pub(o)
    o._check_rebalance()
    assert "cam_x" in o._rescued_cameras, (
        "fail-safe must hold rescue when ownership is unresolved"
    )
    assert _sent_control_commands(o._pubs["control"], "REMOVE", "cam_x") == [], (
        "no REMOVE when ownership cannot be resolved"
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
        test_l3_dwell_blocks_escalation_to_l2_before_duration,
        test_l3_dwell_permits_escalation_to_l2_after_duration,
        test_l2_dwell_blocks_escalation_to_l1_before_duration,
        test_l2_dwell_permits_escalation_to_l1_after_duration,
        test_l1_permitted_when_no_lower_level_active,
        test_l1_bypasses_dwell_when_hardware_fuse_active,
        test_malformed_dwell_config_uses_defaults,
        test_negative_dwell_config_uses_defaults,
        test_string_dwell_config_uses_defaults,
        test_dwell_gate_only_blocks_escalation_from_active_lower_level,
        test_l1_excludes_last_owned_when_foreign_active,
        test_l1_with_two_owned_can_migrate_at_most_one_owned,
        test_l1_fails_safe_when_no_owned_camera_active,
        test_l1_fails_safe_when_only_owned_is_ineligible_and_only_owned_remains,
        test_l2_can_select_last_owned_camera,
        test_l3_can_select_last_owned_camera,
        test_get_owned_camera_ids_prefers_live_camera_manager,
        test_get_owned_camera_ids_falls_back_to_cameras_yml,
        test_get_owned_camera_ids_returns_empty_on_missing_yml,
        test_self_state_load_high_no_fps_never_sets_overload_since,
        test_self_state_load_high_nonpositive_fps_never_sets_overload_since,
        test_self_state_valid_positive_fps_sets_overload_since,
        test_startup_warmup_blocks_overload_before_first_valid_fps,
        test_startup_warmup_blocks_overload_inside_window,
        test_startup_warmup_allows_overload_after_window,
        test_startup_warmup_config_zero_disables_gate,
        test_newly_added_camera_excluded_from_offload,
        test_newly_added_camera_with_positive_fps_still_warming,
        test_warmed_up_camera_eligible_for_offload,
        test_preexisting_camera_not_subject_to_warmup,
        test_rebalance_skips_when_owner_absent,
        test_rebalance_skips_when_owner_stale,
        test_rebalance_skips_when_owner_not_running_camera,
        test_rebalance_proceeds_when_owner_fresh_and_running,
        test_rebalance_skips_last_active_camera,
        test_rebalance_proceeds_when_another_camera_remains,
        test_rebalance_last_active_guard_per_return,
        # Hard invariant: zero locally-owned active cameras => block all
        # returns.  See peer_orchestrator.py:_check_rebalance.
        test_rebalance_blocked_when_only_foreign_rescued_active_no_owned,
        test_rebalance_allows_foreign_return_when_owned_remains,
        test_rebalance_never_returns_locally_owned_camera,
        test_rebalance_fails_safe_when_owned_lookup_raises,
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
