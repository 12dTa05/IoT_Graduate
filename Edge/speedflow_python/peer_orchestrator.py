"""
Edge/speedflow_python/peer_orchestrator.py

Peer Orchestrator — P2P brain, replaces MasterOrchestrator.

Each Edge Node runs an independent PeerOrchestrator instance.
Instances communicate via Zenoh key expressions:
  - peers/status/<node_id>  ← heartbeat from all peers
  - peers/vote/request      ← RFO (Request for Offload) from overloaded peer
  - peers/vote/proposal     ← bid from capable peer
  - peers/vote/decision     ← election result
  - peers/vote/ack/{cam}    ← confirmation that stream is PLAYING

Migration uses Make-before-Break strategy:
  1. Requester opens vote window → collects proposals (3s)
  2. Select winner = proposal with lowest F(x) (ε-constraint)
  3. Publish decision → winner auto-ADD camera to pipeline
  4. Winner publishes peers/vote/ack/{cam} when stream PLAYING
  5. Requester receives ack → REMOVE camera from its pipeline
"""

from __future__ import annotations

import csv
import hashlib
import logging
import math
import os
import random
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import msgpack

from .zenoh_session import make_session

# Settings loaded from Edge/.env
from .settings import ROOT as _ROOT

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("peer_orchestrator")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PeerState:
    """Current state of a Peer Node (replaces NodeState)."""
    node_id: str
    load_score: float = 0.0
    gpu_percent: float = 0.0
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    gpu_temp_c: Optional[float] = 0.0
    avg_fps: Optional[float] = None
    fps_per_camera: Dict[str, float] = field(default_factory=dict)
    active_cameras: List[str] = field(default_factory=list)
    camera_configs: Dict[str, dict] = field(default_factory=dict)
    max_streams: int = 8
    last_seen: float = field(default_factory=time.time)
    overload_since: Optional[float] = None
    penalty_until: float = 0.0
    # Proactive model output — populated when proactive.enabled is True.
    # Defaults to 0.0 (no risk) so legacy comparisons (load_score only) are unaffected.
    risk_index: float = 0.0
    # Per-camera workload (n_track + n_plate) from health payload.
    # L1 offload picks min workload; L2/L3 pick max workload.
    # Empty dict is the safe default when payload is missing/malformed.
    camera_workload: Dict[str, float] = field(default_factory=dict)
    # Camera IDs reported as source-starved by the health agent.
    # These are excluded from offload candidate selection (fail safe).
    source_starved_cameras: List[str] = field(default_factory=list)


def _parse_camera_workload(raw) -> Dict[str, float]:
    """Parse camera_workload from a health payload; malformed → {}.

    Accepts only finite, non-negative numeric values keyed by str camera_id.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for k, v in raw.items():
        if (isinstance(k, str)
                and isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                and v >= 0):
            out[k] = float(v)
    return out


def _parse_starved_cameras(raw) -> List[str]:
    """Parse source_starved_cameras from a health payload; malformed → []."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [c for c in raw if isinstance(c, str)]


def _pick_fps_dict(pipeline: dict) -> dict:
    """Return the canonical per-camera FPS dict from a pipeline section.

    Phase 1 added ``output_fps_per_camera`` as the unambiguous name for
    pipeline throughput FPS.  ``fps_per_camera`` is kept for backward
    compatibility.  Prefer the new name; fall back to the legacy key so
    peers on older firmware still work.
    """
    if not isinstance(pipeline, dict):
        return {}
    out = pipeline.get("output_fps_per_camera")
    if isinstance(out, dict):
        return out
    fallback = pipeline.get("fps_per_camera", {})
    return fallback if isinstance(fallback, dict) else {}


def _has_valid_positive_fps(fps_per_camera) -> bool:
    """Return True if fps_per_camera has at least one finite, positive (>0) value.

    ponytail: gates overload decisions on the presence of *real* FPS evidence.
    Confirmed from Jetson logs: a freshly-started pipeline reports load_score=100
    while fps_per_camera is still empty (or contains only 0/NaN) — the load
    score is meaningless without running streams and must NOT escalate the
    node into RFO.  Dashboard keeps the raw load_score; only the decision path
    is gated, preserving what the operator sees.
    """
    if not fps_per_camera or not isinstance(fps_per_camera, dict):
        return False
    for v in fps_per_camera.values():
        if (isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                and v > 0.0):
            return True
    return False


def _dwell_s(cfg: dict, key: str, default: float) -> float:
    """Read a dwell duration from config; malformed/missing → default (fail-safe)."""
    raw = cfg.get(key, default)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return default
    if not math.isfinite(float(raw)) or float(raw) < 0.0:
        return default
    return float(raw)


def _thermal_admission_ok(gpu_temp_c, therm_cfg) -> bool:
    """
    Shared thermal-admission gate for BOTH roles.

    Used by the sender side (_pick_best_peer, checking a peer before
    offloading to it) and the receiver side (_evaluate_and_bid, checking
    this node's own temperature before bidding on an RFO), so the rules
    cannot drift apart. Config is the `p2p.thermal` section of
    Edge/configs/edge_node.yml.

    Returns True when the (peer or self) node may accept offload work.
    An absent thermal section leaves behaviour unchanged (always accept).
    """
    if not therm_cfg:
        return True
    if not therm_cfg.get("admission_enabled", True):
        return True
    max_t = float(therm_cfg.get("max_gpu_temp_c", 85.0))
    if gpu_temp_c is None or not isinstance(gpu_temp_c, (int, float)):
        # Unknown / missing measurement. Default (conservative) policy
        # rejects so we never send work to a node whose thermal state is
        # unknown; permissive accepts on the assumption it is otherwise
        # healthy. Both behaviours are explicit config.
        policy = therm_cfg.get("invalid_policy", "conservative")
        if policy == "permissive":
            return True  # accept despite missing data
        # reject_invalid=True → reject (return False)
        # reject_invalid=False → accept (return True)
        return not bool(therm_cfg.get("reject_invalid", True))
    if gpu_temp_c <= 0:
        # Zero or negative is almost certainly a sensor failure
        policy = therm_cfg.get("invalid_policy", "conservative")
        return policy != "conservative"
    if gpu_temp_c > max_t:
        return False
    return True


# ---------------------------------------------------------------------------
# Migration Log
# ---------------------------------------------------------------------------

class MigrationLogger:
    """Log each migration to a CSV file — copied from master_orchestrator.py."""

    HEADER = [
        "timestamp_iso", "from_node", "to_node", "camera_id",
        "trigger_reason", "trigger_load", "trigger_fps",
        "migration_time_ms", "result", "blind_spot_ms",
    ]

    def __init__(self, log_file: Path) -> None:
        self._path = log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if not log_file.exists():
            with open(log_file, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADER)

    def log(
        self,
        from_node: str,
        to_node: str,
        camera_id: str,
        trigger_reason: str,
        trigger_load: float,
        trigger_fps: Optional[float],
        migration_time_ms: float,
        result: str,
        blind_spot_ms: Optional[float] = None,
    ) -> None:
        row = [
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            from_node, to_node, camera_id,
            trigger_reason,
            round(trigger_load, 1),
            round(trigger_fps, 1) if trigger_fps is not None else "",
            round(migration_time_ms, 0),
            result,
            round(blind_spot_ms, 0) if blind_spot_ms is not None else "",
        ]
        try:
            with open(self._path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
        except Exception as exc:
            logger.warning("MigrationLogger write error: %s", exc)


# ---------------------------------------------------------------------------
# Peer Orchestrator
# ---------------------------------------------------------------------------

class PeerOrchestrator:
    """
    P2P version of MasterOrchestrator — runs on each Edge Node.

    Communicates via Zenoh (peer mode, key expressions).
    """

    def __init__(
        self,
        node_id: str,
        cfg: dict,
        camera_manager: object,
        camera_configs_dir: Optional[Path] = None,
    ) -> None:
        self._node_id = node_id
        self._cfg = cfg
        self._camera_manager = camera_manager

        # Peer state
        self._peers: Dict[str, PeerState] = {}
        self._lock = threading.RLock()

        # State of this node itself (updated from our own peers/status/+)
        self._self_state = PeerState(node_id=node_id)
        # BUG-1 fix: separate lock for _self_state so the Zenoh callback
        # thread and the decision loop never see a torn write.
        self._self_lock = threading.RLock()

        # Migration log — relative to Edge/logs/
        log_dir = _ROOT / "logs"
        self._migration_log = MigrationLogger(log_dir / "p2p_migrations.csv")

        # Cooldown per-camera: camera_id → timestamp of most recent migration
        self._cam_cooldown: Dict[str, float] = {}

        # Vote windows: camera_id → list[proposal]
        self._vote_windows: Dict[str, List[dict]] = {}
        self._vote_timers: Dict[str, threading.Timer] = {}
        # Cameras with RFO sent but vote window still open (prevent re-trigger)
        self._vote_in_progress: set = set()

        # Pending ack events for Make-before-Break
        self._pending_acks: Dict[str, threading.Event] = {}

        # Camera config lookup — relative to Edge/configs/
        if camera_configs_dir is None:
            camera_configs_dir = _ROOT / "configs"
        self._camera_configs_dir = camera_configs_dir

        # Zenoh session + publishers
        self._session = None
        self._pubs: dict = {}
        self._running = False
        self._decision_thread: Optional[threading.Thread] = None

        # Track peers already reported offline (no cameras) to avoid log spam
        self._notified_offline: set = set()

        # BUG-6 fix: track which dead peers have already had failover triggered
        # so we don't re-trigger on the next decision-loop tick.  We no longer
        # clear active_cameras on the dead peer's PeerState (that would race
        # with _check_rebalance reading it to detect camera returns).
        self._failover_triggered: set = set()

        # Cameras rescued via failover: camera_id → original_owner_node_id
        # Used to return cameras when the original owner comes back online.
        self._rescued_cameras: Dict[str, str] = {}

        # Cameras migrated away due to overload: camera_id → winner_node_id
        # Used to reclaim cameras when this node's load drops below threshold.
        self._migrated_out: Dict[str, str] = {}

        # Phase 3 — bounded in-flight reservation accounting.
        #
        # Sender side: _peer_inflight[peer_node_id] counts how many L1 stream
        # migrations have been decided (decision published) toward that peer but
        # whose ADD ack has not yet arrived.  _pick_best_peer adds this to
        # len(peer.active_cameras) before applying the capacity gate so two
        # simultaneous RFOs cannot both pick the same already-full peer.
        # Decremented on ACK (stream PLAYING) or on migration timeout (rollback).
        # ponytail: per-peer int rather than per-camera set — O(1), no camera-id
        # coupling, naturally collapses when the reservation resolves.
        self._peer_inflight: Dict[str, int] = {}

        # Phase 3 — sender-side camera→winner mapping used to decrement
        # _peer_inflight on ACK or timeout.  Populated in _close_vote_window;
        # cleared in _on_vote_ack (ACK path) and _wait_and_remove (timeout path).
        self._pending_winner: Dict[str, str] = {}

        # Receiver side: count of bids sent but whose ADD command has not yet
        # arrived.  ε1 in _evaluate_and_bid gates on
        # current_streams + _self_inflight >= eps_streams_max so a node with 3
        # streams that has already bid on one RFO won't bid on a second and
        # overflow capacity.  Decayed by a timer (vote_window_s +
        # migration_timeout_s) since the ADD command goes to ZenohCommandSubscriber
        # rather than back through this orchestrator — timeout-only decay is the
        # conservative safe path.
        self._self_inflight: int = 0
        self._self_inflight_lock = threading.Lock()  # separate from _lock to avoid nesting

        # Reclaim post-return observation window: camera_id → timestamp of reclaim
        # completion.  Prevents the reclaimed camera from being immediately
        # migrated away again during its transient FPS warm-up window on this node.
        self._reclaim_completed_at: Dict[str, float] = {}

        # ponytail: rate-limit blocked-decision diagnostics so they don't spew
        # every 1-second tick.  Keys are short reason strings; value is the Unix
        # timestamp of the last log.  Cooldown = 15 s (half a typical vote window);
        # increase to 30 s if log volume is still too high.
        self._blocked_logged_at: Dict[str, float] = {}

        # ponytail: single settle deadline that suppresses ALL L3/L2/L1 overload
        # actions after a migration completes or reclaim ADD is initiated.
        # Stale/draining FPS samples can otherwise escalate the node during the
        # transition.  Configurable via p2p.transition_settle_s (default 5.0 s).
        self._transition_settle_until: float = 0.0

        # Timestamp when load first dropped below reclaim threshold (for stability check)
        self._reclaim_eligible_since: Optional[float] = None

        # Penalty timestamp for this node itself (set on migration timeout rollback)
        self._self_penalty_until: float = 0.0

        # ponytail: track when self FIRST observed valid positive FPS (startup
        # warmup gate).  Sticky — set on the first update that contains real
        # measurements, never reset, so the gate measures "time since valid FPS
        # first appeared" rather than "current sample validity".  Suppresses
        # overload escalation for overload_warmup_s (default 10 s) after start.
        self._self_first_valid_fps_at: Optional[float] = None

        # ponytail: per-camera "ADD timestamp".  Set whenever we issue an ADD
        # command (winner of vote, leaderless failover, reclaim).  Cameras NOT
        # in this dict are assumed pre-existing and skip the per-camera warmup
        # gate — this keeps the existing L1/L2/L3 selector tests passing
        # without forcing every test to set a fake ADD timestamp.
        self._camera_added_at: Dict[str, float] = {}

        # ponytail: per-camera "first valid positive FPS" timestamp.  Sticky
        # within the lifetime of the dict entry; cleared (overwritten) when we
        # ADD the camera again so the warmup restarts.
        self._camera_first_valid_fps_at: Dict[str, float] = {}

        # -----------------------------------------------------------------------
        # Offload level table — shared with SpeedProbe (read from probe thread).
        # Maps camera_id → offload level (0=none, 1=stream, 2=vehicle, 3=plate).
        # Written only by the decision loop; read-only from the probe.
        # Protected by _offload_lock (separate from _lock to avoid deadlock with
        # the Zenoh callback thread which holds _lock).
        # -----------------------------------------------------------------------
        self._offload_table: Dict[str, int] = {}
        self._offload_lock = threading.RLock()

        # Per-camera timestamp of the last offload-level change (for cooldown)
        self._offload_level_changed_at: Dict[str, float] = {}

        # Target peer for each camera's offload (camera_id → node_id or "")
        self._offload_targets: Dict[str, str] = {}

        # Step-7: migration-complete timestamps for Δτ computation
        # camera_id → unix timestamp when REMOVE was confirmed sent
        self._migration_complete_ts: Dict[str, float] = {}

        # Stop event for cleanly blocking start()
        self._stop_event = threading.Event()
        # Signaled once Zenoh session and publishers are ready
        self._ready_event = threading.Event()

        # Thread pool for blocking I/O (RTT measurement) off the Zenoh callback thread
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="PeerOrch-IO")

        # BUG-15: Cache cameras.yml to avoid repeated disk reads on every vote.
        # Reset to None to force reload only if the file changes (not implemented
        # here — a future improvement could use inotify or mtime check).
        self._cameras_cache: Optional[dict] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open Zenoh session, declare pubs/subs, start decision thread."""
        import zenoh

        self._session = make_session()
        logger.info("[PeerOrch] Zenoh session opened (peer mode).")

        # Declare publishers once
        self._pubs["status"]        = self._session.declare_publisher(f"peers/status/{self._node_id}")
        self._pubs["vote_request"]  = self._session.declare_publisher("peers/vote/request")
        self._pubs["vote_proposal"] = self._session.declare_publisher("peers/vote/proposal")
        self._pubs["vote_decision"] = self._session.declare_publisher("peers/vote/decision")
        self._pubs["control"]       = self._session.declare_publisher(f"peers/control/{self._node_id}")

        # Subscribe to all P2P topics
        self._session.declare_subscriber("peers/status/**",      self._on_sample)
        self._session.declare_subscriber("peers/vote/request",   self._on_sample)
        self._session.declare_subscriber("peers/vote/proposal",  self._on_sample)
        self._session.declare_subscriber("peers/vote/decision",  self._on_sample)
        self._session.declare_subscriber("peers/vote/ack/**",    self._on_sample)
        logger.info("[PeerOrch] Subscribed to: peers/status/**, peers/vote/*, peers/vote/ack/**")

        self._running = True
        self._ready_event.set()

        self._decision_thread = threading.Thread(
            target=self._decision_loop,
            name=f"PeerDecision-{self._node_id}",
            daemon=True,
        )
        self._decision_thread.start()

        # Park — Zenoh peer mode needs no blocking loop
        self._stop_event.wait()

    def publish_status(self, payload: bytes) -> None:
        """Publish health status on peers/status/<node_id> (called by health push loop)."""
        pub = self._pubs.get("status")
        if pub:
            pub.put(payload)

    def update_self_state(self, payload: dict) -> None:
        """Update this node's local state without publishing a Zenoh heartbeat.

        The standalone health_agent.py is the single publisher for
        peers/status/<node_id>.  The pipeline process still needs a fresh
        _self_state for local offload/migration decisions, so the internal
        health loop calls this method directly instead of publishing a second
        heartbeat for the same NODE_ID.
        """
        with self._self_lock:
            self._self_state.load_score  = payload.get("load_score",  0.0)
            self._self_state.gpu_percent = payload.get("gpu_percent", 0.0)
            self._self_state.cpu_percent = payload.get("cpu_percent", 0.0)
            self._self_state.ram_percent = payload.get("ram_percent", 0.0)
            self._self_state.gpu_temp_c  = payload.get("gpu_temp_c",  0.0)
            self._self_state.risk_index  = payload.get("risk_index",  0.0)

            pipeline = payload.get("pipeline", {}) or {}
            self._self_state.avg_fps = pipeline.get("avg_fps")
            # Prefer output_fps_per_camera (Phase 1 unambiguous key); fall back
            # to fps_per_camera for backward compatibility with older firmware.
            self._self_state.fps_per_camera = _pick_fps_dict(pipeline)
            self._self_state.active_cameras = list(pipeline.get("active_cameras", []))
            self._self_state.camera_configs = pipeline.get("camera_configs", {})
            # Backwards-compatible: missing or malformed mapping → empty dict.
            self._self_state.camera_workload = _parse_camera_workload(
                pipeline.get("camera_workload", {})
            )
            self._self_state.source_starved_cameras = _parse_starved_cameras(
                pipeline.get("source_starved_cameras", [])
            )
            self._self_state.last_seen = time.time()
            # max_streams sourced from health payload; malformed values fall back to 8
            try:
                self._self_state.max_streams = int(pipeline.get("max_streams", 8) or 8)
            except (TypeError, ValueError):
                self._self_state.max_streams = 8

            # ponytail: track when valid positive FPS first appeared — drives the
            # startup warmup gate in _check_self_overload.  Sticky: once set,
            # never reset, so the gate measures elapsed time since first valid
            # sample rather than the validity of the current sample.
            fps_valid = _has_valid_positive_fps(self._self_state.fps_per_camera)
            if fps_valid and self._self_first_valid_fps_at is None:
                self._self_first_valid_fps_at = time.time()

            # Per-camera first-valid-FPS timestamps (sticky, per camera).
            # Drives the post-ADD warmup gate in _pick_camera_to_offload so a
            # freshly-ADDed camera is ineligible for offload until its FPS has
            # been valid for camera_warmup_s seconds.
            if fps_valid:
                now_ts = time.time()
                for cam_id, fps_val in self._self_state.fps_per_camera.items():
                    if not isinstance(cam_id, str):
                        continue
                    if (isinstance(fps_val, (int, float))
                            and not isinstance(fps_val, bool)
                            and math.isfinite(fps_val)
                            and fps_val > 0.0):
                        self._camera_first_valid_fps_at.setdefault(cam_id, now_ts)

            # Overload onset: must be BOTH overloaded (legacy load_score or
            # proactive risk_index) AND have valid positive FPS samples.
            # load_score=100 with empty/zero/NaN fps is meaningless without
            # running streams and must NOT escalate the node.
            overloaded = (
                self._is_overloaded(
                    self._self_state.load_score, self._self_state.risk_index
                )
                and fps_valid
            )
            if overloaded:
                if self._self_state.overload_since is None:
                    self._self_state.overload_since = time.time()
                self._reclaim_eligible_since = None
            else:
                self._self_state.overload_since = None

    def get_offload_level(self, camera_id: str) -> int:
        """
        Return the current offload level for camera_id (0–3).
        Called from SpeedProbe on every frame — must be lock-free fast.
        Uses a separate RLock from the main _lock to avoid priority inversion
        with the Zenoh callback thread.
        """
        with self._offload_lock:
            return self._offload_table.get(camera_id, 0)

    def get_offload_target(self, camera_id: str) -> str:
        """Return the node_id of the offload peer for camera_id, or '' if none."""
        with self._offload_lock:
            return self._offload_targets.get(camera_id, "")

    def set_offload_level(self, camera_id: str, level: int, target_node: str = "") -> None:
        """
        Set the offload level for camera_id.  Called only from the decision loop.
        level 0 = local processing (clear offload)
        level 3 = plate crop → peer
        level 2 = vehicle crop → peer
        level 1 = full stream migration (handled by existing RFO path)
        """
        with self._offload_lock:
            old = self._offload_table.get(camera_id, 0)
            self._offload_table[camera_id] = level
            self._offload_targets[camera_id] = target_node
            self._offload_level_changed_at[camera_id] = time.time()
        if old != level:
            logger.info(
                "[PeerOrch] Offload level %d→%d for '%s' (target='%s')",
                old, level, camera_id, target_node,
            )

    def stop(self) -> None:
        """Stop orchestrator."""
        self._running = False
        self._stop_event.set()
        if self._session:
            self._session.close()
        for timer in self._vote_timers.values():
            timer.cancel()
        self._vote_timers.clear()
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Zenoh subscriber callback
    # ------------------------------------------------------------------

    def _on_sample(self, sample) -> None:
        """Route incoming Zenoh samples by key expression."""
        try:
            payload = msgpack.unpackb(sample.payload.to_bytes(), raw=False)
        except Exception:
            return

        key = str(sample.key_expr)

        if key.startswith("peers/status/"):
            self._on_peer_status(payload)
        elif key == "peers/vote/request":
            self._on_vote_request(payload)
        elif key == "peers/vote/proposal":
            self._on_vote_proposal(payload)
        elif key == "peers/vote/decision":
            self._on_vote_decision(payload)
        elif key.startswith("peers/vote/ack/"):
            self._on_vote_ack(payload)
        else:
            logger.debug("[PeerOrch] Unknown key: %s", key)

    # ------------------------------------------------------------------
    # Peer status tracking
    # ------------------------------------------------------------------

    def _on_peer_status(self, payload: dict) -> None:
        """Update PeerState from heartbeat."""
        node_id = payload.get("node_id", "")
        if not node_id:
            return

        # Update state of this node itself
        if node_id == self._node_id:
            # BUG-1 fix: hold _self_lock while updating so the decision loop
            # always reads a consistent snapshot of _self_state.
            with self._self_lock:
                self._self_state.load_score  = payload.get("load_score",  0.0)
                self._self_state.gpu_percent = payload.get("gpu_percent", 0.0)
                self._self_state.cpu_percent = payload.get("cpu_percent", 0.0)
                self._self_state.ram_percent = payload.get("ram_percent", 0.0)
                self._self_state.gpu_temp_c  = payload.get("gpu_temp_c",  0.0)
                self._self_state.risk_index  = payload.get("risk_index",  0.0)
                pipeline = payload.get("pipeline", {}) or {}
                self._self_state.avg_fps = pipeline.get("avg_fps")
                # Prefer output_fps_per_camera (Phase 1 unambiguous key); fall back
                # to fps_per_camera for backward compatibility with older firmware.
                self._self_state.fps_per_camera = _pick_fps_dict(pipeline)
                self._self_state.active_cameras = list(pipeline.get("active_cameras", []))
                # Backwards-compatible: missing or malformed mapping → empty.
                self._self_state.camera_workload = _parse_camera_workload(
                    pipeline.get("camera_workload", {})
                )
                self._self_state.source_starved_cameras = _parse_starved_cameras(
                    pipeline.get("source_starved_cameras", [])
                )

                # ponytail: startup + per-camera warmup timestamp tracking —
                # same logic as update_self_state so the Zenoh-self path and
                # the direct update_self_state path behave identically.
                fps_valid = _has_valid_positive_fps(self._self_state.fps_per_camera)
                if fps_valid and self._self_first_valid_fps_at is None:
                    self._self_first_valid_fps_at = time.time()
                if fps_valid:
                    now_ts = time.time()
                    for cam_id, fps_val in self._self_state.fps_per_camera.items():
                        if not isinstance(cam_id, str):
                            continue
                        if (isinstance(fps_val, (int, float))
                                and not isinstance(fps_val, bool)
                                and math.isfinite(fps_val)
                                and fps_val > 0.0):
                            self._camera_first_valid_fps_at.setdefault(cam_id, now_ts)

                # Overload onset: must be BOTH overloaded AND have valid
                # positive FPS samples.  load_score=100 with empty/zero/NaN
                # fps is meaningless without running streams — dashboard
                # keeps the raw load_score, but the decision path is gated.
                overloaded = (
                    self._is_overloaded(
                        self._self_state.load_score, self._self_state.risk_index
                    )
                    and fps_valid
                )
                if overloaded:
                    if self._self_state.overload_since is None:
                        self._self_state.overload_since = time.time()
                    # Node is overloaded again — reset reclaim eligibility
                    self._reclaim_eligible_since = None
                else:
                    self._self_state.overload_since = None
            return

        # Update state of other peers
        # BUG-04: update ALL mutable fields inside the lock to prevent torn
        # reads from _decision_loop running on a separate thread.
        with self._lock:
            is_new = node_id not in self._peers
            if is_new:
                self._peers[node_id] = PeerState(node_id=node_id)
                logger.info("[PeerOrch] Discovered peer '%s' via Zenoh", node_id)
            peer = self._peers[node_id]

            peer.load_score  = payload.get("load_score",  0.0)
            peer.gpu_percent = payload.get("gpu_percent", 0.0)
            peer.cpu_percent = payload.get("cpu_percent", 0.0)
            peer.ram_percent = payload.get("ram_percent", 0.0)
            peer.gpu_temp_c  = payload.get("gpu_temp_c",  0.0)
            peer.risk_index  = payload.get("risk_index",  0.0)

            pipeline = payload.get("pipeline", {}) or {}
            peer.avg_fps        = pipeline.get("avg_fps")
            # Prefer output_fps_per_camera (Phase 1 unambiguous key); fall back
            # to fps_per_camera for backward compatibility with older firmware.
            peer.fps_per_camera = _pick_fps_dict(pipeline)
            peer.active_cameras = list(pipeline.get("active_cameras", []))
            peer.camera_configs = pipeline.get("camera_configs", peer.camera_configs)
            # Backwards-compatible: missing or malformed mapping → empty.
            peer.camera_workload = _parse_camera_workload(
                pipeline.get("camera_workload", {})
            )
            peer.source_starved_cameras = _parse_starved_cameras(
                pipeline.get("source_starved_cameras", [])
            )
            peer.last_seen = time.time()
            # max_streams from peer health payload; malformed values fall back to 8
            try:
                peer.max_streams = int(pipeline.get("max_streams", 8) or 8)
            except (TypeError, ValueError):
                peer.max_streams = 8

            # Track overload onset using same proactive-aware helper.
            # Gate on valid positive FPS: load_score=100 with fps={} means the
            # peer pipeline is unavailable (pipeline_available=False), NOT
            # genuinely overloaded.  Without this gate a newly-started peer
            # would appear overloaded to election logic before its pipeline runs.
            peer_fps_valid = _has_valid_positive_fps(peer.fps_per_camera)
            overloaded = (
                self._is_overloaded(peer.load_score, peer.risk_index)
                and peer_fps_valid
            )
            if overloaded:
                if peer.overload_since is None:
                    peer.overload_since = time.time()
            else:
                peer.overload_since = None

    # ------------------------------------------------------------------
    # Overload classification helper (proactive-aware)
    # ------------------------------------------------------------------

    def _is_overloaded(self, load_score: float, risk_index: float) -> bool:
        """
        Determine if this node (or a peer) should be considered overloaded.

        When proactive.enabled is True, use_index is driven by cycle-smoothed
        risk_index (U) against proactive.risk_threshold.

        Shadow mode (proactive.shadow_mode = true) emits proactive telemetry
        (risk_index) while keeping ALL decisions on the legacy reactive path.
        It returns load_score >= overload_threshold BEFORE any proactive risk
        or hard-fuse check — so the proactive predictor can be observed
        side-by-side without affecting migration behaviour.  Hard fuse is
        also bypassed: shadow mode is audit-only.

        A hard_fuse_threshold (default 0.95) forces overload regardless of
        proactive.enabled — it is a safety fuse against hardware saturation that
        load_score alone may under-report (e.g. when thermal throttling has just
        started and the jtop reading hasn't caught up).

        Falls back to legacy load_score >= overload_threshold when disabled.
        """
        proactive_cfg = self._cfg.get("proactive", {})

        # Shadow mode: telemetry only — strictly passive/reactive decisions
        if proactive_cfg.get("shadow_mode", False):
            return load_score >= self._cfg.get("overload_threshold", 42.0)

        hard_fuse = float(proactive_cfg.get("hard_fuse_threshold", 0.95))

        # Hard fuse — always active regardless of proactive.enabled
        if risk_index >= hard_fuse:
            return True

        if proactive_cfg.get("enabled", False) and risk_index > 0.0:
            threshold = float(proactive_cfg.get("risk_threshold", 0.85))
            return risk_index >= threshold

        # Legacy path
        return load_score >= self._cfg.get("overload_threshold", 42.0)

    # ------------------------------------------------------------------
    # Overload trigger score helper (for log messages + RFO payload)
    # ------------------------------------------------------------------

    def _effective_load(self, load_score: float, risk_index: float) -> float:
        """
        Return the score that best represents the current load for logging
        and for populating RFO payloads.  When proactive is enabled, returns
        risk_index × 100 (scaled to the same 0–100 range as load_score).
        """
        proactive_cfg = self._cfg.get("proactive", {})
        if proactive_cfg.get("enabled", False) and risk_index > 0.0:
            return round(risk_index * 100.0, 1)
        return load_score

    # ------------------------------------------------------------------
    # Decision loop (runs every 1s)
    # ------------------------------------------------------------------

    def _decision_loop(self) -> None:
        """Main loop — check overload + OFFLINE peers + rebalance."""
        logger.info("[PeerOrch] Decision loop started (interval=1s).")
        while self._running:
            time.sleep(1.0)
            try:
                self._check_offline_peers()
                self._check_rebalance()
                self._check_reclaim()
                self._check_self_overload()
            except Exception as exc:
                logger.error("[PeerOrch] Decision loop error: %s", exc)

    def _check_offline_peers(self) -> None:
        """
        Detect offline peers (heartbeat timeout).
        If peer has active cameras → trigger leaderless failover.
        """
        now = time.time()
        timeout = self._cfg.get("heartbeat_timeout_s", 5.0)

        with self._lock:
            to_check = list(self._peers.items())

        for node_id, peer in to_check:
            if node_id == self._node_id:
                continue
            silent_s = now - peer.last_seen
            if silent_s > timeout:
                self._clear_offload_target(node_id)
                if peer.active_cameras:
                    # BUG-6 fix: use _failover_triggered set to prevent
                    # re-triggering instead of clearing active_cameras.
                    # Clearing active_cameras races with _check_rebalance which
                    # reads it to decide whether the original owner has resumed
                    # a rescued camera.
                    if node_id not in self._failover_triggered:
                        orphans = list(peer.active_cameras)
                        self._failover_triggered.add(node_id)
                        logger.critical(
                            "[PeerOrch] Peer '%s' OFFLINE with %d cameras! Triggering failover...",
                            node_id, len(orphans),
                        )
                        self._notified_offline.discard(node_id)
                        threading.Thread(
                            target=self._leaderless_failover,
                            args=(node_id, orphans),
                            daemon=True,
                        ).start()
                else:
                    if node_id not in self._notified_offline:
                        logger.warning("[PeerOrch] Peer '%s' OFFLINE (no cameras).", node_id)
                        self._notified_offline.add(node_id)
            else:
                # Peer is alive — clear the notified/failover flags so we
                # react again if it goes offline a second time.
                self._notified_offline.discard(node_id)
                self._failover_triggered.discard(node_id)

    def _clear_offload_target(self, node_id: str) -> None:
        """Clear Level 2/3 crop offload entries that target an offline peer."""
        with self._offload_lock:
            affected = [
                camera_id for camera_id, target in self._offload_targets.items()
                if target == node_id and self._offload_table.get(camera_id, 0) in (2, 3)
            ]
            for camera_id in affected:
                old_level = self._offload_table.get(camera_id, 0)
                self._offload_table[camera_id] = 0
                self._offload_targets[camera_id] = ""
                self._offload_level_changed_at[camera_id] = time.time()
                logger.warning(
                    "[PeerOrch] Offload target '%s' offline — clearing L%d for '%s'",
                    node_id, old_level, camera_id,
                )

    def _check_rebalance(self) -> None:
        """
        Return rescued cameras when their original owner comes back online.

        If peer X died and we rescued cam_01, cam_02, and then X restarts
        and reports cam_01, cam_02 in its active_cameras, we remove our
        rescued copies to avoid duplicate streams.

        Guards (per spec):
        - owner absent (peer is None) → skip; never REMOVE/return to a
          peer that is no longer in the routing table.
        - owner stale (now - last_seen > heartbeat_timeout_s, default 5 s)
          → skip; the heartbeat timeout is sourced from config so operators
          can tune it.
        - owner not running the camera (not in peer.active_cameras) → skip;
          rebalance is only valid when the owner has actually resumed
          processing it.
        - last-active-camera guard → never return a rescued camera that
          would leave this node processing zero streams.
        - hard owned-camera invariant (NEW) → never leave this node with
          zero locally-owned cameras (cameras.yml via _get_owned_camera_ids),
          not merely zero active cameras.  Implemented as two layered checks:
            1. aggregate guard: if no locally-owned camera is currently
               active, block ALL returns (the node is in a degraded state;
               foreign returns make it worse).
            2. per-camera guard: if a specific camera in _rescued_cameras is
               locally-owned (defensive — _rescued_cameras should only ever
               hold foreign cameras), skip its return.
          Failover rescue is unaffected because _leaderless_failover does
          not route through _check_rebalance.
        """
        if not self._rescued_cameras:
            return

        now = time.time()
        timeout = self._cfg.get("heartbeat_timeout_s", 5.0)
        to_return: List[str] = []

        # ponytail: snapshot self-active under _self_lock and rescued/peers
        # under _lock so we can compute "after return" atomically without
        # races.  _camera_added_at is not read here.
        with self._self_lock:
            self_active_snapshot = set(self._self_state.active_cameras)

        # Resolve locally-owned camera IDs (live CameraManager first, then
        # cameras.yml).  ponytail: wrap in try/except — the helper is
        # documented as fail-safe (returns empty on error) but we belt-and-
        # brace it because a leak here would silently violate the hard
        # invariant that protects L1 migration availability.
        try:
            owned_ids = self._get_owned_camera_ids()
        except Exception as exc:
            logger.warning(
                "[PeerOrch][Rebalance] _get_owned_camera_ids() raised: %s; "
                "treating ownership as unresolved and blocking all returns.",
                exc,
            )
            owned_ids = None
        if owned_ids is None:
            owned_ids = set()
        owned_active_snapshot = owned_ids & self_active_snapshot

        # Aggregate owned-camera guard: zero locally-owned active cameras
        # means the node cannot satisfy the L1 ownership invariant for
        # migration decisions (see _pick_camera_to_offload).  Returning
        # any rescued camera would not change owned_active (rescued cameras
        # are foreign) but it would also fail to fix the degradation, so
        # we hold all rescues here until at least one owned camera resumes.
        # This is the hard invariant requested in the spec.
        if not owned_active_snapshot:
            logger.warning(
                "[PeerOrch][Rebalance] BLOCKED: no locally-owned active "
                "cameras (owned_active=0, rescued=%d). Holding rescued "
                "cameras until an owned stream resumes.",
                len(self._rescued_cameras),
            )
            return

        with self._lock:
            rescued_snapshot = list(self._rescued_cameras.items())
            peers_snapshot = dict(self._peers)
        # Decrement as we commit each return so two concurrent eligible
        # returns don't both pass the "more than one active remains" check.
        remaining_after = set(self_active_snapshot)
        for camera_id, original_owner in rescued_snapshot:
            # Per-camera guard: an owned camera must NEVER be returned by
            # this rebalance path.  _rescued_cameras is only ever populated
            # by _leaderless_failover from a peer's active_cameras, but a
            # transition window or a misconfigured yml could in principle
            # place an owned camera here.  Skip defensively.
            if camera_id in owned_ids:
                logger.warning(
                    "[PeerOrch][Rebalance] Skipping return of '%s' to '%s': "
                    "camera is locally-owned; this rebalance path only "
                    "handles foreign rescued cameras.",
                    camera_id, original_owner,
                )
                continue
            peer = peers_snapshot.get(original_owner)
            if peer is None:
                # Owner absent from routing table.
                continue
            if now - peer.last_seen > timeout:
                # Owner heartbeat older than configured timeout — stale.
                continue
            if camera_id not in peer.active_cameras:
                # Owner is back but hasn't resumed running this camera yet.
                continue
            if camera_id in remaining_after and len(remaining_after) <= 1:
                # Last-active-camera guard: would leave zero streams locally.
                logger.warning(
                    "[PeerOrch][Rebalance] Skipping return of '%s' to '%s': "
                    "it is the last active camera on this node (active=%d).",
                    camera_id, original_owner, len(remaining_after),
                )
                continue
            remaining_after.discard(camera_id)
            to_return.append(camera_id)

        for camera_id in to_return:
            original_owner = self._rescued_cameras.pop(camera_id, None)
            if original_owner is None:
                continue
            remove_cmd = {"cmd": "REMOVE", "camera_id": camera_id}
            self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
            logger.info(
                "[Rebalance] Returning '%s' to original owner '%s'. REMOVE sent.",
                camera_id, original_owner,
            )
            # BUG-1 fix: read _self_state under its lock
            with self._self_lock:
                _self_load = self._self_state.load_score
                _self_fps  = self._self_state.avg_fps
            self._migration_log.log(
                self._node_id, original_owner, camera_id,
                "rebalance_return", _self_load, _self_fps,
                0.0, "RETURNED",
            )

    def _check_reclaim(self) -> None:
        """
        Reclaim cameras that were migrated away due to overload, once this
        node's load drops sufficiently below the overload threshold.

        Reclaim condition:
          - This node's load_score has been below (overload_threshold - reclaim_margin)
            for at least reclaim_stable_s seconds.
          - The camera is still being held by the peer it was migrated to
            (confirmed via peer heartbeat).
          - Cooldown has expired since the migration.

        Reclaim is done one camera at a time to avoid oscillation.
        """
        if not self._migrated_out:
            return

        cfg = self._cfg
        now = time.time()

        reclaim_threshold = cfg.get("overload_threshold", 75.0) - cfg.get("reclaim_margin", 15.0)
        reclaim_stable_s  = cfg.get("reclaim_stable_s", 20.0)
        cooldown_s        = cfg.get("cooldown_s", 45.0)
        heartbeat_timeout = cfg.get("heartbeat_timeout_s", 6.0)

        with self._self_lock:
            load         = self._self_state.load_score
            overload_since = self._self_state.overload_since

        # Only reclaim if load has been stable and low
        if load >= reclaim_threshold:
            return
        # overload_since being None means load has dropped — good.
        # If it's still set, the node is still above overload_threshold.
        if overload_since is not None:
            return
        # Wait reclaim_stable_s after load dropped before reclaiming.
        # _reclaim_eligible_since is initialised to None in __init__ and set
        # here on first entry; the hasattr guard was dead code.
        if self._reclaim_eligible_since is None:
            self._reclaim_eligible_since = now
            return
        if now - self._reclaim_eligible_since < reclaim_stable_s:
            return

        # Find one camera to reclaim (oldest migration first)
        with self._lock:
            candidates = list(self._migrated_out.items())

        for camera_id, holder_node in candidates:
            # Check cooldown
            last_mig = self._cam_cooldown.get(camera_id, 0.0)
            if now - last_mig < cooldown_s:
                continue

            # Confirm holder is still alive and still holding this camera
            with self._lock:
                peer = self._peers.get(holder_node)
            if peer is None:
                # Peer gone — remove stale entry
                self._migrated_out.pop(camera_id, None)
                continue
            if now - peer.last_seen > heartbeat_timeout:
                # Holder offline — failover will handle it
                self._migrated_out.pop(camera_id, None)
                continue
            if camera_id not in peer.active_cameras:
                # Holder no longer running this camera — already returned or lost
                self._migrated_out.pop(camera_id, None)
                continue

            # Send ADD to self (reclaim)
            cam_config = self._get_camera_config(camera_id)
            if cam_config is None:
                logger.warning("[PeerOrch] Reclaim: cannot get config for '%s', skipping", camera_id)
                continue

            # Make-before-Break: Step 1 — ADD to self first, wait for stream PLAYING ack
            # Step 2 — Only then REMOVE from holder
            add_cmd = {**cam_config, "cmd": "ADD"}
            self._pubs["control"].put(msgpack.packb(add_cmd, use_bin_type=True))
            # ponytail: record the ADD so the per-camera warmup gate in
            # _pick_camera_to_offload suppresses offload actions on this
            # camera until its FPS has been valid for camera_warmup_s seconds.
            self._camera_added_at[camera_id] = now
            # Clear any stale first-valid-fps snapshot so the warmup restarts
            # from zero for this new ADD event.
            self._camera_first_valid_fps_at.pop(camera_id, None)
            logger.info(
                "[PeerOrch] Reclaim: load=%.1f < threshold=%.1f — "
                "ADD '%s' back to self (was held by '%s'), waiting for ack...",
                load, reclaim_threshold, camera_id, holder_node,
            )

            # Record reclaim start: this camera is ineligible for offload until
            # its FPS stabilises after returning home.
            self._reclaim_completed_at[camera_id] = now

            # Suppress overload decisions for the settle window once reclaim
            # ADD is initiated: incoming stream warm-up FPS can look like a
            # fresh overload and re-escalate the node moments after reclaim.
            self._transition_settle_until = now + cfg.get("transition_settle_s", 5.0)

            # Spin up a thread that waits for the local ADD ack then removes holder
            threading.Thread(
                target=self._wait_and_remove_reclaim,
                args=(camera_id, holder_node),
                daemon=True,
            ).start()

            self._migrated_out.pop(camera_id, None)
            self._cam_cooldown[camera_id] = now
            # Reset eligible timer to avoid immediately reclaiming next camera
            self._reclaim_eligible_since = now

            self._migration_log.log(
                holder_node, self._node_id, camera_id,
                "reclaim", load, None,
                0.0, "RECLAIMED",
            )
            # Reclaim one at a time
            return

    def _check_self_overload(self) -> None:
        """
        Checks if this node is overloaded and selects the cheapest offload
        level first (3 → 2 → 1) before escalating.

        Level 3 (plate-crop offload) is tried when load ≥ level3_threshold.
        Level 2 (vehicle-crop offload) escalates if load ≥ level2_threshold.
        Level 1 (full-stream migration, existing RFO path) is the last resort.

        All thresholds are read from edge_node.yml p2p section.
        """
        cfg   = self._cfg
        now   = time.time()
        # BUG-1 fix: snapshot _self_state under lock so we work with a
        # consistent view for the entire overload-check decision.
        with self._self_lock:
            state = PeerState(
                node_id=self._self_state.node_id,
                load_score=self._self_state.load_score,
                gpu_percent=self._self_state.gpu_percent,
                cpu_percent=self._self_state.cpu_percent,
                ram_percent=self._self_state.ram_percent,
                gpu_temp_c=self._self_state.gpu_temp_c,
                avg_fps=self._self_state.avg_fps,
                fps_per_camera=dict(self._self_state.fps_per_camera),
                active_cameras=list(self._self_state.active_cameras),
                overload_since=self._self_state.overload_since,
                penalty_until=self._self_state.penalty_until,
                risk_index=self._self_state.risk_index,
                camera_workload=dict(self._self_state.camera_workload),
                source_starved_cameras=list(self._self_state.source_starved_cameras),
            )

        if state.overload_since is None:
            logger.debug("[PeerOrch] Not overloaded (overload_since=None)")
            return
        if now - state.overload_since < cfg.get("overload_duration_s", 10.0):
            logger.debug("[PeerOrch] Overload too recent (%.1fs < %.1fs)",
                        now - state.overload_since, cfg.get("overload_duration_s", 10.0))
            return

        # ponytail: startup warmup gate.  Even with overload_since set,
        # suppress all overload escalation until self has had valid positive
        # FPS for overload_warmup_s seconds (default 10).  Without this, a
        # load_score that spiked before the pipeline stabilised could fire
        # RFO moments after start.  Field is sticky — once valid FPS appears
        # the timer counts from that moment; the gate never goes backwards.
        warmup_s = cfg.get("overload_warmup_s", 10.0)
        first_valid_fps_at = self._self_first_valid_fps_at
        if first_valid_fps_at is None:
            if self._maybe_log_block("warmup_no_fps", now):
                logger.warning(
                    "[PeerOrch] Overload check BLOCKED: startup warmup — "
                    "no valid positive FPS observed yet. No L3/L2/L1 actions."
                )
            return
        if now - first_valid_fps_at < warmup_s:
            if self._maybe_log_block("warmup_active", now):
                logger.warning(
                    "[PeerOrch] Overload check BLOCKED: startup warmup "
                    "(%.1fs since first valid FPS, need %.1fs). No L3/L2/L1 actions.",
                    now - first_valid_fps_at, warmup_s,
                )
            return

        # ── Decision suppression: pending-ack & post-migration settle ──
        # While a make-before-break migration is in flight (pending ack), no
        # L3/L2/L1 action is allowed — stale FPS samples could escalate the
        # node prematurely.  After a migration completes or reclaim ADD is
        # initiated, a configurable settle window (`transition_settle_s`,
        # default 5.0 s) holds all overload decisions so draining samples
        # cannot trigger a second migration before the pipeline stabilises.
        with self._lock:
            has_pending = bool(self._pending_acks)
        if has_pending:
            if self._maybe_log_block("pending_ack", now):
                logger.warning(
                    "[PeerOrch] Overload check BLOCKED: %d pending ack(s) "
                    "still in flight — waiting for migration completion "
                    "before issuing any L3/L2/L1 action.",
                    len(self._pending_acks),
                )
            return

        settle_remaining = self._transition_settle_until - now
        if settle_remaining > 0:
            if self._maybe_log_block("settle", now):
                logger.warning(
                    "[PeerOrch] Overload check BLOCKED: post-migration "
                    "settle window active (%.1f s remaining). No L3/L2/L1 "
                    "actions until settle expires.",
                    settle_remaining,
                )
            return
        # ── End decision suppression ──

        # Use effective load (risk_index×100 when proactive, else load_score)
        # for threshold comparisons so Level 1/2/3 boundaries are consistent
        # whether proactive mode is on or off.
        load = self._effective_load(state.load_score, state.risk_index)
        thr3          = cfg.get("offload_level3_threshold", 65.0)
        thr2          = cfg.get("offload_level2_threshold", 75.0)
        thr1          = cfg.get("offload_level1_threshold", 85.0)
        level_cd      = cfg.get("offload_level_cooldown_s", 20.0)
        global_offload = cfg.get("offload_level", 0)
        logger.debug("[PeerOrch] Overload check: load=%.1f, thr1=%.1f, thr2=%.1f, thr3=%.1f, "
                    "global_offload=%d, cam_ready=%s",
                    load, thr1, thr2, thr3, global_offload, state)

        # If offload is disabled in config, fall straight through to Level 1
        if global_offload == 0:
            self._trigger_level1_if_due(state, now, cfg)
            return

        # Determine intended target level (0 = clear, 1 = L1 migration, 2 = L2, 3 = L3)
        # FIRST, so camera selection can differ per level (L1 → lightest, L2/L3 → heaviest).
        if load >= thr1 and global_offload >= 1:
            intended_level = 1
        elif load >= thr2 and global_offload >= 2:
            intended_level = 2
        elif load >= thr3 and global_offload >= 3:
            intended_level = 3
        else:
            # Load dropped below all thresholds — clear fine-grained offload
            for cam_id in list(state.active_cameras):
                cl = self.get_offload_level(cam_id)
                if cl in (2, 3):
                    self.set_offload_level(cam_id, 0)
            return

        cam_to_offload = self._pick_camera_to_offload(state, level=intended_level)
        if not cam_to_offload:
            if self._maybe_log_block("no_cam", now):
                logger.warning(
                    "[PeerOrch] Overloaded (load=%.1f, over_thresh) but no eligible camera to "
                    "offload (active=%d, fps_data=%d) — cannot escalate",
                    load, len(state.active_cameras),
                    len(state.fps_per_camera) if state.fps_per_camera else 0,
                )
            return

        # Transition guard: this camera was recently reclaimed and its FPS is
        # still stabilising.  Re-escalating it would immediately undo reclaim.
        reclaim_age = now - self._reclaim_completed_at.get(cam_to_offload, 0.0)
        reclaim_stable_s = cfg.get("reclaim_stable_s", 20.0)
        if reclaim_age < reclaim_stable_s:
            logger.info(
                "[PeerOrch] Transition guard: '%s' was reclaimed %.0fs ago "
                "(need %.0fs) — skipping offload to avoid re-escalation.",
                cam_to_offload, reclaim_age, reclaim_stable_s,
            )
            return

        current_level = self.get_offload_level(cam_to_offload)

        # Escalation-aware cooldown: when moving toward greater urgency
        # (numeric level decreases, e.g. 3→2, 3→1, 2→1) use a shorter
        # bounded wait so offload reacts faster under worsening load.
        # Preserve full cooldown for same-level, de-escalation (0→1 becomes
        # L1-migration path which uses per-camera migration cooldown), and
        # clear/recovery (level→0).
        last_change = self._offload_level_changed_at.get(cam_to_offload, 0.0)
        if (current_level in (2, 3)
                and intended_level in (1, 2)
                and intended_level < current_level):
            esc_cd = min(
                cfg.get("escalation_cooldown_s", 5),
                level_cd,
            )
            if now - last_change < esc_cd:
                if self._maybe_log_block("lvl_cd_esc", now):
                    logger.info(
                        "[PeerOrch] Overload but escalation cooldown active for '%s' "
                        "(%.1fs elapsed, need %.1fs, current=%d→intended=%d) — offload blocked",
                        cam_to_offload, now - last_change, esc_cd,
                        current_level, intended_level,
                    )
                return
        else:
            if now - last_change < level_cd:
                if self._maybe_log_block("lvl_cd", now):
                    logger.info(
                        "[PeerOrch] Overload but level-change cooldown active for '%s' "
                        "(%.1fs elapsed, need %.1fs, level=%d) — offload blocked",
                        cam_to_offload, now - last_change, level_cd, current_level,
                    )
                return

        # Escalation ladder: 3 → 2 → 1
        # Read dwell config (fail-safe defaults if missing/malformed)
        l3_dwell = _dwell_s(cfg, "l3_dwell_s", 10.0)
        l2_dwell = _dwell_s(cfg, "l2_dwell_s", 7.0)
        # Hardware emergency fuse bypass: if risk_index >= hard_fuse_threshold,
        # skip dwell gates entirely so L1 can trigger immediately.
        hard_fuse = float(cfg.get("proactive", {}).get("hard_fuse_threshold", 0.95))
        fuse_active = state.risk_index >= hard_fuse

        if load >= thr1 and global_offload >= 1:
            # Full stream migration (Level 1) — existing RFO path.
            # Dwell gate: if this camera currently has L2 active, require L2
            # to have been active for l2_dwell seconds before escalating to L1.
            # Bypassed if hardware emergency fuse is active.
            # Only blocks normal escalation FROM an active L2, not all L1 globally.
            if not fuse_active and current_level == 2:
                l2_since = self._offload_level_changed_at.get(cam_to_offload, 0.0)
                if now - l2_since < l2_dwell:
                    if self._maybe_log_block("l2_dwell", now):
                        logger.info(
                            "[PeerOrch] L1 escalation blocked: L2 dwell not met for '%s' "
                            "(%.1fs elapsed, need %.1fs, fuse=%s) — retaining Level 2",
                            cam_to_offload, now - l2_since, l2_dwell, fuse_active,
                        )
                    return

            # Guard: check vote-in-progress and per-camera migration cooldown
            # BEFORE clearing any fine-grained offload so Level 2/3 survives
            # when L1 is blocked.
            with self._lock:
                already_voting = bool(self._vote_in_progress)
            if already_voting:
                if self._maybe_log_block("l1_vote", now):
                    logger.warning(
                        "[PeerOrch] L1 blocked: vote in progress for '%s' — "
                        "retaining Level %d offload for '%s'",
                        self._vote_in_progress,
                        self.get_offload_level(cam_to_offload),
                        cam_to_offload,
                    )
                return
            last_mig = self._cam_cooldown.get(cam_to_offload, 0.0)
            if now - last_mig < cfg.get("cooldown_s", 45.0):
                if self._maybe_log_block("l1_cooldown", now):
                    logger.warning(
                        "[PeerOrch] L1 blocked: migration cooldown active for '%s' "
                        "(%.1f s elapsed, need %.1f s) — retaining Level %d offload",
                        cam_to_offload,
                        now - last_mig,
                        cfg.get("cooldown_s", 45.0),
                        self.get_offload_level(cam_to_offload),
                    )
                return
            # Actually going to trigger L1 — clear fine-grained offload first
            if self.get_offload_level(cam_to_offload) in (2, 3):
                self.set_offload_level(cam_to_offload, 0)
            logger.warning(
                "[PeerOrch] Load=%.1f ≥ L1 threshold=%.1f. Escalating to "
                "Level 1 stream migration for '%s'.",
                load, thr1, cam_to_offload,
            )
            trigger = "fps_drop" if (state.avg_fps and
                                     state.avg_fps < cfg.get("eps_fps_strict", 18.0)
                                     ) else "load_score"
            logger.warning("[PeerOrch] RFO trigger: %s reason=%s", cam_to_offload, trigger)
            self._trigger_rfo(cam_to_offload, relaxation_tier=0)

        elif load >= thr2 and global_offload >= 2:
            # Dwell gate: if this camera currently has L3 active, require L3
            # to have been active for l3_dwell seconds before escalating to L2.
            # Bypassed if hardware emergency fuse is active.
            # Only blocks normal escalation FROM an active L3, not all L2 globally.
            if not fuse_active and current_level == 3:
                l3_since = self._offload_level_changed_at.get(cam_to_offload, 0.0)
                if now - l3_since < l3_dwell:
                    if self._maybe_log_block("l3_dwell", now):
                        logger.info(
                            "[PeerOrch] L2 escalation blocked: L3 dwell not met for '%s' "
                            "(%.1fs elapsed, need %.1fs, fuse=%s) — retaining Level 3",
                            cam_to_offload, now - l3_since, l3_dwell, fuse_active,
                        )
                    return

            best_peer = self._pick_best_peer(for_offload_level=2)
            if not best_peer:
                if self._maybe_log_block("no_peer_l2", now):
                    logger.warning(
                        "[PeerOrch] Overloaded (load=%.1f ≥ L2=%.1f) but no suitable "
                        "peer for Level 2 crop offload — offload blocked",
                        load, thr2,
                    )
                return
            if current_level != 2:
                logger.warning(
                    "[PeerOrch] Load=%.1f ≥ L2 threshold=%.1f. "
                    "Level 2 vehicle-crop offload for '%s' → '%s'.",
                    load, thr2, cam_to_offload, best_peer,
                )
                self.set_offload_level(cam_to_offload, 2, target_node=best_peer)

        elif load >= thr3 and global_offload >= 3:
            best_peer = self._pick_best_peer(for_offload_level=3)
            if not best_peer:
                if self._maybe_log_block("no_peer_l3", now):
                    logger.warning(
                        "[PeerOrch] Overloaded (load=%.1f ≥ L3=%.1f) but no suitable "
                        "peer for Level 3 plate-crop offload — offload blocked",
                        load, thr3,
                    )
                return
            if current_level != 3:
                logger.warning(
                    "[PeerOrch] Load=%.1f ≥ L3 threshold=%.1f. "
                    "Level 3 plate-crop offload for '%s' → '%s'.",
                    load, thr3, cam_to_offload, best_peer,
                )
                self.set_offload_level(cam_to_offload, 3, target_node=best_peer)

    def _trigger_level1_if_due(self, state, now: float, cfg: dict) -> None:
        """Legacy Level-1 overload trigger (existing behaviour, unchanged).
        NOTE: `state` is already a consistent snapshot captured by _check_self_overload.
        """
        # If ANY camera already has a vote in progress, wait for its outcome
        # before triggering another RFO. This prevents migrating more cameras
        # than needed — one migration at a time until load drops.
        with self._lock:
            if self._vote_in_progress:
                logger.debug("[PeerOrch] Vote already in progress for %s, skipping re-trigger",
                             self._vote_in_progress)
                return

        cam_to_offload = self._pick_camera_to_offload(state, level=1)
        if not cam_to_offload:
            logger.debug("[PeerOrch] No camera to offload (all inactive or locked)")
            return
        
        last_mig = self._cam_cooldown.get(cam_to_offload, 0.0)
        time_since_mig = now - last_mig
        if time_since_mig < cfg.get("cooldown_s", 45.0):
            logger.debug("[PeerOrch] Cooldown not met for '%s' (%.1fs / %.1fs)",
                        cam_to_offload, time_since_mig, cfg.get("cooldown_s", 45.0))
            return
        trigger_reason = (
            "fps_drop"
            if (state.avg_fps and state.avg_fps < cfg.get("eps_fps_strict", 18.0))
            else "load_score"
        )
        logger.warning(
            "[PeerOrch] OVERLOADED (%.1f%%, FPS=%s). Triggering RFO for '%s' (reason: %s)",
            state.load_score, state.avg_fps, cam_to_offload, trigger_reason,
        )
        self._trigger_rfo(cam_to_offload, relaxation_tier=0)

    def _pick_best_peer(self, for_offload_level: int = 1) -> Optional[str]:
        """
        Return the node_id of the alive peer with the lowest load score,
        subject to not being in cooldown. Stream capacity is enforced only for
        Level 1 full-stream migration; Level 2/3 crop offload does not consume
        a streammux slot.
        Returns None if no suitable peer is found.
        """
        now = time.time()
        timeout = self._cfg.get("heartbeat_timeout_s", 5.0)

        # Thermal admission gate: reject peers with unsafe or ambiguous
        # temperatures before evaluating load score. Configurable via
        # Edge/configs/edge_node.yml p2p.thermal section.
        # Reuses the shared _thermal_admission_ok so sender and receiver
        # rules cannot drift.
        therm_cfg = self._cfg.get("thermal")
        best_id : Optional[str] = None
        best_load = float("inf")

        with self._lock:
            for nid, peer in self._peers.items():
                if nid == self._node_id:
                    continue
                if now - peer.last_seen > timeout:
                    continue
                if for_offload_level <= 1 and (
                    len(peer.active_cameras) + self._peer_inflight.get(nid, 0)
                    >= peer.max_streams
                ):
                    # ponytail: count in-flight reservations so simultaneous
                    # RFOs from this node don't both pick the same peer and
                    # overflow its stream capacity before ACKs arrive.
                    continue
                if not _thermal_admission_ok(peer.gpu_temp_c, therm_cfg):
                    logger.info(
                        "[PeerOrch] Peer '%s' rejected by thermal gate: "
                        "gpu_temp_c=%s (max=%.1f)",
                        nid, peer.gpu_temp_c,
                        self._cfg.get("thermal", {}).get("max_gpu_temp_c", 85.0),
                    )
                    continue
                if now < peer.penalty_until:
                    continue
                if peer.load_score < best_load:
                    best_load = peer.load_score
                    best_id   = nid

        return best_id

    # ------------------------------------------------------------------
    # Voting — Requester side
    # ------------------------------------------------------------------

    def _trigger_rfo(self, camera_id: str, relaxation_tier: int = 0) -> None:
        """
        Send Request for Offload (RFO) and open vote window.

        relaxation_tier:
          0 = strict (eps_fps_strict, eps_network_ms_strict)
          1 = tier1  (eps_network_ms_tier1)
          2 = tier2  (eps_fps_tier1/2)
        """
        cfg = self._cfg
        eps_fps_map = [
            cfg.get("eps_fps_strict", 18.0),
            cfg.get("eps_fps_tier1", 15.0),
            cfg.get("eps_fps_tier2", 12.0),
        ]
        eps_fps = eps_fps_map[min(relaxation_tier, 2)]
        eps_net = cfg.get("eps_network_ms_strict", 50.0) if relaxation_tier == 0 \
                  else cfg.get("eps_network_ms_tier1", 80.0)

        cam_uri = self._get_camera_uri(camera_id) or ""

        with self._self_lock:
            _rfo_load = self._self_state.load_score
            _rfo_fps  = self._self_state.avg_fps

        payload = {
            "requester":      self._node_id,
            "camera_id":      camera_id,
            "cam_uri":        cam_uri,
            "load_score":     _rfo_load,
            "avg_fps":        _rfo_fps,
            "eps_fps":        eps_fps,
            "eps_network_ms": eps_net,
            "tier":           relaxation_tier,
            "ts":             time.time(),
        }

        with self._lock:
            self._vote_windows[camera_id] = []
            # Mark this camera as having RFO in progress
            self._vote_in_progress.add(camera_id)

        self._pubs["vote_request"].put(msgpack.packb(payload, use_bin_type=True))
        logger.info("[PeerOrch] RFO sent for '%s' (tier=%d, eps_fps=%.1f, eps_net=%.0fms)",
                    camera_id, relaxation_tier, eps_fps, eps_net)

        # Timer to close vote window
        timer = threading.Timer(
            cfg.get("vote_window_s", 3.0),
            self._close_vote_window,
            args=(camera_id, relaxation_tier),
        )
        with self._lock:
            self._vote_timers[camera_id] = timer
        timer.start()

    def _close_vote_window(self, camera_id: str, relaxation_tier: int) -> None:
        """
        Close vote window, select winner.

        If no proposals → escalate relaxation tier.
        If max tier exhausted with no proposals → log CLUSTER_SATURATED.
        """
        with self._lock:
            proposals = self._vote_windows.pop(camera_id, [])
            self._vote_timers.pop(camera_id, None)
            # Keep _vote_in_progress set until we know we're not escalating,
            # to prevent _check_self_overload from re-triggering in the gap.
            # It will be cleared below if we're not escalating another tier.

        if not proposals:
            if relaxation_tier < 2:
                logger.warning(
                    "[PeerOrch] Zero bids for '%s' (tier=%d). Relaxing ε...",
                    camera_id, relaxation_tier,
                )
                # _vote_in_progress stays set; _trigger_rfo will keep it set
                self._trigger_rfo(camera_id, relaxation_tier=relaxation_tier + 1)
            else:
                logger.error(
                    "[PeerOrch] CLUSTER_SATURATED: no peer can accept '%s'. "
                    "Continuing with current load.",
                    camera_id,
                )
                # All tiers exhausted — clear in_progress and set cooldown
                with self._lock:
                    self._vote_in_progress.discard(camera_id)
                self._cam_cooldown[camera_id] = time.time()
                logger.info(
                    "[PeerOrch] Cooldown set for '%s' (%.1fs) to prevent RFO spam",
                    camera_id, self._cfg.get("cooldown_s", 45.0),
                )
            return

        # Winner found — clear in_progress
        with self._lock:
            self._vote_in_progress.discard(camera_id)

        # Winner = proposal with lowest F(x)
        winner = min(proposals, key=lambda p: p["score"])
        cam_config = self._get_camera_config(camera_id)
        if cam_config is None:
            logger.error("[PeerOrch] Cannot get config for camera '%s'. Aborting election.", camera_id)
            return

        decision = {
            "winner":     winner["bidder"],
            "camera_id":  camera_id,
            "from_node":  self._node_id,
            "cam_config": cam_config,
            "ts":         time.time(),
        }
        winner_id = winner["bidder"]

        # Phase 3 review fix 1+3: atomically (a) reserve a stream slot on the
        # winner and (b) register the pending-ACK event BEFORE the decision is
        # published.  A fast valid ACK from the winner can therefore never
        # arrive before its event exists, so it cannot be dropped into a false
        # timeout/penalty.  All three lifecycle actors — _close_vote_window
        # (reserve), _on_vote_ack (ACK release), _wait_and_remove (timeout
        # release) — mutate _peer_inflight/_pending_winner under _lock so a
        # single canonical owner always wins the pop.
        ack_event = threading.Event()
        with self._lock:
            self._peer_inflight[winner_id] = self._peer_inflight.get(winner_id, 0) + 1
            self._pending_winner[camera_id] = winner_id
            self._pending_acks[camera_id] = ack_event

        self._pubs["vote_decision"].put(msgpack.packb(decision, use_bin_type=True))
        self._cam_cooldown[camera_id] = time.time()
        logger.info(
            "[PeerOrch] Election won by '%s' for '%s' (score=%.1f, fps_pred=%.1f, inflight=%d)",
            winner_id, camera_id, winner["score"], winner.get("fps_predicted", 0),
            self._peer_inflight[winner_id],
        )

    # ------------------------------------------------------------------
    # Voting — Bidder side
    # ------------------------------------------------------------------

    def _on_vote_request(self, payload: dict) -> None:
        """
        Receive RFO from another peer.
        Check ε-constraints, if pass → send proposal.

        RTT measurement runs in a thread pool so we never block
        the Zenoh subscriber callback thread.
        """
        requester = payload.get("requester", "")
        if requester == self._node_id:
            return  # Ignore own RFO

        camera_id = payload.get("camera_id", "")
        logger.info("[PeerOrch] RFO received from '%s' for camera '%s'", requester, camera_id)

        # Offload the blocking work immediately so this callback returns fast
        self._executor.submit(self._evaluate_and_bid, payload)

    def _evaluate_and_bid(self, payload: dict) -> None:
        """
        Run ε-constraint checks and publish proposal if eligible.
        Runs in ThreadPoolExecutor — safe to block for RTT measurement.
        """
        camera_id     = payload.get("camera_id", "")
        eps_fps       = payload.get("eps_fps", 18.0)
        eps_net_ms    = payload.get("eps_network_ms", 50.0)

        # BUG-1 fix: read _self_state under its own lock
        with self._self_lock:
            current_streams = len(self._self_state.active_cameras)
            self_load = self._self_state.load_score
            self_temp = self._self_state.gpu_temp_c

        # Phase 3 review fix 2 and Fix 5: explicit L1 capacity semantics.
        #
        # ``max_streams`` is the hard hardware/pipeline limit (e.g. GStreamer
        # streammux max-sources / decoder max slots) — enforced for direct
        # sender-side peer selection in ``_pick_best_peer`` and for failover
        # self-eligibility.  It is NEVER relaxed by L2/L3 offload.
        #
        # ``eps_streams_max`` is the ε admission policy limit — it is the
        # ceiling used by the receiver's ε1 gate (_evaluate_and_bid) which
        # additionally accounts ``_self_inflight`` reservations (bids accepted
        # but not yet ADDed).  Both the capacity check and the reservation
        # increment are held under ``_self_inflight_lock`` as a single atomic
        # read–modify–write, with explicit rollback on every downstream reject
        # path so concurrent evaluators cannot overbook eps_streams_max.
        #
        # Canonical expression of L1 slot availability (used consistently in
        # _pick_best_peer, _evaluate_and_bid, and failover self-eligibility):
        #   sender gate (receiver hardware):           peer.active_cameras + peer_inflight < peer.max_streams
        #   receiver admission (receiver ε policy):    current_active + self_inflight < eps_streams_max
        #   failover self-eligibility (self hardware): current_active + self_accepted < eps_streams_max  (= eps_streams_max)

        # ε0 — Thermal admission gate for THIS node (receiver).
        # Same rule as _pick_best_peer's sender-side gate: do not bid when
        # this node is too hot or has an unknown reading under a conservative
        # policy. Accepting a stream onto a throttled node helps nobody.
        therm_cfg = self._cfg.get("thermal")
        if not _thermal_admission_ok(self_temp, therm_cfg):
            logger.info(
                "[PeerOrch] RFO rejected for '%s': ε0 (thermal-self) — "
                "gpu_temp_c=%s (max=%.1f)",
                camera_id, self_temp,
                therm_cfg.get("max_gpu_temp_c", 85.0) if therm_cfg else 85.0,
            )
            return

        # ε1 — Capacity constraint (Phase 3 review fix 2).
        #
        # The capacity check AND the _self_inflight reservation increment are
        # one atomic critical section: concurrent evaluators cannot both read
        # the same "free slot" and overbook past eps_streams_max.  The
        # reservation is made BEFORE any later ε-check (ε2..ε5) or the RTT
        # measurement, and rolled back via the finally block below on every
        # downstream rejection/exception — so a bid that never ships does not
        # hold capacity, and one that ships is counted exactly once.
        eps_streams_max = self._cfg.get("eps_streams_max", 4)
        with self._self_inflight_lock:
            accounted_streams = current_streams + self._self_inflight
            if accounted_streams >= eps_streams_max:
                logger.info(
                    "[PeerOrch] RFO rejected for '%s': ε1 (capacity) — "
                    "current=%d inflight=%d max=%d",
                    camera_id, current_streams,
                    accounted_streams - current_streams, eps_streams_max,
                )
                return
            self._self_inflight += 1  # reserve now, atomically with the check

        # Flag flipped to False once the bid is fully shipped (no rollback).
        reservation_committed = False
        try:
            # ε2 — FPS prediction
            # YAML parses bare integer keys as int; look up both int and str forms
            fps_model = self._cfg.get("fps_model", {})
            streams_after = current_streams + 1
            predicted_fps = fps_model.get(streams_after,
                            fps_model.get(str(streams_after), None))
            if predicted_fps is None:
                logger.info(
                    "[PeerOrch] RFO rejected for '%s': ε2 (FPS) — "
                    "no fps_model entry for streams_after=%d (current=%d, max modeled=%d)",
                    camera_id, streams_after, current_streams, max(fps_model.keys(), default=0),
                )
                return
            if predicted_fps < eps_fps:
                logger.info(
                    "[PeerOrch] RFO rejected for '%s': ε2 (FPS) — "
                    "predicted=%.1f, required=%.1f (streams_after=%d)",
                    camera_id, predicted_fps, eps_fps, streams_after,
                )
                return

            # ε3 — Network RTT to camera RTSP origin (blocking — safe here in thread pool)
            # Prefer URI from the RFO payload (sent by requester who owns the camera).
            # Fall back to local lookup for backward compatibility.
            cam_uri = payload.get("cam_uri") or self._get_camera_uri(camera_id)
            if not cam_uri:
                logger.info("[PeerOrch] RFO rejected for '%s': ε3 (network) — camera URI not found", camera_id)
                return
            rtt_ms = self._measure_rtt(cam_uri)
            if rtt_ms is None or rtt_ms > eps_net_ms:
                logger.info(
                    "[PeerOrch] RFO rejected for '%s': ε3 (network) — "
                    "RTT=%.1fms, threshold=%.1fms",
                    camera_id, rtt_ms if rtt_ms else -1.0, eps_net_ms,
                )
                return

            # ε4 — Per-camera cooldown
            last_mig = self._cam_cooldown.get(camera_id, 0.0)
            cooldown_s = self._cfg.get("cooldown_s", 45.0)
            time_since_last = time.time() - last_mig
            if time_since_last < cooldown_s:
                logger.info(
                    "[PeerOrch] RFO rejected for '%s': ε4 (cooldown) — "
                    "%.1fs since last migration, need %.1fs",
                    camera_id, time_since_last, cooldown_s,
                )
                return

            # ε5 — Penalty check (applied when this node previously caused a migration timeout)
            now = time.time()
            if now < self._self_penalty_until:
                logger.info(
                    "[PeerOrch] RFO rejected for '%s': ε5 (penalty) — "
                    "penalized until %.1f",
                    camera_id, self._self_penalty_until,
                )
                return

            # All constraints pass — compute F(x)
            # F(x) = estimated load score after accepting this stream
            f_x = self_load + (100.0 - self_load) * 0.25

            proposal = {
                "bidder":        self._node_id,
                "camera_id":     camera_id,
                "score":         round(f_x, 2),
                "fps_predicted": predicted_fps,
                "rtt_ms":        round(rtt_ms, 1),
                "ts":            time.time(),
            }

            self._pubs["vote_proposal"].put(msgpack.packb(proposal, use_bin_type=True))
            logger.info(
                "[PeerOrch] RFO accepted for '%s' (ALL ε-constraints pass) — "
                "Bid: score=%.1f, fps_pred=%.1f, rtt=%.0fms",
                camera_id, f_x, predicted_fps, rtt_ms,
            )
            reservation_committed = True

            # Phase 3: hold the receiver-side reservation for the worst-case
            # resolution window (vote_window_s + migration_timeout_s), then
            # decay it.  Conservative timeout-only decay: the ADD command goes
            # to ZenohCommandSubscriber, not back through this orchestrator,
            # so we cannot hook the ADD arrival to release earlier.
            decay_s = (
                self._cfg.get("vote_window_s", 2.0)
                + self._cfg.get("migration_timeout_s", 15.0)
            )

            def _decay_self_inflight():
                with self._self_inflight_lock:
                    self._self_inflight = max(0, self._self_inflight - 1)
                logger.debug("[PeerOrch] Self inflight reservation decayed (camera='%s')", camera_id)

            threading.Timer(decay_s, _decay_self_inflight).start()
        finally:
            # Roll back the reservation if we never shipped a bid (any ε-check
            # rejection above, RTT failure, or an unexpected exception).
            if not reservation_committed:
                with self._self_inflight_lock:
                    self._self_inflight = max(0, self._self_inflight - 1)

    def _on_vote_proposal(self, payload: dict) -> None:
        """Collect proposals — only requester processes."""
        camera_id = payload.get("camera_id", "")
        if not camera_id:
            return
        with self._lock:
            if camera_id in self._vote_windows:
                self._vote_windows[camera_id].append(payload)

    # ------------------------------------------------------------------
    # Voting — Decision execution
    # ------------------------------------------------------------------

    def _on_vote_decision(self, payload: dict) -> None:
        """
        Receive election result.

        If I am winner → ADD camera.
        If I am requester → wait for ack then REMOVE.
        """
        winner    = payload.get("winner", "")
        camera_id = payload.get("camera_id", "")
        from_node = payload.get("from_node", "")

        if winner == self._node_id:
            # --- I WON: ADD camera to pipeline ---
            cam_config = payload.get("cam_config", {})
            if not cam_config:
                logger.error("[PeerOrch] Decision missing cam_config for '%s'", camera_id)
                return
            add_cmd = {**cam_config, "cmd": "ADD"}
            # ZenohCommandSubscriber is the single owner of ADD/ACK: it
            # processes the ADD, waits for the stream to reach PLAYING,
            # and publishes peers/vote/ack/{cam}.  Publishing directly to
            # peers/control/{winner} routes through Zenoh — which loops
            # back to our own subscriber when we are the winner.
            winner_key = f"peers/control/{winner}"
            self._session.put(winner_key, msgpack.packb(add_cmd, use_bin_type=True))
            logger.info("[PeerOrch] ADD command published for '%s' to '%s'", camera_id, winner)
            # ponytail: camera now being added from vote winner — record the ADD
            # so _pick_camera_to_offload can apply the per-camera warmup gate.
            self._camera_added_at[camera_id] = time.time()
            self._camera_first_valid_fps_at.pop(camera_id, None)

        elif from_node == self._node_id:
            # --- I AM REQUESTER: wait for ack then REMOVE ---
            threading.Thread(
                target=self._wait_and_remove,
                args=(camera_id, winner),
                daemon=True,
            ).start()

    def _wait_and_remove(self, camera_id: str, winner_node: str) -> None:
        """
        Make-before-Break: wait for winner to confirm PLAYING → REMOVE from self.

        If timeout → rollback (penalize winner node).

        Phase 3 review fix 1: the pending-ACK event was already registered in
        _close_vote_window BEFORE the decision was published, so a fast valid
        ACK can never arrive before its event exists (no dropped ACK → no false
        timeout/penalty).  We reuse that pre-registered event here instead of
        creating a fresh one.
        """
        with self._lock:
            event = self._pending_acks.get(camera_id)
        if event is None:
            # Defensive fallback (e.g. caller other than the requester path)
            # — create it now, even though it should already exist.
            event = threading.Event()
            with self._lock:
                self._pending_acks[camera_id] = event

        start_ms = time.time() * 1000
        timeout = self._cfg.get("migration_timeout_s", 15.0)
        # BUG-14 fix: capture trigger metrics under _self_lock for a consistent
        # snapshot at the moment the migration starts, not at some later point.
        with self._self_lock:
            trigger_load = self._self_state.load_score
            trigger_fps  = self._self_state.avg_fps

        confirmed = event.wait(timeout=timeout)

        with self._lock:
            self._pending_acks.pop(camera_id, None)

        if not confirmed:
            # Timeout — rollback: penalise the winner so we don't pick it again soon
            logger.error(
                "[PeerOrch] TIMEOUT (%ds) waiting for ack from '%s' for '%s'. Rolling back.",
                int(timeout), winner_node, camera_id,
            )
            # Phase 3 review fix 3: release the in-flight reservation ONLY if
            # this camera still owns one, and ONLY once.  _on_vote_ack and the
            # timeout path both race to pop _pending_winner under _lock — the
            # single winner of the pop is the single owner of the decrement, so
            # an ACK/timeout race can never double-release or decrement a
            # reservation belonging to a different camera/winner.
            with self._lock:
                owned = self._pending_winner.pop(camera_id, None)
            if owned == winner_node:
                self._peer_inflight[winner_node] = max(
                    0, self._peer_inflight.get(winner_node, 0) - 1
                )
                logger.debug(
                    "[PeerOrch] Timeout released reservation for '%s' (winner='%s', inflight=%d)",
                    camera_id, winner_node, self._peer_inflight[winner_node],
                )
            penalty_until = time.time() + self._cfg.get("cooldown_s", 45.0) * 2
            if winner_node == self._node_id:
                # The winner is ourselves — set our own penalty field
                self._self_penalty_until = penalty_until
            else:
                with self._lock:
                    if winner_node in self._peers:
                        self._peers[winner_node].penalty_until = penalty_until
            self._migration_log.log(
                self._node_id, winner_node, camera_id,
                "timeout", trigger_load, trigger_fps,
                time.time() * 1000 - start_ms, "TIMEOUT_ROLLBACK",
            )
            return

        # Success — REMOVE from self
        remove_cmd = {"cmd": "REMOVE", "camera_id": camera_id}
        self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
        logger.info(
            "[PeerOrch] REMOVE sent to self for '%s'. Migration complete.",
            camera_id,
        )

        # Update cooldown and track migration for future reclaim
        self._cam_cooldown[camera_id] = time.time()
        self._migrated_out[camera_id] = winner_node

        elapsed_ms = time.time() * 1000 - start_ms
        # Δτ: time from migration complete to first valid speed on the new node.
        # The SpeedProbe on winner_node will update _first_valid_speed_ts once
        # it produces its first valid measurement; that timestamp is compared
        # against the local time here to get the Application Blind-spot duration.
        # We record the migration-complete timestamp; blind_spot_ms is computed
        # once the winner's first heartbeat confirms active FPS on this camera.
        self._migration_log.log(
            self._node_id, winner_node, camera_id,
            "overload", trigger_load, trigger_fps,
            elapsed_ms, "SUCCESS",
            blind_spot_ms=None,   # filled in by _update_blind_spot() when known
        )
        # Store migration-complete timestamp so _update_blind_spot can reference it
        self._migration_complete_ts[camera_id] = time.time()

        # Suppress overload decisions for the settle window so stale/draining
        # FPS samples on the reduced pipeline cannot trigger a second offload.
        self._transition_settle_until = time.time() + self._cfg.get(
            "transition_settle_s", 5.0,
        )

        logger.info(
            "[PeerOrch] Migration DONE in %.0fms: '%s' → %s",
            elapsed_ms, camera_id, winner_node,
        )

    def _wait_and_remove_reclaim(self, camera_id: str, holder_node: str) -> None:
        """
        Make-before-Break for reclaim:
          - Wait for local ADD ack (stream PLAYING on self)
          - Then send REMOVE to holder node

        Reuses the same _pending_acks event mechanism as _wait_and_remove.
        If timeout, log error but do NOT rollback — the ADD is already live
        and the holder will eventually be cleaned up by rebalance.
        """
        event = threading.Event()
        with self._lock:
            self._pending_acks[camera_id] = event

        timeout = self._cfg.get("migration_timeout_s", 15.0)
        confirmed = event.wait(timeout=timeout)

        with self._lock:
            self._pending_acks.pop(camera_id, None)

        if not confirmed:
            logger.error(
                "[PeerOrch] Reclaim: TIMEOUT (%ds) waiting for local ADD ack of '%s'. "
                "Camera added to self but holder '%s' NOT removed — may cause duplicate stream.",
                int(timeout), camera_id, holder_node,
            )
            return

        # Stream confirmed PLAYING on self — safe to remove from holder
        remove_cmd = {"cmd": "REMOVE", "camera_id": camera_id}
        holder_control_key = f"peers/control/{holder_node}"
        try:
            self._session.put(
                holder_control_key,
                msgpack.packb(remove_cmd, use_bin_type=True),
            )
            logger.info(
                "[PeerOrch] Reclaim: stream PLAYING on self — REMOVE sent to '%s' for '%s'. Reclaim complete.",
                holder_node, camera_id,
            )
        except Exception as exc:
            logger.error(
                "[PeerOrch] Reclaim: failed to send REMOVE to '%s' for '%s': %s",
                holder_node, camera_id, exc,
            )

    # ------------------------------------------------------------------
    # Vote ack
    # ------------------------------------------------------------------

    def _on_vote_ack(self, payload: dict) -> None:
        """Receive ack that stream is PLAYING on winner node.

        Phase 3 review fix 4 — the ACK is authenticated against the expected
        winner/migration identity before it can release the reservation or
        trigger the REMOVE: the payload ``node_id`` (the winner, as published
        by ZenohCommandSubscriber) must equal the stored ``_pending_winner``
        for that camera.  A stale, wrong, or duplicate ACK therefore cannot
        release a reservation it does not own, and cannot set the pending-ack
        event (which is what drives the requester's REMOVE in _wait_and_remove).
        Fail-closed for ambiguous ACKs; safe legacy compatibility: a legacy ACK
        without ``node_id`` is only honoured when no migration is pending for
        that camera (i.e. there is nothing to authenticate against).
        """
        camera_id = payload.get("camera_id", "")
        if not camera_id:
            return
        ack_node = payload.get("node_id", "")
        event_type = payload.get("event")

        # Fail closed on clearly-invalid ACK semantics.
        if event_type is not None and event_type != "PLAYING":
            logger.warning(
                "[PeerOrch] Ignoring ACK for '%s': event='%s' != 'PLAYING'.",
                camera_id, event_type,
            )
            return

        with self._lock:
            expected_winner = self._pending_winner.get(camera_id)
            event = self._pending_acks.get(camera_id)

            if expected_winner is not None and ack_node not in ("", expected_winner):
                # Wrong/forged/stale sender for an in-flight migration — fail closed.
                logger.warning(
                    "[PeerOrch] Ignoring ACK for '%s': sender='%s' != expected winner='%s'.",
                    camera_id, ack_node, expected_winner,
                )
                return
            if expected_winner is None and ack_node not in ("", self._node_id):
                # No pending migration but a foreign sender claims this camera —
                # ambiguous, fail closed (do not set event, do not release).
                logger.warning(
                    "[PeerOrch] Ignoring ACK for '%s': no pending migration but "
                    "sender='%s' != self.", camera_id, ack_node,
                )
                return

            # Authenticated — now atomically claim the reservation if we own it.
            winner_id = self._pending_winner.pop(camera_id, None)
            if winner_id is not None:
                self._peer_inflight[winner_id] = max(
                    0, self._peer_inflight.get(winner_id, 0) - 1
                )
            if event is not None:
                event.set()

        if winner_id is not None:
            logger.info(
                "[PeerOrch] Ack received for '%s' from '%s' — stream is PLAYING. "
                "Reservation released (inflight=%d).",
                camera_id, ack_node, self._peer_inflight.get(winner_id, 0),
            )
        elif event is not None:
            logger.info("[PeerOrch] Ack received for '%s' — stream is PLAYING.", camera_id)

    # ------------------------------------------------------------------
    # Leaderless failover (Phase 5)
    # ------------------------------------------------------------------

    @staticmethod
    def _consistent_hash(camera_id: str, peer_ids: List[str]) -> str:
        """
        Deterministic hash: all nodes use sorted(peer_ids) → same input → same output.
        """
        alive = sorted(peer_ids)
        key = int(hashlib.sha256(camera_id.encode()).hexdigest(), 16)
        return alive[key % len(alive)]

    def _leaderless_failover(self, dead_node_id: str, orphaned_cameras: List[str]) -> None:
        """
        Rescue orphaned cameras using consistent hash.

        Each surviving peer runs independently → same hash result.
        Winner executes ADD after jitter (0-2s) to avoid race.
        After jitter, check peers/status/+ to see if camera already rescued.
        """
        cfg = self._cfg
        now = time.time()
        timeout = cfg.get("heartbeat_timeout_s", 5.0)

        # Read dead peer's camera configs AND build alive_peers in a single lock
        # acquisition to prevent torn reads between the two operations.
        with self._lock:
            dead_peer = self._peers.get(dead_node_id)
            peer_cam_configs = dead_peer.camera_configs if dead_peer else {}

            # Build alive candidate list — includes self so this node can rescue too
            # BUG-1 fix: read active_cameras from _self_state under _self_lock.
        with self._self_lock:
            self_streams = len(self._self_state.active_cameras)
        with self._lock:
            self_eligible = (
                self._node_id != dead_node_id
                and self_streams < self._cfg.get("eps_streams_max", 4)
            )
            alive_peers = sorted([
                nid for nid, peer in self._peers.items()
                if nid != dead_node_id
                and now - peer.last_seen <= timeout
                and len(peer.active_cameras) < peer.max_streams
            ] + ([self._node_id] if self_eligible else []))

        # BUG-11: Track how many cameras this node has accepted during this
        # failover loop so we don't exceed eps_streams_max across iterations.
        self_accepted = 0

        # Single jitter sleep BEFORE the loop to avoid race conditions with
        # other peers, without accumulating delay per camera.
        jitter = random.uniform(0, cfg.get("failover_jitter_max_s", 2.0))
        time.sleep(jitter)

        for camera_id in orphaned_cameras:
            if not alive_peers:
                logger.error("[Failover] No alive peers to rescue '%s'", camera_id)
                continue

            winner = self._consistent_hash(camera_id, alive_peers)

            if winner == self._node_id:
                # Re-check capacity including cameras accepted earlier in this loop
                # BUG-1 fix: read active_cameras under _self_lock
                with self._self_lock:
                    current_streams = len(self._self_state.active_cameras)
                if current_streams + self_accepted >= self._cfg.get("eps_streams_max", 4):
                    logger.warning(
                        "[Failover] Cannot rescue '%s': at stream capacity (%d). Skipping.",
                        camera_id, current_streams + self_accepted,
                    )
                    continue

                # Double-check: camera already rescued by another peer?
                # Exclude both self AND the dead peer — the dead peer's stale
                # active_cameras still lists its own cameras and would otherwise
                # cause every rescue to be skipped in a 2-node cluster.
                with self._lock:
                    already_handled = any(
                        camera_id in peer.active_cameras
                        for nid, peer in self._peers.items()
                        if nid != self._node_id and nid != dead_node_id
                    )
                if already_handled:
                    logger.info(
                        "[Failover] Camera '%s' already rescued. Skipping.",
                        camera_id,
                    )
                    continue

                # Use camera config from the dead peer's heartbeat (original URIs),
                # fall back to local cameras.yml only if peer didn't share configs.
                cam_config = peer_cam_configs.get(camera_id) or self._get_camera_config(camera_id)
                if cam_config is None:
                    logger.error("[Failover] No config for '%s'. Skipping.", camera_id)
                    continue

                # Verify the camera RTSP source is reachable before adding.
                # If the source is hosted on the dead node, it's unreachable.
                cam_uri = cam_config.get("uri", "")
                rtt = self._measure_rtt(cam_uri)
                if rtt is None:
                    logger.warning(
                        "[Failover] Camera '%s' source unreachable (%s). Skipping.",
                        camera_id, cam_uri,
                    )
                    continue

                add_cmd = {**cam_config, "cmd": "ADD"}
                self._pubs["control"].put(msgpack.packb(add_cmd, use_bin_type=True))
                self_accepted += 1
                self._rescued_cameras[camera_id] = dead_node_id
                # ponytail: rescue ADD — record so _pick_camera_to_offload
                # applies the per-camera warmup gate to this camera too.
                self._camera_added_at[camera_id] = time.time()
                self._camera_first_valid_fps_at.pop(camera_id, None)
                logger.info("[Failover] Rescue ADD sent: '%s' → me (rtt=%.0fms)", camera_id, rtt)

                self._migration_log.log(
                    dead_node_id, self._node_id, camera_id,
                    "node_offline", 0.0, 0.0,
                    0.0, "FAILOVER_ADD",
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _measure_rtt(self, rtsp_uri: str) -> Optional[float]:
        """
        Verify an RTSP stream is reachable AND the path exists.

        Sends an RTSP DESCRIBE request.  Returns RTT in ms on success
        (any 2xx response), or None if the host is down or the path
        does not exist (4xx).
        """
        try:
            parsed = urllib.parse.urlparse(rtsp_uri)
            host = parsed.hostname
            port = parsed.port or 554
            t0 = time.monotonic()
            with socket.create_connection((host, port), timeout=0.5) as sock:
                # Send a minimal RTSP DESCRIBE to check if the path exists
                req = (
                    f"DESCRIBE {rtsp_uri} RTSP/1.0\r\n"
                    f"CSeq: 1\r\n"
                    f"Accept: application/sdp\r\n"
                    f"\r\n"
                )
                sock.sendall(req.encode())
                resp = sock.recv(512).decode(errors="ignore")
                rtt = (time.monotonic() - t0) * 1000.0
                # Accept any 2xx response; reject 404/401/etc.
                if resp.startswith("RTSP/1.0 2"):
                    return rtt
                return None
        except Exception:
            return None

    def _get_camera_config(self, camera_id: str) -> Optional[dict]:
        """
        Read camera config from cameras.yml.

        BUG-2 fix: prefer the live CameraManager when available — it already
        maintains a hot-reloaded, up-to-date config dict so we never serve
        stale homography/ROI/URI data after a cameras.yml change.  Fall back
        to a YAML parse only when the manager is not set (standalone tests).

        BUG-15 (original BUG-15 from bug report): cache is now irrelevant
        because CameraManager owns the in-memory state.  The _cameras_cache
        field is kept for the YAML fallback path only.
        """
        # Fast path: use live CameraManager (always up-to-date)
        if self._camera_manager is not None:
            try:
                cfg_obj = None
                # CameraManager stores CameraConfig objects keyed by camera_id
                with self._camera_manager._lock:
                    cfg_obj = self._camera_manager._configs.get(camera_id)
                if cfg_obj is not None:
                    return {
                        "camera_id":       camera_id,
                        "source_id":       int(cfg_obj.source_id),
                        "uri":             cfg_obj.uri,
                        "name":            cfg_obj.name,
                        "fps":             float(cfg_obj.fps),
                        "speed_limit_kmh": float(cfg_obj.speed_limit_kmh),
                        "homography": {
                            "source_points": cfg_obj.source_points.tolist(),
                            "target_width":  int(cfg_obj.target_points[2, 0]),
                            "target_height": int(cfg_obj.target_points[2, 1]),
                        },
                        "roi_polygon": cfg_obj.roi_polygon.tolist(),
                        "output": {
                            "record":      cfg_obj.record,
                            "record_path": cfg_obj.record_path,
                        },
                    }
            except Exception as exc:
                logger.debug("CameraManager config lookup failed for '%s': %s", camera_id, exc)

        # Fallback: parse cameras.yml (used in tests / standalone mode)
        # BUG-2 fix: invalidate the cache based on file mtime so hot-reload is
        # respected even in the fallback path.
        try:
            import yaml
            yml_path = self._camera_configs_dir / "cameras.yml"
            try:
                current_mtime = yml_path.stat().st_mtime
            except OSError:
                current_mtime = 0.0

            if (self._cameras_cache is None
                    or getattr(self, "_cameras_cache_mtime", None) != current_mtime):
                with open(yml_path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                self._cameras_cache = raw.get("cameras", {})
                self._cameras_cache_mtime = current_mtime

            cameras = self._cameras_cache
            cfg = cameras.get(camera_id)
            if not cfg:
                return None
            return {
                "camera_id":       camera_id,
                "source_id":       int(cfg.get("source_id", 0)),
                "uri":             cfg.get("uri", ""),
                "name":            cfg.get("name", camera_id),
                "fps":             float(cfg.get("fps", 25.0)),
                "speed_limit_kmh": float(cfg.get("speed_limit_kmh", 80.0)),
                "homography":      cfg.get("homography", {}),
                "roi_polygon":     cfg.get("roi_polygon", []),
                "output":          cfg.get("output", {}),
            }
        except Exception as exc:
            logger.error("Failed to load camera config for '%s': %s", camera_id, exc)
            return None

    # ponytail: single rate-limiter for blocked-decision diagnostics so they
    # don't spew every 1-second tick.  Each "block reason" key gets logged at
    # most once every BLOCK_LOG_COOLDOWN seconds (default 15 s).  Increase to
    # 30 s if still too noisy.
    BLOCKED_LOG_COOLDOWN = 15.0

    def _maybe_log_block(self, reason: str, now: float) -> bool:
        """Return True the first time `reason` fires or after its cooldown expires."""
        last = self._blocked_logged_at.get(reason, 0.0)
        if now - last >= self.BLOCKED_LOG_COOLDOWN:
            self._blocked_logged_at[reason] = now
            return True
        return False

    def _get_camera_uri(self, camera_id: str) -> Optional[str]:
        """Get RTSP URI of camera from cameras.yml."""
        cfg = self._get_camera_config(camera_id)
        if cfg:
            return cfg.get("uri")
        return None

    def _get_owned_camera_ids(self) -> set:
        """
        Return the set of camera IDs this node is configured to own.

        Ownership = cameras enabled in this node's local cameras.yml. Rescued
        and migrated-in cameras are NOT owned by this node — they live on a
        peer's cameras.yml. The helper is consumed by _pick_camera_to_offload
        to enforce the invariant "at least one locally-owned camera remains
        active during full-stream L1 offload".

        Hot reload:
          * Preferred path: live CameraManager (inotify-watched inotify
            handler reloads _configs on every cameras.yml change).
          * Fallback path: cameras.yml on disk, cached by mtime so a change
            is picked up on the next call after the file is rewritten.

        Returns an empty set on any error — fail safe (the L1 caller
        treats empty ownership as "fail safe, don't migrate").
        """
        # Fast path: live CameraManager (hot-reloaded via inotify)
        cm = self._camera_manager
        if cm is not None:
            try:
                with cm._lock:
                    return {
                        c.camera_id for c in cm._configs.values()
                        if getattr(c, "enabled", False)
                    }
            except Exception as exc:
                logger.debug(
                    "[PeerOrch] CameraManager ownership lookup failed: %s", exc
                )

        # Fallback: cameras.yml with mtime-based cache invalidation.
        # Mirrors the same pattern used in _get_camera_config so reloads
        # are visible without restarting the orchestrator.
        try:
            import yaml
            yml_path = self._camera_configs_dir / "cameras.yml"
            try:
                current_mtime = yml_path.stat().st_mtime
            except OSError:
                current_mtime = 0.0

            if (self._cameras_cache is None
                    or getattr(self, "_cameras_cache_mtime", None) != current_mtime):
                with open(yml_path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                self._cameras_cache = (raw or {}).get("cameras", {}) or {}
                self._cameras_cache_mtime = current_mtime

            return {
                cam_id for cam_id, cfg in (self._cameras_cache or {}).items()
                if isinstance(cfg, dict) and bool(cfg.get("enabled", True))
            }
        except Exception as exc:
            logger.debug(
                "[PeerOrch] cameras.yml ownership lookup failed: %s", exc
            )
            return set()

    def _pick_camera_to_offload(self, state: PeerState, level: int) -> Optional[str]:
        """
        Select camera to offload based on intended offload level.

        Level 1 (full-stream migration): choose the **lightest** eligible camera
        (min workload) — migrate the easiest stream, keep heavy cameras local.

        Level 2 / Level 3 (crop offload): choose the **heaviest** eligible camera
        (max workload) — offload the most impactful camera to a peer as crop work.

        Workload comes from the health payload's camera_workload mapping
        (n_track + n_plate per camera) — NOT output FPS, which is a poor proxy
        for processing cost.  A candidate must have a finite, non-negative
        workload; cameras with missing evidence are skipped.  If no candidate
        has workload evidence, return None (fail safe) — never rank on FPS.

        Source-starved cameras (health agent reports them starved) and cameras
        within the reclaim post-return observation window are ineligible.

        Never offload the last camera — node must keep at least 1 camera
        to continue operation. If only 1 camera remains and still overloaded,
        that's a hardware limit that cannot be solved by migration.

        L1 ownership invariant
        ──────────────────────
        Full-stream migration removes a stream from this node. We must keep
        at least one locally-owned camera (enabled in this node's cameras.yml)
        active at all times. Rescued/migrated-in cameras are NOT owned by
        this node — the previous selector kept one arbitrary active camera,
        so a foreign camera could "stand in" while every owned camera got
        migrated away.

          * If no owned camera is active → fail safe (return None).
          * Otherwise, among eligible candidates, pick the lightest whose
            migration still leaves ≥1 owned camera active.

        L2/L3 crop offload does NOT remove the stream, so the ownership
        guard does not apply — it is allowed to pick the last owned camera
        as a crop source.
        """
        if len(state.active_cameras) <= 1:
            if state.active_cameras:
                logger.debug(
                    "[PeerOrch] Only 1 camera left ('%s') — cannot offload last camera",
                    state.active_cameras[0],
                )
            return None

        now = time.time()
        reclaim_stability = self._cfg.get("reclaim_stability_s", 30.0)
        starved = set(state.source_starved_cameras or [])

        # ponytail: per-camera warmup gate.  A freshly-ADDed camera whose FPS
        # hasn't stabilised is ineligible for offload — its workload is
        # untrustworthy and offloading it would just thrash.  Cameras NOT
        # recorded in _camera_added_at are pre-existing and skip the gate,
        # which keeps the existing L1/L2/L3 selector tests passing.
        camera_warmup_s = self._cfg.get(
            "camera_warmup_s", self._cfg.get("overload_warmup_s", 10.0),
        )

        def _camera_warming_up(cam_id: str) -> bool:
            if cam_id not in self._camera_added_at:
                return False  # pre-existing — no warmup
            first_fps = self._camera_first_valid_fps_at.get(cam_id)
            if first_fps is None:
                return True   # added but no valid FPS observed yet
            return (now - first_fps) < camera_warmup_s

        # Workload evidence must come from the health payload. Require a
        # finite, non-negative workload per camera; missing/malformed values
        # are skipped (fail safe). Do NOT fall back to output FPS.
        workload = state.camera_workload or {}
        eligible = {}
        for c in state.active_cameras:
            if c in starved:
                continue
            if now - self._reclaim_completed_at.get(c, 0.0) < reclaim_stability:
                continue
            if _camera_warming_up(c):
                continue
            if c not in workload:
                continue
            w = workload[c]
            if not (isinstance(w, (int, float))
                    and not isinstance(w, bool)
                    and math.isfinite(w)
                    and w >= 0):
                continue
            eligible[c] = float(w)

        if not eligible:
            return None

        if level == 1:
            # L1 ownership guard: never migrate away the last owned camera.
            owned_active = self._get_owned_camera_ids() & set(state.active_cameras)
            if not owned_active:
                # Fail safe: nothing locally-owned is active. Even if a foreign
                # (rescued/migrated-in) camera is available, do not migrate —
                # the node would end up owning zero local streams.
                logger.warning(
                    "[PeerOrch] L1 fail-safe: no locally-owned camera is active "
                    "(active=%d). Skipping migration to preserve ownership.",
                    len(state.active_cameras),
                )
                return None
            # Walk candidates lightest-first; skip any whose migration would
            # leave zero owned cameras active. The first one that preserves
            # ≥1 owned camera wins.
            for c in sorted(eligible, key=lambda cam: eligible[cam]):
                if c not in owned_active or (owned_active - {c}):
                    return c
            # Every eligible candidate would zero out owned cameras — fail safe.
            logger.warning(
                "[PeerOrch] L1 fail-safe: every eligible candidate would leave "
                "zero owned cameras active (eligible=%d, owned_active=%d).",
                len(eligible), len(owned_active),
            )
            return None

        # L2/L3 crop offload: heaviest camera = max workload. The crop
        # offload keeps the stream local; ownership guard does not apply.
        return max(eligible, key=lambda c: eligible[c])
