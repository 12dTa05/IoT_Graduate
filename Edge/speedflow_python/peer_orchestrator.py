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
from typing import Dict, List, Optional, Tuple

import msgpack

from .zenoh_session import make_session

# Settings loaded from Edge/.env
from .settings import ROOT as _ROOT

def _setup_logging() -> logging.Logger:
    raw_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, raw_level, logging.INFO)
    root_level = logging.INFO if raw_level == "DEBUG" else level

    if not logging.root.handlers:
        logging.basicConfig(
            level=root_level,
            format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        logging.root.setLevel(root_level)

    if raw_level == "DEBUG":
        for name in (
            "peer_orchestrator",
            "health_agent",
            "speedflow_python.probes",
            "speedflow_python.offload_receiver",
        ):
            logging.getLogger(name).setLevel(logging.DEBUG)

    return logging.getLogger("peer_orchestrator")

logger = _setup_logging()

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
    streaming_cameras: List[str] = field(default_factory=list)
    camera_configs: Dict[str, dict] = field(default_factory=dict)
    camera_owners: Dict[str, str] = field(default_factory=dict)
    camera_holders: Dict[str, str] = field(default_factory=dict)
    camera_epochs: Dict[str, int] = field(default_factory=dict)
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
    # Rate of offload crops received per second (for Level 2/3 peer evaluation)
    offload_crops_received_per_s: float = 0.0
    load_score_breakdown: Dict[str, float] = field(default_factory=dict)
    status: Optional[str] = None
    pipeline_idle: bool = False
    workload_ema: Optional[float] = None
    fps_ema: Optional[float] = None
    qos_state: Optional[str] = None


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


def _has_valid_or_unreported_fps(fps_per_camera) -> bool:
    """Return True if fps_per_camera has valid positive fps OR is not reported (dict empty/None).

    Used for rebalance backward-compatibility where older/synthetic peers might not populate fps_per_camera.
    """
    if fps_per_camera is None or (isinstance(fps_per_camera, dict) and len(fps_per_camera) == 0):
        return True
    return _has_valid_positive_fps(fps_per_camera)


def is_waiting_state(fps_per_camera, active_cameras: Optional[List[str]] = None, status: Optional[str] = None) -> bool:
    """Predicate for WAITING/RECOVERY state.

    Returns True if:
      - status string is explicitly 'waiting', 'recovering', or 'recovery', OR
      - active_cameras is empty or None (len == 0), OR
      - fps_per_camera is empty ({}, None) or has no valid positive FPS.

    In this state, the node is waiting/recovering and must NEVER be classified
    as overloaded or offline, and must not trigger failover/rescue or be picked as migration destination.
    """
    if status and str(status).strip().lower() in ("waiting", "recovering", "recovery"):
        return True
    if active_cameras is not None and len(active_cameras) == 0:
        return True
    if not _has_valid_positive_fps(fps_per_camera):
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
            # File size rotation guard: rotate if CSV exceeds 10MB to avoid filling Jetson eMMC
            if self._path.exists() and self._path.stat().st_size > 10 * 1024 * 1024:
                backup = self._path.with_suffix(".csv.old")
                try:
                    self._path.replace(backup)
                except Exception:
                    pass
                with open(self._path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self.HEADER)

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

        # Startup validation: add_ack_timeout_s must be strictly less than migration_timeout_s
        migration_timeout = float(self._cfg.get("migration_timeout_s", 15.0))
        if "add_ack_timeout_s" in self._cfg and self._cfg["add_ack_timeout_s"] is not None:
            ack_timeout = float(self._cfg["add_ack_timeout_s"])
            if ack_timeout >= migration_timeout:
                raise ValueError(
                    f"add_ack_timeout_s ({ack_timeout:.1f}s) must be strictly less than "
                    f"migration_timeout_s ({migration_timeout:.1f}s) to ensure sender timeout safety"
                )

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

        # Status/heartbeat publish failure tracking
        self._status_sent_count = 0
        self._status_error_count = 0
        self._status_consecutive_errors = 0
        self._last_status_sent_time: Optional[float] = None
        self._last_status_error_time: Optional[float] = None
        # Cameras with RFO sent but vote window still open (prevent re-trigger)
        self._vote_in_progress: set = set()
        # RFO trigger snapshots: camera_id -> (trigger_load, trigger_fps)
        self._rfo_snapshots: Dict[str, Tuple[float, Optional[float]]] = {}

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
        self._failover_triggered: Dict[str, float] = {}

        # Rescue claims received from other nodes: (dead_node_id, camera_id) → (node_id, local_received_ts, priority_weight, remote_ts)
        self._failover_claims: Dict[Tuple[str, str], Tuple[str, float, int, float]] = {}
        self._claims_lock = threading.Lock()

        # Cameras rescued via failover: camera_id → original_owner_node_id
        # Used to return cameras when the original owner comes back online.
        self._rescued_cameras: Dict[str, str] = {}
        # Timestamps when cameras were rescued: camera_id → timestamp
        self._rescued_at: Dict[str, float] = {}

        # Duplicate camera observation counts for safe duplicate reconciliation:
        # (peer_node_id, camera_id) -> count of consecutive heartbeat observations
        self._duplicate_camera_seen: Dict[Tuple[str, str], int] = {}

        # Cameras migrated away due to overload: camera_id → winner_node_id
        # Used to reclaim cameras when this node's load drops below threshold.
        self._migrated_out: Dict[str, str] = {}

        # Per-camera migration timestamp history for bounce dampening: camera_id -> list[timestamp]
        self._cam_migration_history: Dict[str, List[float]] = {}

        # Consecutive migration ACK timeouts per target peer: node_id -> count
        self._peer_consecutive_timeouts: Dict[str, int] = {}

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
        self._pending_started_at: Dict[str, float] = {}

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

        # Round-robin counter for baseline experiment policy
        self._rr_counter: int = 0

        # Reclaim retry tracking: camera_id → timestamp when reclaim can be retried
        self._reclaim_retry_at: Dict[str, float] = {}
        self._reclaim_retry_count: Dict[str, int] = {}
        self._reclaim_attempts: Dict[str, int] = {}
        self._reclaim_pending_remove: Dict[str, str] = {}
        # Cameras currently undergoing reclaim Make-before-Break
        self._reclaim_in_progress: set = set()
        # Tracking camera owner epochs and active migration IDs
        self._camera_epochs: Dict[str, int] = {}
        self._camera_migration_ids: Dict[str, str] = {}
        self._pending_migration_ids: Dict[str, str] = {}
        self._pending_epochs: Dict[str, int] = {}
        self._reclaim_pending_remove_epoch: Dict[str, int] = {}
        self._reclaim_pending_remove_mig_id: Dict[str, str] = {}
        self._reclaim_remove_acks: Dict[str, threading.Event] = {}

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

        # Track when any peer was detected offline (node_id -> timestamp)
        # to ensure failover_convergence_grace activates for all peer departures.
        self._peer_offline_at: Dict[str, float] = {}

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

        # Per-camera timestamp when offload (L2/L3) was started
        self._offload_started_at: Dict[str, float] = {}

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

        # Declared publishers
        self._pubs["status"]        = self._session.declare_publisher(f"peers/status/{self._node_id}")
        self._pubs["vote_request"]  = self._session.declare_publisher("peers/vote/request")
        self._pubs["vote_proposal"] = self._session.declare_publisher("peers/vote/proposal")
        self._pubs["vote_decision"] = self._session.declare_publisher("peers/vote/decision")
        self._pubs["control"]       = self._session.declare_publisher(f"peers/control/{self._node_id}")
        self._pubs["failover_claim"] = self._session.declare_publisher("peers/failover/claim")

        # Subscribe to all P2P topics
        self._session.declare_subscriber("peers/status/**",      self._on_sample)
        self._session.declare_subscriber("peers/vote/request",   self._on_sample)
        self._session.declare_subscriber("peers/vote/proposal",  self._on_sample)
        self._session.declare_subscriber("peers/vote/decision",  self._on_sample)
        self._session.declare_subscriber("peers/vote/ack/**",    self._on_sample)
        self._session.declare_subscriber("peers/remove/ack/**",  self._on_sample)
        self._session.declare_subscriber("peers/failover/claim", self._on_sample)
        logger.info("[PeerOrch] Subscribed to: peers/status/**, peers/vote/*, peers/vote/ack/**, peers/remove/ack/**, peers/failover/claim")

        self._running = True
        self._ready_event.set()

        # Startup preemption announcement: notify cluster of owned cameras so peers holding them release immediately
        self._publish_startup_announcement()

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
            try:
                pub.put(payload)
                self._status_sent_count += 1
                self._status_consecutive_errors = 0
                self._last_status_sent_time = time.time()
            except Exception as exc:
                self._status_error_count += 1
                self._status_consecutive_errors += 1
                self._last_status_error_time = time.time()
                if self._status_consecutive_errors == 1 or self._status_consecutive_errors % 10 == 0:
                    logger.warning(
                        "[PeerOrch] Status publish failed (consecutive=%d, total=%d): %s",
                        self._status_consecutive_errors, self._status_error_count, exc,
                    )

    def update_self_state(self, payload: dict) -> None:
        """Update this node's local state without publishing a Zenoh heartbeat.

        Thin compatibility wrapper routing directly to _on_peer_status(payload).
        """
        if not isinstance(payload, dict):
            return
        if "node_id" not in payload:
            payload = dict(payload)
            payload["node_id"] = self._node_id
        self._on_peer_status(payload)

    def get_ownership_records(self) -> Dict[str, dict]:
        """
        Return thread-safe snapshot of locally active/owned camera ownership records.
        Distinguishes static/original owner from current holder.
        Maps camera_id -> {"owner": str, "holder": str, "epoch": int, "migration_id": Optional[str]}
        """
        records = {}
        static_owned = self._get_owned_camera_ids()
        with self._lock:
            with self._self_lock:
                active_cams = list(self._self_state.active_cameras)
            for cam_id in active_cams:
                epoch = self._camera_epochs.get(cam_id, 1)
                mig_id = self._camera_migration_ids.get(cam_id)
                # Static owner is this node if configured in cameras.yml;
                # otherwise check rescued_cameras or fallback to static mapping.
                orig_owner = self._rescued_cameras.get(cam_id)
                if orig_owner is None:
                    if cam_id in static_owned:
                        orig_owner = self._node_id
                    else:
                        # Check configured mapping for any peer
                        node_cam_map = self._cfg.get("node_camera_map")
                        if isinstance(node_cam_map, dict):
                            for nid, cams in node_cam_map.items():
                                if isinstance(cams, (list, tuple, set)) and cam_id in cams:
                                    orig_owner = nid
                                    break
                rec = {
                    "owner": orig_owner,
                    "holder": self._node_id,
                    "epoch": epoch,
                }
                if mig_id is not None:
                    rec["migration_id"] = mig_id
                records[cam_id] = rec
        return records

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
            now = time.time()
            self._offload_level_changed_at[camera_id] = now
            if level > 0 and old == 0:
                self._offload_started_at[camera_id] = now
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
        elif key.startswith("peers/remove/ack/"):
            self._on_remove_ack(payload)
        elif key == "peers/failover/claim":
            self._on_failover_claim(payload)
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
                self._self_state.load_score_breakdown = payload.get("load_score_breakdown", {})
                self._self_state.workload_ema = payload.get("workload_ema")
                self._self_state.fps_ema = payload.get("fps_ema")
                self._self_state.qos_state = payload.get("qos_state")
                self._self_state.gpu_percent = payload.get("gpu_percent", 0.0)
                self._self_state.cpu_percent = payload.get("cpu_percent", 0.0)
                self._self_state.ram_percent = payload.get("ram_percent", 0.0)
                self._self_state.gpu_temp_c  = payload.get("gpu_temp_c",  0.0)
                self._self_state.risk_index  = payload.get("risk_index",  0.0)
                pipeline = payload.get("pipeline", {}) or {}
                self._self_state.status = pipeline.get("status")
                self._self_state.pipeline_idle = bool(pipeline.get("pipeline_idle", False))
                self._self_state.avg_fps = pipeline.get("avg_fps")
                # Prefer output_fps_per_camera (Phase 1 unambiguous key); fall back
                # to fps_per_camera for backward compatibility with older firmware.
                self._self_state.fps_per_camera = _pick_fps_dict(pipeline)
                if isinstance(pipeline.get("active_cameras"), (list, tuple, set)):
                    self._self_state.active_cameras = [str(c) for c in pipeline["active_cameras"] if isinstance(c, (str, int))]
                if isinstance(pipeline.get("streaming_cameras"), (list, tuple, set)):
                    self._self_state.streaming_cameras = [str(c) for c in pipeline["streaming_cameras"] if isinstance(c, (str, int))]
                else:
                    self._self_state.streaming_cameras = list(self._self_state.active_cameras)
                raw_configs = pipeline.get("camera_configs")
                if isinstance(raw_configs, dict) and raw_configs:
                    self._self_state.camera_configs = raw_configs
                elif isinstance(raw_configs, dict) and len(raw_configs) == 0:
                    if (
                        pipeline.get("status") is not None
                        and str(pipeline.get("status")).strip().lower() not in ("waiting", "recovering", "recovery", "starting")
                        and "active_cameras" in pipeline
                        and len(self._self_state.active_cameras) == 0
                    ):
                        self._self_state.camera_configs = {}
                # Backwards-compatible: missing or malformed mapping → empty.
                self._self_state.camera_workload = _parse_camera_workload(
                    pipeline.get("camera_workload", {})
                )
                self._self_state.source_starved_cameras = _parse_starved_cameras(
                    pipeline.get("source_starved_cameras", [])
                )
                self._self_state.offload_crops_received_per_s = float(
                    pipeline.get("offload_crops_received_per_s", 0.0) or 0.0
                )
                self._self_state.last_seen = time.time()
                try:
                    self._self_state.max_streams = int(pipeline.get("max_streams", 8) or 8)
                except (TypeError, ValueError):
                    self._self_state.max_streams = 8

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
                # positive FPS samples AND not be in waiting/recovery state.
                # load_score=100 with empty/zero/NaN fps is meaningless without
                # running streams — dashboard keeps the raw load_score, but the decision path is gated.
                in_waiting = is_waiting_state(
                    self._self_state.fps_per_camera,
                    self._self_state.active_cameras,
                    pipeline.get("status"),
                )
                overloaded = (
                    self._is_overloaded(
                        self._self_state.load_score,
                        self._self_state.risk_index,
                        self._self_state.qos_state,
                    )
                    and fps_valid
                    and not in_waiting
                )
                if overloaded:
                    if self._self_state.overload_since is None:
                        self._self_state.overload_since = time.time()
                    # Only reset reclaim eligibility outside the post-reclaim
                    # transition settle window — same guard as update_self_state.
                    if time.time() <= self._transition_settle_until:
                        pass  # preserve _reclaim_eligible_since
                    else:
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
            peer.load_score_breakdown = payload.get("load_score_breakdown", {})
            peer.workload_ema = payload.get("workload_ema")
            peer.fps_ema = payload.get("fps_ema")
            peer.qos_state = payload.get("qos_state")
            peer.gpu_percent = payload.get("gpu_percent", 0.0)
            peer.cpu_percent = payload.get("cpu_percent", 0.0)
            peer.ram_percent = payload.get("ram_percent", 0.0)
            peer.gpu_temp_c  = payload.get("gpu_temp_c",  0.0)
            peer.risk_index  = payload.get("risk_index",  0.0)

            pipeline = payload.get("pipeline", {}) or {}
            peer.status         = pipeline.get("status")
            peer.pipeline_idle  = bool(pipeline.get("pipeline_idle", False))
            peer.avg_fps        = pipeline.get("avg_fps")
            # Prefer output_fps_per_camera (Phase 1 unambiguous key); fall back
            # to fps_per_camera for backward compatibility with older firmware.
            peer.fps_per_camera = _pick_fps_dict(pipeline)
            if not peer.fps_per_camera and peer.avg_fps and isinstance(peer.avg_fps, (int, float)) and peer.avg_fps > 0:
                # If per-camera fps dict is omitted in older/synthetic payloads, populate from avg_fps for active cameras
                if isinstance(pipeline.get("active_cameras"), (list, tuple, set)):
                    peer.fps_per_camera = {str(c): float(peer.avg_fps) for c in pipeline["active_cameras"] if isinstance(c, (str, int))}
            if isinstance(pipeline.get("active_cameras"), (list, tuple, set)):
                peer.active_cameras = [str(c) for c in pipeline["active_cameras"] if isinstance(c, (str, int))]
            if isinstance(pipeline.get("streaming_cameras"), (list, tuple, set)):
                peer.streaming_cameras = [str(c) for c in pipeline["streaming_cameras"] if isinstance(c, (str, int))]
            else:
                peer.streaming_cameras = list(peer.active_cameras)
            # Preserve valid prior camera_configs when payload field is missing/malformed
            raw_configs = pipeline.get("camera_configs")
            if isinstance(raw_configs, dict) and raw_configs:
                peer.camera_configs = raw_configs
            elif isinstance(raw_configs, dict) and len(raw_configs) == 0:
                # Explicit valid empty snapshot only if pipeline is running and status indicates explicit state
                if (
                    pipeline.get("status") is not None
                    and str(pipeline.get("status")).strip().lower() not in ("waiting", "recovering", "recovery", "starting")
                    and "active_cameras" in pipeline
                    and len(peer.active_cameras) == 0
                ):
                    peer.camera_configs = {}
                # Otherwise (e.g. transient empty dictionary during recovery or startup), preserve prior valid configs
            # Backwards-compatible: missing or malformed mapping → empty.
            peer.camera_workload = _parse_camera_workload(
                pipeline.get("camera_workload", {})
            )
            peer.source_starved_cameras = _parse_starved_cameras(
                pipeline.get("source_starved_cameras", [])
            )
            peer.offload_crops_received_per_s = float(
                pipeline.get("offload_crops_received_per_s", 0.0) or 0.0
            )
            peer.camera_owners = payload.get("camera_owners", {}) or {}
            peer.camera_holders = payload.get("camera_holders", {}) or {}
            peer.camera_epochs = payload.get("camera_epochs", {}) or {}
            peer.last_seen = time.time()
            # max_streams from peer health payload; malformed values fall back to 8
            try:
                peer.max_streams = int(pipeline.get("max_streams", 8) or 8)
            except (TypeError, ValueError):
                peer.max_streams = 8

            # Track overload onset using same proactive-aware helper.
            # Gate on valid positive FPS: load_score=100 with fps={} means the
            # peer pipeline is unavailable (pipeline_available=False) or in waiting/recovery,
            # NOT genuinely overloaded.  Without this gate a newly-started peer
            # would appear overloaded to election logic before its pipeline runs.
            peer_fps_valid = _has_valid_positive_fps(peer.fps_per_camera)
            peer_in_waiting = is_waiting_state(
                peer.fps_per_camera,
                peer.active_cameras,
                pipeline.get("status"),
            )
            overloaded = (
                self._is_overloaded(
                    peer.load_score,
                    peer.risk_index,
                    peer.qos_state,
                )
                and peer_fps_valid
                and not peer_in_waiting
            )
            if overloaded:
                if peer.overload_since is None:
                    peer.overload_since = time.time()
            else:
                peer.overload_since = None

            # Return / yield rescued camera only when owner is alive AND ready:
            # heartbeat fresh, peer not in waiting, valid positive FPS, not overloaded,
            # and owner confirms destination active / PLAYING evidence.
            peer_ready_to_resume = (
                peer_fps_valid
                and not peer_in_waiting
                and not overloaded
            )
            cameras_to_yield = [
                cam_id for cam_id, orig_owner in self._rescued_cameras.items()
                if orig_owner == node_id and (
                    peer_ready_to_resume
                    and cam_id in peer.active_cameras
                )
            ]
            for cam_id in cameras_to_yield:
                self._rescued_cameras.pop(cam_id, None)
                self._rescued_at.pop(cam_id, None)
                remove_cmd = self._build_remove_cmd(cam_id, context="immediate_yield")
                if self._pubs.get("control") is not None:
                    self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
                logger.info(
                    "[PeerOrch] Original owner '%s' resumed '%s' (fps_valid=%s, load=%.1f). Immediate yield: sent REMOVE.",
                    node_id, cam_id, peer_fps_valid, peer.load_score,
                )

            # Safe duplicate reconciliation defense-in-depth:
            # If self and an alive peer both report the same active camera, check monotonic epoch / identity:
            # 1. Higher epoch wins. Lower epoch node yields/removes.
            # 2. Equal epoch / absent identity: static owner is deterministic tie-breaker.
            # 3. Duplicate observation must be stable across at least 2 consecutive heartbeats.
            # Skip if camera is in-flight across rescue/reclaim/migration/warmup.
            to_remove_self_reconcile: List[str] = []
            with self._self_lock:
                self_active = set(self._self_state.active_cameras)

            # Update duplicate observation trackers
            peer_active = set(peer.active_cameras)
            for cam_id in list(self_active & peer_active):
                key = (node_id, cam_id)
                self._duplicate_camera_seen[key] = self._duplicate_camera_seen.get(key, 0) + 1

                # Clean up if not active or not in-flight
                # Check in-flight and exclusion gates
                if (
                    cam_id in self._rescued_cameras
                    or cam_id in self._reclaim_in_progress
                    or cam_id in self._pending_acks
                    or cam_id in self._pending_winner
                    or cam_id in self._camera_added_at
                ):
                    continue

                if self._duplicate_camera_seen.get(key, 0) < 2:
                    continue

                # Monotonic epoch check
                self_epoch = self._camera_epochs.get(cam_id, 1)
                peer_epoch = peer.camera_epochs.get(cam_id, 1)

                should_self_yield = False
                if peer_epoch > self_epoch:
                    logger.info(
                        "[PeerOrch][Reconcile] Peer '%s' has higher epoch (%d > %d) for duplicate camera '%s'. Self yielding.",
                        node_id, peer_epoch, self_epoch, cam_id,
                    )
                    should_self_yield = True
                elif self_epoch > peer_epoch:
                    logger.info(
                        "[PeerOrch][Reconcile] Self has higher epoch (%d > %d) for duplicate camera '%s'. Peer '%s' expected to yield.",
                        self_epoch, peer_epoch, cam_id, node_id,
                    )
                    should_self_yield = False
                else:
                    # Equal epoch: use reported original owner vs current holder
                    # Check peer.camera_owners first, fall back to static mapping
                    peer_owner_claim = peer.camera_owners.get(cam_id)
                    peer_is_owner = (peer_owner_claim == node_id)

                    static_owned = self._get_owned_camera_ids()
                    self_is_owner = (cam_id in static_owned) or (self._rescued_cameras.get(cam_id) == self._node_id)
                    if peer_owner_claim is None:
                        peer_static = self._get_node_owned_cameras(node_id)
                        peer_is_owner = peer_static is not None and cam_id in peer_static

                    # Fail closed on ambiguity
                    if peer_is_owner and self_is_owner:
                        logger.warning(
                            "[PeerOrch][Reconcile] Ambiguous ownership for duplicate camera '%s': both '%s' and self configured/reporting as owner at epoch %d. Failing closed.",
                            cam_id, node_id, self_epoch,
                        )
                        continue
                    if not peer_is_owner and not self_is_owner:
                        logger.warning(
                            "[PeerOrch][Reconcile] Unresolved ownership for duplicate camera '%s' between '%s' and self at epoch %d. Failing closed.",
                            cam_id, node_id, self_epoch,
                        )
                        continue

                    if peer_is_owner and not self_is_owner:
                        should_self_yield = True

                if should_self_yield:
                    now_ts = time.time()
                    if now_ts - self._cam_cooldown.get(cam_id, 0.0) < 5.0:
                        continue
                    to_remove_self_reconcile.append(cam_id)
                    self._cam_cooldown[cam_id] = now_ts

            # Clean tracking for non-duplicate cameras on this peer
            for (p_node, p_cam) in list(self._duplicate_camera_seen.keys()):
                if p_node == node_id and p_cam not in (self_active & peer_active):
                    self._duplicate_camera_seen.pop((p_node, p_cam), None)

            # Issue REMOVE outside _lock
            for cam_id in to_remove_self_reconcile:
                remove_cmd = self._build_remove_cmd(cam_id, context="duplicate_reconciliation")
                if self._pubs.get("control") is not None:
                    self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
                logger.info(
                    "[PeerOrch][Reconcile] Duplicate camera '%s' active on both self and static owner '%s'. Non-owner self yielding: REMOVE sent.",
                    cam_id, node_id,
                )

            # Phase 6 / Reviewer Finding 2.4: compute migration blind-spot metric (Δτ)
            # Check if any camera migrated out to this peer is now reporting valid FPS on the peer.
            for cam_id, mig_ts in list(self._migration_complete_ts.items()):
                if self._migrated_out.get(cam_id) == node_id:
                    cam_fps = peer.fps_per_camera.get(cam_id, 0.0) if peer.fps_per_camera else 0.0
                    if isinstance(cam_fps, (int, float)) and cam_fps > 0.0:
                        blind_spot_ms = max(0.0, (time.time() - mig_ts) * 1000.0)
                        logger.info(
                            "[PeerOrch] Migration blind-spot resolved for '%s' on peer '%s': %.0fms",
                            cam_id, node_id, blind_spot_ms,
                        )
                        # ponytail: log resolved blind spot row to CSV
                        self._migration_log.log(
                            self._node_id, node_id, cam_id,
                            "blind_spot_resolved", peer.load_score, cam_fps,
                            0.0, "RESOLVED",
                            blind_spot_ms=blind_spot_ms,
                        )
                        self._migration_complete_ts.pop(cam_id, None)

    # ------------------------------------------------------------------
    # Overload classification helper (proactive-aware)
    # ------------------------------------------------------------------

    def _is_overloaded(
        self,
        load_score: float,
        risk_index: float,
        qos_state: Optional[str] = None,
    ) -> bool:
        """
        Determine if this node (or a peer) should be considered overloaded.

        When qos_state is supplied as a non-empty string matching the recognized
        workload-primary states ('healthy', 'moderate', 'degraded', 'overloaded', 'critical'):
        - 'healthy', 'moderate' -> False (even if load_score is 42..59)
        - 'degraded', 'overloaded', 'critical' -> True
        Proactive hard-fuse (risk_index >= hard_fuse) continues to override as a safety fuse.

        When qos_state is None/blank/unrecognized (legacy peers or disabled workload policy):
        Falls back to legacy reactive load_score >= overload_threshold (or proactive predictor).
        """
        proactive_cfg = self._cfg.get("proactive", {})
        hard_fuse = float(proactive_cfg.get("hard_fuse_threshold", 0.95))

        # Hard fuse — safety fuse against hardware saturation
        if not proactive_cfg.get("shadow_mode", False) and risk_index >= hard_fuse:
            return True

        norm_qos = str(qos_state).strip().lower() if qos_state is not None else ""
        if norm_qos in ("healthy", "moderate", "degraded", "overloaded", "critical"):
            return norm_qos in ("degraded", "overloaded", "critical")

        # Shadow mode: telemetry only — strictly passive/reactive decisions
        if proactive_cfg.get("shadow_mode", False):
            return load_score >= self._cfg.get("overload_threshold", 60.0)

        if proactive_cfg.get("enabled", False) and risk_index > 0.0:
            threshold = float(proactive_cfg.get("risk_threshold", 0.85))
            return risk_index >= threshold

        # Legacy path
        return load_score >= self._cfg.get("overload_threshold", 60.0)

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
                self._check_pending_migration_timeouts()
                self._check_self_overload()
            except Exception as exc:
                logger.error("[PeerOrch] Decision loop error: %s", exc)

    def _check_offline_peers(self) -> None:
        """
        Detect offline peers (heartbeat timeout).
        If peer has active cameras → trigger leaderless failover.

        Grace/convergence guard: a peer is only declared OFFLINE after
        ``heartbeat_timeout_s + failover_grace_s`` of silence.  The extra
        grace window prevents transient Zenoh / network blips from
        triggering an expensive leaderless failover for a peer that is
        still alive but briefly unreachable.  Default grace equals
        ``heartbeat_timeout_s`` so the total wait is 2× the heartbeat
        timeout — configurable via ``p2p.failover_grace_s`` in
        ``Edge/configs/edge_node.yml``.

        Failover convergence grace:
        When a failover has just been triggered (within ``failover_convergence_grace_s``,
        default 15s), suppress declaring other peers offline to prevent cascading
        mutual false-offline cascades during convergence. Detection resumes once
        the grace window expires.
        """
        now = time.time()
        timeout = float(self._cfg.get("heartbeat_timeout_s", 5.0))
        grace_s = float(self._cfg.get("failover_grace_s", timeout))
        offline_threshold = timeout + grace_s
        convergence_grace_s = float(self._cfg.get("failover_convergence_grace_s", 15.0))

        with self._self_lock:
            if self._self_state.last_seen == 0.0 or (now - self._self_state.last_seen > offline_threshold):
                logger.debug(
                    "[PeerOrch] Self heartbeat stale or missing (age=%.1fs > %.1fs). "
                    "Suppressing peer-offline detection.",
                    now - self._self_state.last_seen,
                    offline_threshold,
                )
                return

        with self._lock:
            # Per-dead-node / per-peer suppression:
            # Map of node_id -> timestamp of recent failover/offline event
            all_offline_events = {**self._failover_triggered, **self._peer_offline_at}
            to_check = list(self._peers.items())

        for node_id, peer in to_check:
            if node_id == self._node_id:
                continue
            silent_s = now - peer.last_seen
            # Harden peer offline handling: ONLY heartbeat silence (silent_s > offline_threshold)
            # can declare a peer offline.
            # If peer heartbeat is arriving (silent_s <= offline_threshold), even with
            # fps={}, 0 active cameras, or waiting/recovery state, the peer is alive
            # and MUST NOT be declared offline or trigger failover rescue.
            if silent_s > offline_threshold:
                with self._lock:
                    already_triggered = node_id in self._failover_triggered
                    last_event_time = all_offline_events.get(node_id, 0.0)
                # Per-dead-node suppression: if this specific node already triggered recently,
                # suppress re-declaring it offline during its own convergence window.
                # Other dead peers are NOT blocked.
                if not already_triggered and (now - last_event_time) < convergence_grace_s and last_event_time > 0.0:
                    continue

                with self._lock:
                    self._peer_offline_at[node_id] = now
                self._clear_offload_target(node_id)
                # Reclaim cameras migrated out to this dead peer locally
                self._reclaim_migrated_from_dead_peer(node_id)

                # Compute orphan cameras from authoritative static config first, then dynamic states
                candidate_orphans: List[str] = []
                seen_candidates = set()

                # 1. Authoritative static node-owned cameras
                static_owned = self._get_node_owned_cameras(node_id)
                if static_owned:
                    for c in sorted(static_owned):
                        if c and c not in seen_candidates:
                            candidate_orphans.append(c)
                            seen_candidates.add(c)

                # 2. active_cameras
                for c in peer.active_cameras:
                    if c and c not in seen_candidates:
                        candidate_orphans.append(c)
                        seen_candidates.add(c)
                # 3. camera_configs keys
                if isinstance(peer.camera_configs, dict):
                    for c in peer.camera_configs.keys():
                        if c and c not in seen_candidates:
                            candidate_orphans.append(c)
                            seen_candidates.add(c)
                # 4. _migrated_out entries whose holder is the dead peer
                with self._lock:
                    for c, holder in self._migrated_out.items():
                        if holder == node_id and c and c not in seen_candidates:
                            candidate_orphans.append(c)
                            seen_candidates.add(c)

                if candidate_orphans:
                    # Time-based re-arm: a failed attempt (e.g. RTSP sources
                    # unreachable while the dead host is down) must retry once
                    # sources come back, instead of latching until the peer's
                    # heartbeat revives.
                    retry_interval_s = float(self._cfg.get("failover_retry_interval_s", 30.0))
                    with self._lock:
                        last_attempt = self._failover_triggered.get(node_id, 0.0)
                        should_trigger = (now - last_attempt) >= retry_interval_s
                        if should_trigger:
                            self._failover_triggered[node_id] = now
                    if should_trigger:
                        orphans = list(candidate_orphans)
                        logger.critical(
                            "[PeerOrch] Peer '%s' OFFLINE (silent %.1fs ≥ %.1fs) with %d cameras! "
                            "Triggering failover...",
                            node_id, silent_s, offline_threshold, len(orphans),
                        )
                        self._notified_offline.discard(node_id)
                        self._executor.submit(self._leaderless_failover, node_id, orphans)
                else:
                    if node_id not in self._notified_offline:
                        logger.warning("[PeerOrch] Peer '%s' OFFLINE (no cameras).", node_id)
                        self._notified_offline.add(node_id)
            else:
                # Peer is alive — clear the notified/failover flags so we
                # react again if it goes offline a second time.
                self._notified_offline.discard(node_id)
                with self._lock:
                    self._failover_triggered.pop(node_id, None)
                    self._peer_offline_at.pop(node_id, None)

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
                start_ts = self._offload_started_at.pop(camera_id, 0.0)
                dur = f" (duration: {time.time() - start_ts:.1f}s)" if start_ts > 0 else ""
                logger.info("[PeerOrch] L%d offload ENDED for '%s': peer_offline%s", old_level, camera_id, dur)
                logger.warning(
                    "[PeerOrch] Offload target '%s' offline — clearing L%d for '%s'",
                    node_id, old_level, camera_id,
                )

    def _reclaim_migrated_from_dead_peer(self, dead_node_id: str) -> None:
        """Reclaim cameras owned by this node that were migrated out to a peer that just died."""
        with self._lock:
            migrated_to_dead = [
                cam_id for cam_id, holder in self._migrated_out.items()
                if holder == dead_node_id
            ]

        if not migrated_to_dead:
            return

        now = time.time()
        for camera_id in migrated_to_dead:
            with self._lock:
                if camera_id in self._reclaim_in_progress:
                    continue
                retry_at = self._reclaim_retry_at.get(camera_id, 0.0)
                if now < retry_at:
                    continue

            # Check if camera is already running locally
            with self._self_lock:
                if camera_id in self._self_state.active_cameras:
                    with self._lock:
                        self._migrated_out.pop(camera_id, None)
                        self._reclaim_in_progress.discard(camera_id)
                        self._reclaim_retry_at.pop(camera_id, None)
                    continue

            cam_config = self._get_camera_config(camera_id)
            if cam_config is None:
                logger.warning(
                    "[PeerOrch] Dead-peer reclaim: cannot get config for '%s', skipping",
                    camera_id,
                )
                continue

            now_ts = time.time()
            with self._lock:
                cur_epoch = self._camera_epochs.get(camera_id, 1) + 1
                self._camera_epochs[camera_id] = cur_epoch
                mig_id = f"mig_{camera_id}_{int(now_ts * 1000)}"
                self._camera_migration_ids[camera_id] = mig_id
                self._pending_migration_ids[camera_id] = mig_id
                self._pending_epochs[camera_id] = cur_epoch

            add_cmd = {
                **cam_config,
                "cmd": "ADD",
                "epoch": cur_epoch,
                "migration_id": mig_id,
            }
            event = threading.Event()
            with self._lock:
                # Register before publishing ADD: the receiver can ACK
                # immediately, before the waiter thread gets scheduled.
                self._pending_acks[camera_id] = event
            if self._pubs.get("control") is not None:
                self._pubs["control"].put(msgpack.packb(add_cmd, use_bin_type=True))
            self._camera_added_at[camera_id] = now
            self._camera_first_valid_fps_at.pop(camera_id, None)
            with self._lock:
                self._reclaim_in_progress.add(camera_id)

            logger.info(
                "[PeerOrch] Dead-peer reclaim: ADD '%s' back to self (holder '%s' dead), waiting for ack...",
                camera_id, dead_node_id,
            )
            self._reclaim_completed_at[camera_id] = now
            with self._lock:
                self._reclaim_retry_count[camera_id] = 0
            self._transition_settle_until = now + self._cfg.get("transition_settle_s", 5.0)

            self._executor.submit(self._wait_and_remove_reclaim, camera_id, dead_node_id)

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
        with self._lock:
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

        # ponytail: no rescue-hold expiry DROP. Rescued cameras stay on the
        # rescuer until the original owner revives and resumes them (normal
        # return path below). The old force-REMOVE left the camera owned by
        # nobody cluster-wide when the owner stayed dead.

        # Aggregate owned-camera guard: zero locally-owned active cameras
        # means the node cannot satisfy the L1 ownership invariant for
        # migration decisions (see _pick_camera_to_offload).  Returning
        # any rescued camera would not change owned_active (rescued cameras
        # are foreign) but it would also fail to fix the degradation, so
        # we hold all rescues here until at least one owned camera resumes.
        # This is the hard invariant requested in the spec.
        # ponytail: rate-limit the diagnostic — this branch fires every 1s
        # tick while the node is in the degraded state; one log per
        # BLOCKED_LOG_COOLDOWN (15 s) is enough to alert without spam.
        if not owned_active_snapshot:
            with self._lock:
                has_rescues = bool(self._rescued_cameras)
            if has_rescues and self._maybe_log_block("rebalance_no_owned", now):
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
                if self._maybe_log_block(f"rebalance_owned_{camera_id}", now):
                    logger.warning(
                        "[PeerOrch][Rebalance] Skipping return of '%s' to '%s': "
                        "camera is locally-owned; this rebalance path only "
                        "handles foreign rescued cameras.",
                        camera_id, original_owner,
                    )
                continue
            # Return rescued camera only if original owner is online AND ready:
            # fresh owner heartbeat, owner not waiting/recovery, owner not overloaded,
            # and static ownership matches original owner. REMOVE locally without requiring
            # owner active_cameras.
            peer = peers_snapshot.get(original_owner)
            if peer is None:
                # Owner absent from routing table.
                continue
            if now - peer.last_seen > timeout:
                # Owner heartbeat older than configured timeout — stale.
                continue
            peer_in_waiting = is_waiting_state(peer.fps_per_camera, peer.active_cameras, peer.status)
            peer_overloaded = self._is_overloaded(
                peer.load_score,
                peer.risk_index,
                peer.qos_state,
            )
            if peer_in_waiting or peer_overloaded:
                continue
            # Accept return only if qos_state is healthy/moderate when supplied; legacy blank remains compatible.
            if peer.qos_state is not None and str(peer.qos_state).strip().lower() not in ("healthy", "moderate", ""):
                continue
            # Validate static ownership of the original owner
            owner_static_cameras = self._get_node_owned_cameras(original_owner)
            if owner_static_cameras is not None and camera_id not in owner_static_cameras:
                if self._maybe_log_block(f"rebalance_not_owned_{camera_id}", now):
                    logger.warning(
                        "[PeerOrch][Rebalance] Skipping return of '%s' to '%s': camera is not statically owned by peer.",
                        camera_id, original_owner,
                    )
                continue
            # Owner is alive, not waiting/recovering, not overloaded, and statically owns the camera! Yield.
            remaining_after.discard(camera_id)
            to_return.append(camera_id)

        for camera_id in to_return:
            with self._lock:
                original_owner = self._rescued_cameras.pop(camera_id, None)
                self._rescued_at.pop(camera_id, None)
            if original_owner is None:
                continue
            remove_cmd = self._build_remove_cmd(camera_id, context="rebalance_return")
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
          - This node stays below the capacity ceiling after taking the camera back
            (active_cameras + 1 < effective_capacity).
          - The camera is still being held by the peer it was migrated to
            (confirmed via peer heartbeat).
          - Cooldown has expired since the migration.

        Reclaim is done one camera at a time to avoid oscillation.
        """
        if not self._migrated_out:
            return

        cfg = self._cfg
        now = time.time()

        reclaim_threshold = cfg.get("overload_threshold", 60.0) - cfg.get("reclaim_margin", 15.0)
        reclaim_stable_s  = cfg.get("reclaim_stable_s", 30.0)
        cooldown_s        = cfg.get("cooldown_s", 45.0)
        heartbeat_timeout = cfg.get("heartbeat_timeout_s", 6.0)

        with self._self_lock:
            if self._self_state.last_seen == 0.0 or (now - self._self_state.last_seen > heartbeat_timeout):
                if self._maybe_log_block("stale_self_reclaim", now):
                    logger.warning(
                        "[PeerOrch] Self heartbeat stale or missing (age=%.1fs > %.1fs). "
                        "Skipping reclaim check.",
                        now - self._self_state.last_seen, heartbeat_timeout,
                    )
                return
            load         = self._self_state.load_score
            overload_since = self._self_state.overload_since
            self_active_count = len(self._self_state.active_cameras)
            self_max_streams = self._self_state.max_streams

        # Capacity guard: block reclaim when adding a camera would reach/exceed local stream capacity
        cm = self._camera_manager
        local_max = cm.get_max_streams() if cm is not None else self_max_streams
        configured_max = int(cfg.get("eps_streams_max", local_max))
        effective_capacity = min(local_max, configured_max)
        if self_active_count + 1 >= effective_capacity:
            logger.debug(
                "[PeerOrch] Reclaim blocked by local capacity: active=%d + 1 >= capacity=%d",
                self_active_count, effective_capacity,
            )
            return

        # Only reclaim if load has been stable and low
        if load >= reclaim_threshold:
            self._reclaim_eligible_since = None
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
            # Check in-progress and retry timing
            with self._lock:
                if camera_id in self._reclaim_in_progress:
                    continue
                retry_at = self._reclaim_retry_at.get(camera_id, 0.0)
            if now < retry_at:
                continue

            # Check cooldown
            last_mig = self._cam_cooldown.get(camera_id, 0.0)
            if now - last_mig < cooldown_s:
                continue

            # 1. Check if holder is alive and still reports the camera
            with self._lock:
                holder_peer = self._peers.get(holder_node)
            holder_alive = holder_peer is not None and (now - holder_peer.last_seen <= heartbeat_timeout)

            if holder_alive and holder_peer is not None and camera_id in holder_peer.active_cameras:
                # Still running fine on holder; check if load/cooldown allows normal reclaim
                pass
            elif holder_alive and holder_peer is not None and camera_id not in holder_peer.active_cameras:
                # Holder dropped the camera while still alive!
                # If camera is already active on self, reclaim has succeeded; clear state safely.
                with self._self_lock:
                    is_active_local = camera_id in self._self_state.active_cameras
                if is_active_local:
                    logger.info(
                        "[PeerOrch][Reclaim] Camera '%s' is already active locally and holder '%s' no longer has it. Clearing reclaim mapping.",
                        camera_id, holder_node,
                    )
                    with self._lock:
                        self._migrated_out.pop(camera_id, None)
                        self._reclaim_in_progress.discard(camera_id)
                        self._reclaim_retry_at.pop(camera_id, None)
                        self._reclaim_retry_count.pop(camera_id, None)
                        self._reclaim_attempts.pop(camera_id, None)
                        self._reclaim_pending_remove.pop(camera_id, None)
                    continue

                # Inspect latest fresh peer heartbeats for another alive peer reporting camera_id
                other_holder = None
                with self._lock:
                    for pid, p in self._peers.items():
                        if pid != holder_node and (now - p.last_seen <= heartbeat_timeout) and (camera_id in p.active_cameras):
                            other_holder = pid
                            break
                    if other_holder is not None:
                        # Found another alive peer reporting this camera; update mapping atomically & skip local ADD
                        self._migrated_out[camera_id] = other_holder
                        self._reclaim_in_progress.discard(camera_id)
                        self._reclaim_retry_at.pop(camera_id, None)
                        self._reclaim_retry_count.pop(camera_id, None)
                        self._reclaim_attempts.pop(camera_id, None)
                        self._reclaim_pending_remove.pop(camera_id, None)

                if other_holder is not None:
                    logger.info(
                        "[PeerOrch][Reclaim] Camera '%s' found active on alive peer '%s' (reassigned from '%s'). Skipping local ADD.",
                        camera_id, other_holder, holder_node,
                    )
                    continue

                logger.warning(
                    "[PeerOrch][Reclaim] Camera '%s' missing from active_cameras of alive holder '%s'! Initiating recovery...",
                    camera_id, holder_node,
                )
            else:
                # Holder offline or unknown — let dead-peer recovery / offline check handle it, but do not drop tracking
                continue

            # Increment _reclaim_attempts[camera_id]; never permanently abandon/pop _migrated_out
            attempts = self._reclaim_attempts.get(camera_id, 0) + 1
            self._reclaim_attempts[camera_id] = attempts
            # Apply backoff cooldown on repeat attempts without dropping camera tracking
            if attempts > 5:
                backoff_s = min(30.0, 5.0 * (attempts - 5))
                self._reclaim_retry_at[camera_id] = now + backoff_s
                logger.warning(
                    "[PeerOrch][Reclaim] Camera '%s' reclaim attempt %d > 5; backing off for %.1fs without dropping tracking.",
                    camera_id, attempts, backoff_s,
                )
                self._reclaim_in_progress.discard(camera_id)
                continue

            # Make-before-Break: send ADD to self FIRST (do not REMOVE holder until stream PLAYING).
            # _wait_and_remove_reclaim() will send REMOVE to holder only after local ADD is confirmed.

            # Send ADD to self (reclaim)
            cam_config = self._get_camera_config(camera_id)
            if cam_config is None:
                logger.warning("[PeerOrch] Reclaim: cannot get config for '%s', skipping", camera_id)
                continue

            # Make-before-Break: Step 1 — ADD to self first, wait for stream PLAYING ack
            # Step 2 — Only then REMOVE from holder
            now_ts = time.time()
            with self._lock:
                cur_epoch = self._camera_epochs.get(camera_id, 1) + 1
                self._camera_epochs[camera_id] = cur_epoch
                mig_id = f"mig_{camera_id}_{int(now_ts * 1000)}"
                self._camera_migration_ids[camera_id] = mig_id
                self._pending_migration_ids[camera_id] = mig_id
                self._pending_epochs[camera_id] = cur_epoch

            add_cmd = {
                **cam_config,
                "cmd": "ADD",
                "epoch": cur_epoch,
                "migration_id": mig_id,
            }
            event = threading.Event()
            with self._lock:
                # Register before publishing ADD: the receiver can ACK
                # immediately, before the waiter thread gets scheduled.
                self._pending_acks[camera_id] = event
                self._reclaim_in_progress.add(camera_id)
            if self._pubs.get("control") is not None:
                self._pubs["control"].put(msgpack.packb(add_cmd, use_bin_type=True))
            # ponytail: record the ADD so the per-camera warmup gate in
            # _pick_camera_to_offload suppresses offload actions on this
            # camera until its FPS has been valid for camera_warmup_s seconds.
            self._camera_added_at[camera_id] = now
            # Clear any stale first-valid-fps snapshot so the warmup restarts
            # from zero for this new ADD event.
            self._camera_first_valid_fps_at.pop(camera_id, None)

            logger.info(
                "[PeerOrch] Reclaim: load=%.1f < threshold=%.1f (risk=%.2f, active=%d) — "
                "ADD '%s' back to self (held by '%s'), waiting for ack...",
                load, reclaim_threshold, self._self_state.risk_index,
                len(self._self_state.active_cameras), camera_id, holder_node,
            )

            # Record reclaim start: this camera is ineligible for offload until
            # its FPS stabilises after returning home.
            self._reclaim_completed_at[camera_id] = now
            with self._lock:
                self._reclaim_retry_count[camera_id] = 0
                # ponytail: do NOT reset _reclaim_attempts here — it accumulates across
                # all retry cycles and is the give-up gate; resetting it here caused
                # infinite reclaim loops (always appeared as attempt 1).
                self._reclaim_pending_remove.pop(camera_id, None)

            # Suppress overload decisions for the settle window once reclaim
            # ADD is initiated: incoming stream warm-up FPS can look like a
            # fresh overload and re-escalate the node moments after reclaim.
            self._transition_settle_until = now + cfg.get("transition_settle_s", 5.0)

            # Spin up a thread that waits for the local ADD ack then removes holder
            self._executor.submit(self._wait_and_remove_reclaim, camera_id, holder_node)

            self._cam_cooldown[camera_id] = now
            # Reset eligible timer to avoid immediately reclaiming next camera
            self._reclaim_eligible_since = now

            self._migration_log.log(
                holder_node, self._node_id, camera_id,
                "reclaim", load, None,
                0.0, "RECLAIM_INITIATED",
            )
            # Reclaim one at a time
            return

    def _check_pending_migration_timeouts(self) -> None:
        """
        Periodically garbage-collect stale pending migration state that timed out
        without an ACK or where a background thread might have exited unexpectedly.
        Cleans up _pending_winner, _pending_started_at, _pending_acks, and inflight reservations.
        """
        now = time.time()
        timeout_s = float(self._cfg.get("migration_timeout_s", 15.0))
        # Garbage collect any pending migration running longer than 2x timeout or min 30s
        stale_threshold = max(30.0, timeout_s * 2.0)
        stale_cleanups = []
        with self._lock:
            for cam_id, started_at in list(self._pending_started_at.items()):
                if (now - started_at) >= stale_threshold:
                    winner_id = self._pending_winner.pop(cam_id, None)
                    self._pending_started_at.pop(cam_id, None)
                    self._pending_acks.pop(cam_id, None)
                    if winner_id:
                        stale_cleanups.append((cam_id, winner_id))

        for cam_id, winner_id in stale_cleanups:
            with self._lock:
                self._peer_inflight[winner_id] = max(
                    0, self._peer_inflight.get(winner_id, 0) - 1
                )
            logger.warning(
                "[PeerOrch] Cleaned up stale pending migration for camera '%s' (winner '%s') after timeout.",
                cam_id, winner_id,
            )

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
                last_seen=self._self_state.last_seen,
                status=self._self_state.status,
            )
            self_last_seen = self._self_state.last_seen

        # Stale self-heartbeat guard: if self state has never been received or
        # is stale (last_seen > offline_threshold), skip overload checks.
        # Use same threshold as _check_offline_peers to avoid inconsistent suppression.
        timeout = self._cfg.get("heartbeat_timeout_s", 5.0)
        grace_s = self._cfg.get("failover_grace_s", timeout)
        offline_threshold = timeout + grace_s
        if self_last_seen == 0.0 or (now - self_last_seen > offline_threshold):
            if self._maybe_log_block("stale_self_heartbeat", now):
                logger.warning(
                    "[PeerOrch] Self heartbeat stale or missing (age=%.1fs > %.1fs). "
                    "Skipping overload check.",
                    now - self_last_seen, timeout,
                )
            return

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
        thr3          = cfg.get("offload_level3_threshold", 60.0)
        thr2          = cfg.get("offload_level2_threshold", 67.0)
        thr1          = cfg.get("offload_level1_threshold", 80.0)
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
        hard_fuse = float(cfg.get("proactive", {}).get("hard_fuse_threshold", 0.95))
        fuse_active = state.risk_index >= hard_fuse

        if fuse_active or (load >= thr1 and global_offload >= 1):
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
                    start_ts = self._offload_started_at.pop(cam_id, 0.0)
                    dur = f" (duration: {now - start_ts:.1f}s)" if start_ts > 0 else ""
                    self.set_offload_level(cam_id, 0)
                    logger.info("[PeerOrch] L%d offload ENDED for '%s': load_normalized%s", cl, cam_id, dur)
            return

        raw_intended_level = intended_level

        # Escalation ladder ladder-step clamp:
        # If hardware fuse is not active and global_offload supports higher offload levels,
        # do not jump directly to L1 or L2 from level 0 (or L1 from L3).
        # Normal escalation ladder must step through available intermediate levels:
        #   0 -> 3 (if global_offload >= 3)
        #   0 -> 2 (if global_offload == 2)
        #   3 -> 2 (after l3_dwell)
        #   2 -> 1 (after l2_dwell)

        # Select candidate camera. For initial candidate selection when climbing the ladder,
        # use intended_level (or clamped level if stepping from 0).
        cam_to_offload = self._pick_camera_to_offload(state, level=intended_level)
        if not cam_to_offload:
            if self._maybe_log_block("no_cam", now):
                logger.warning(
                    "[PeerOrch] Overloaded (load=%.1f, risk=%.2f, active=%d, fps_valid=%s) but no eligible camera to "
                    "offload (fps_data=%d) — cannot escalate",
                    load, state.risk_index, len(state.active_cameras),
                    _has_valid_positive_fps(state.fps_per_camera),
                    len(state.fps_per_camera) if state.fps_per_camera else 0,
                )
            return

        # Transition guard: this camera was recently reclaimed and its FPS is
        # still stabilising.  Re-escalating it would immediately undo reclaim.
        reclaim_age = now - self._reclaim_completed_at.get(cam_to_offload, 0.0)
        reclaim_stable_s = cfg.get("reclaim_stable_s", 30.0)
        if reclaim_age < reclaim_stable_s:
            logger.info(
                "[PeerOrch] Transition guard: '%s' was reclaimed %.0fs ago "
                "(need %.0fs) — skipping offload to avoid re-escalation.",
                cam_to_offload, reclaim_age, reclaim_stable_s,
            )
            return

        current_level = self.get_offload_level(cam_to_offload)

        # In normal mode (no fuse), clamp escalation steps per camera
        if not fuse_active:
            def _clamp_intended_level(cur_lvl: int, raw_lvl: int, g_offload: int) -> int:
                if cur_lvl == 0:
                    if g_offload >= 3 and raw_lvl in (1, 2, 3):
                        return 3
                    elif g_offload == 2 and raw_lvl in (1, 2):
                        return 2
                    elif g_offload == 1 and raw_lvl == 1:
                        return 1
                    return 0
                elif cur_lvl == 3:
                    if raw_lvl in (1, 2) and g_offload >= 2:
                        return 2
                    return 3
                elif cur_lvl == 2:
                    if raw_lvl == 1 and g_offload >= 1:
                        return 1
                    return 2
                elif cur_lvl == 1:
                    return 1
                return raw_lvl

            clamped = _clamp_intended_level(current_level, raw_intended_level, global_offload)
            if clamped != intended_level:
                intended_level = clamped
                # Re-pick camera if clamped level differs in selection polarity (L1 lightest vs L2/L3 heaviest)
                # Level 2 & 3 both pick heaviest, Level 1 picks lightest
                new_cam = self._pick_camera_to_offload(state, level=intended_level)
                if new_cam:
                    cam_to_offload = new_cam
                    current_level = self.get_offload_level(cam_to_offload)
                    intended_level = _clamp_intended_level(current_level, raw_intended_level, global_offload)

        # Runtime diagnostic log immediately before overload action
        logger.debug(
            "[PeerOrch] Overload decision check: load=%.1f (thr1=%.1f, thr2=%.1f, thr3=%.1f), "
            "risk_index=%.2f, fps_valid=%s, active_cameras=%d, camera='%s', current_level=%d, target_level=%d, fuse=%s",
            load, thr1, thr2, thr3, state.risk_index, _has_valid_positive_fps(state.fps_per_camera),
            len(state.active_cameras), cam_to_offload, current_level, intended_level, fuse_active,
        )

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

        if intended_level == 1:
            # Ownership guard: only cameras this node owns (cameras.yml) may be
            # migrated away. Escalation ladders lock onto the L2/L3-picked camera,
            # which can be foreign — swap to an owned camera for the actual migration.
            owned_cam_ids = self._get_owned_camera_ids()
            if cam_to_offload not in owned_cam_ids:
                new_cam = self._pick_camera_to_offload(state, level=1)
                if not new_cam:
                    logger.info(
                        "[PeerOrch] L1 escalation blocked: '%s' is foreign and no owned camera is eligible — retaining Level %d offload",
                        cam_to_offload, current_level,
                    )
                    return
                logger.info(
                    "[PeerOrch] L1 target swapped: foreign '%s' retained (crop offload continues); migrating owned '%s' instead",
                    cam_to_offload, new_cam,
                )
                cam_to_offload = new_cam
                current_level = self.get_offload_level(cam_to_offload)

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
                cl = self.get_offload_level(cam_to_offload)
                start_ts = self._offload_started_at.pop(cam_to_offload, 0.0)
                dur = f" (duration: {now - start_ts:.1f}s)" if start_ts > 0 else ""
                self.set_offload_level(cam_to_offload, 0)
                logger.info("[PeerOrch] L%d offload ENDED for '%s': escalated_to_l1%s", cl, cam_to_offload, dur)
            logger.info(
                "[PeerOrch] Load=%.1f ≥ L1 threshold=%.1f. Escalating to "
                "Level 1 stream migration for '%s'.",
                load, thr1, cam_to_offload,
            )
            trigger = "fps_drop" if (state.avg_fps and
                                     state.avg_fps < cfg.get("eps_fps_strict", 18.0)
                                     ) else "load_score"
            logger.info("[PeerOrch] RFO trigger: %s reason=%s", cam_to_offload, trigger)
            self._trigger_rfo(cam_to_offload, relaxation_tier=0)

        elif intended_level == 2:
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
                logger.info(
                    "[PeerOrch] Load=%.1f ≥ L2 threshold=%.1f. "
                    "Level 2 vehicle-crop offload for '%s' → '%s'.",
                    load, thr2, cam_to_offload, best_peer,
                )
                self.set_offload_level(cam_to_offload, 2, target_node=best_peer)

        elif intended_level == 3:
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
                logger.info(
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
        logger.info(
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
        # Allow policy override for baseline comparison experiments (Finding 2.5)
        policy_name = self._cfg.get("policy", "p2p_pareto")
        if policy_name == "no_offload":
            return None

        therm_cfg = self._cfg.get("thermal")
        best_id : Optional[str] = None
        best_load = float("inf")
        best_workload_ema = float("inf")

        with self._lock:
            # Baseline: Round-Robin offload policy
            if policy_name == "round_robin":
                eligible = [
                    nid for nid, peer in sorted(self._peers.items())
                    if nid != self._node_id and (now - peer.last_seen <= timeout)
                ]
                if not eligible:
                    return None
                rr_counter = self._rr_counter
                selected = eligible[rr_counter % len(eligible)]
                self._rr_counter = rr_counter + 1
                return selected

            zombie_timeout_count = int(self._cfg.get("zombie_timeout_count", 3))

            for nid, peer in self._peers.items():
                if nid == self._node_id:
                    continue
                if now - peer.last_seen > timeout:
                    continue
                if peer.load_score >= 60.0:
                    continue
                if self._peer_consecutive_timeouts.get(nid, 0) >= zombie_timeout_count:
                    continue
                # Skip peers in startup/recovery or without valid positive FPS (e.g. load_score=0 placeholder)
                if is_waiting_state(peer.fps_per_camera, peer.active_cameras, getattr(peer, "status", None)):
                    continue
                # For pure least-load greedy baseline, skip stream capacity and thermal admission gates
                if policy_name not in ("least_load_greedy", "centralized_greedy"):
                    if for_offload_level <= 1 and (
                        len(peer.active_cameras) + self._peer_inflight.get(nid, 0)
                        >= peer.max_streams
                    ):
                        continue
                    if not _thermal_admission_ok(peer.gpu_temp_c, therm_cfg):
                        continue
                    if now < peer.penalty_until:
                        continue

                # For Level 2/3 (crop offload), peer selection scores inference/recv capacity
                # instead of pipeline stream load_score, since crop offload does not decode.
                # Normalized composite: 50% load pressure + 50% crop saturation (cap=10.0 crops/s)
                if for_offload_level >= 2 and policy_name not in ("least_load_greedy", "centralized_greedy"):
                    recv_rate = peer.offload_crops_received_per_s
                    recv_cap = float(self._cfg.get("crop_recv_capacity", 10.0))
                    recv_ratio = max(0.0, min(1.0, recv_rate / max(1.0, recv_cap)))
                    candidate_score = (peer.load_score * 0.5) + (recv_ratio * 50.0)
                else:
                    candidate_score = peer.load_score

                peer_wl = peer.workload_ema if (peer.workload_ema is not None and math.isfinite(peer.workload_ema)) else float("inf")
                if candidate_score < best_load:
                    best_load = candidate_score
                    best_workload_ema = peer_wl
                    best_id   = nid
                elif abs(candidate_score - best_load) < 1e-6:
                    # Tiebreaker: workload_ema only
                    if peer_wl < best_workload_ema:
                        best_workload_ema = peer_wl
                        best_id = nid

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

        with self._lock:
            if camera_id not in self._rfo_snapshots:
                self._rfo_snapshots[camera_id] = (_rfo_load, _rfo_fps)

        now_ts = time.time()
        payload = {
            "requester":      self._node_id,
            "camera_id":      camera_id,
            "cam_uri":        cam_uri,
            "load_score":     _rfo_load,
            "avg_fps":        _rfo_fps,
            "eps_fps":        eps_fps,
            "eps_network_ms": eps_net,
            "tier":           relaxation_tier,
            "timestamp":      now_ts,
            "ts":             now_ts,
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
                logger.info(
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
                    self._rfo_snapshots.pop(camera_id, None)
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

        now_ts = time.time()
        with self._lock:
            cur_epoch = self._camera_epochs.get(camera_id, 1) + 1
            self._camera_epochs[camera_id] = cur_epoch
            mig_id = f"mig_{camera_id}_{int(now_ts * 1000)}"
            self._camera_migration_ids[camera_id] = mig_id
            self._pending_migration_ids[camera_id] = mig_id
            self._pending_epochs[camera_id] = cur_epoch

        decision = {
            "winner":       winner["bidder"],
            "camera_id":    camera_id,
            "from_node":    self._node_id,
            "cam_config":   cam_config,
            "epoch":        cur_epoch,
            "migration_id": mig_id,
            "timestamp":    now_ts,
            "ts":           now_ts,
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
            self._pending_started_at[camera_id] = time.time()
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
        self._safe_submit(self._evaluate_and_bid, payload)

    def _safe_submit(self, fn, *args, **kwargs):
        """Submit to executor safely; silently ignores if executor is shut down."""
        try:
            return self._executor.submit(fn, *args, **kwargs)
        except RuntimeError:
            logger.debug("[PeerOrch] Executor already shut down; dropped async task.")
            return None

    def _evaluate_and_bid(self, payload: dict) -> None:
        """
        Run ε-constraint checks and publish proposal if eligible.
        Runs in ThreadPoolExecutor — safe to block for RTT measurement.
        """
        requester     = payload.get("requester", "")
        camera_id     = payload.get("camera_id", "")
        eps_fps       = payload.get("eps_fps", 18.0)
        eps_net_ms    = payload.get("eps_network_ms", 50.0)

        # In-flight guard: do not accept/bid on an RFO if camera is already
        # undergoing uncommitted migration, rescue, or already active/held.
        now = time.time()
        heartbeat_timeout = float(self._cfg.get("heartbeat_timeout_s", 5.0))
        with self._lock:
            if (camera_id in self._pending_acks
                    or camera_id in self._pending_winner
                    or camera_id in self._rescued_cameras
                    or camera_id in self._migrated_out
                    or camera_id in self._reclaim_in_progress):
                logger.info("[PeerOrch] RFO rejected for '%s': uncommitted state/already handled", camera_id)
                return

            # Reject if an alive peer already reports this camera active
            for pid, p in self._peers.items():
                if pid != requester and (now - p.last_seen <= heartbeat_timeout) and (camera_id in p.active_cameras):
                    logger.info("[PeerOrch] RFO rejected for '%s': already active on alive peer '%s'", camera_id, pid)
                    return
        with self._self_lock:
            if camera_id in self._self_state.active_cameras:
                logger.info("[PeerOrch] RFO rejected for '%s': already active locally", camera_id)
                return

        # BUG-1 fix: read _self_state under its own lock
        with self._self_lock:
            if not _has_valid_positive_fps(self._self_state.fps_per_camera):
                logger.info(
                    "[PeerOrch] RFO rejected for '%s': no valid positive FPS locally (fps_per_camera=%s)",
                    camera_id, self._self_state.fps_per_camera,
                )
                return
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
        cm = self._camera_manager
        default_max = cm.get_max_streams() if cm is not None else 4
        eps_streams_max = int(self._cfg.get("eps_streams_max", default_max))
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
            # ε2 — FPS prediction (demoted from hard reject gate to soft bid scoring)
            # YAML parses bare integer keys as int; look up both int and str forms
            fps_model = self._cfg.get("fps_model", {})
            streams_after = current_streams + 1
            predicted_fps = fps_model.get(streams_after,
                            fps_model.get(str(streams_after), None))
            # ponytail: static fps_model is used for soft bid scoring rather than hard rejection
            logger.debug(
                "[PeerOrch] FPS model evaluation for '%s': streams_after=%d, predicted_fps=%s, eps_fps=%.1f (soft bid scoring only)",
                camera_id, streams_after, predicted_fps, eps_fps,
            )

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

            # All constraints pass — compute multi-objective cost F(x)
            # Incorporates load pressure, predicted FPS degradation, network RTT, and thermal headroom.
            bid_weights = self._cfg.get("p2p", {}).get("bid_weights", {})
            w_load = float(bid_weights.get("w_load", 0.50))
            w_fps = float(bid_weights.get("w_fps", 0.25))
            w_rtt = float(bid_weights.get("w_rtt", 0.15))
            w_therm = float(bid_weights.get("w_therm", 0.10))

            target_fps = float(self._cfg.get("target_fps", 25.0))
            fps_degrade_ratio = max(0.0, min(1.0, (target_fps - (predicted_fps or target_fps)) / max(1.0, target_fps)))
            rtt_ratio = max(0.0, min(1.0, (rtt_ms or 0.0) / max(1.0, eps_net_ms)))
            
            # Thermal headroom cost above onset (70C -> 85C)
            onset_c = float(self._cfg.get("thermal", {}).get("onset_gpu_temp_c", 70.0))
            crit_c = float(self._cfg.get("thermal", {}).get("max_gpu_temp_c", 85.0))
            if self_temp is not None:
                therm_ratio = max(0.0, min(1.0, (self_temp - onset_c) / max(1.0, (crit_c - onset_c))))
            else:
                therm_ratio = 0.0
                logger.debug("[PeerOrch] Bid F(x): gpu_temp_c is None, w_therm contribution is 0.0")

            # ponytail: multi-objective incremental cost; lowest cost wins vote
            f_x = (
                w_load * (self_load / 100.0)
                + w_fps * fps_degrade_ratio
                + w_rtt * rtt_ratio
                + w_therm * therm_ratio
            ) * 100.0

            now_ts = time.time()
            proposal = {
                "bidder":        self._node_id,
                "camera_id":     camera_id,
                "score":         round(f_x, 2),
                "fps_predicted": predicted_fps,
                "rtt_ms":        round(rtt_ms, 1),
                "timestamp":     now_ts,
                "ts":            now_ts,
            }

            self._pubs["vote_proposal"].put(msgpack.packb(proposal, use_bin_type=True))
            logger.info(
                "[PeerOrch] RFO accepted for '%s' (ALL ε-constraints pass) — "
                "Bid: score=%.1f, fps_pred=%s, rtt=%.0fms",
                camera_id, f_x, f"{predicted_fps:.1f}" if predicted_fps is not None else "None", rtt_ms,
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
            # Duplicate / uncommitted guard on winner side
            now = time.time()
            heartbeat_timeout = float(self._cfg.get("heartbeat_timeout_s", 5.0))
            with self._lock:
                if (camera_id in self._pending_acks
                        or camera_id in self._pending_winner
                        or camera_id in self._rescued_cameras
                        or camera_id in self._migrated_out
                        or camera_id in self._reclaim_in_progress):
                    logger.info("[PeerOrch] Decision ignored: camera '%s' is in-flight/uncommitted or held.", camera_id)
                    return

                # Reject/abort if an alive peer already reports this camera active
                for pid, p in self._peers.items():
                    if pid != from_node and (now - p.last_seen <= heartbeat_timeout) and (camera_id in p.active_cameras):
                        logger.info("[PeerOrch] Decision ignored: camera '%s' is already active on alive peer '%s'.", camera_id, pid)
                        return
            with self._self_lock:
                if camera_id in self._self_state.active_cameras:
                    logger.info("[PeerOrch] Decision ignored: camera '%s' is already active locally.", camera_id)
                    return

            # --- I WON: ADD camera to pipeline ---
            cam_config = payload.get("cam_config", {})
            if not cam_config:
                logger.error("[PeerOrch] Decision missing cam_config for '%s'", camera_id)
                return

            epoch = payload.get("epoch")
            migration_id = payload.get("migration_id")
            if epoch is not None or migration_id is not None:
                with self._lock:
                    if epoch is not None:
                        self._camera_epochs[camera_id] = int(epoch)
                    if migration_id is not None:
                        self._camera_migration_ids[camera_id] = str(migration_id)

            add_cmd = {**cam_config, "cmd": "ADD"}
            if epoch is not None:
                add_cmd["epoch"] = epoch
            if migration_id is not None:
                add_cmd["migration_id"] = migration_id
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
            self._transition_settle_until = max(
                self._transition_settle_until,
                time.time() + self._cfg.get("transition_settle_s", 5.0),
            )

        elif from_node == self._node_id:
            # --- I AM REQUESTER: wait for ack then REMOVE ---
            self._executor.submit(self._wait_and_remove, camera_id, winner)

    def _l1_remove_ownership_guard(self, camera_id: str) -> bool:
        """
        Final atomic ownership guard, called immediately before an L1 REMOVE
        that would remove ``camera_id`` from this node's pipeline.

        The decision-time guard in ``_pick_camera_to_offload`` ran against a
        snapshot that is now stale: while we waited for the winner's ACK, a
        concurrent migration / reclaim / rebalance may have removed every
        other locally-owned camera, leaving this one as the last owned stream.
        Re-check ownership against the CURRENT active set so a stale decision
        can never REMOVE the final owned camera and leave only foreign streams.

        Logically atomic: reads ownership (``_get_owned_camera_ids``) and the
        active set (``_self_state.active_cameras`` under ``_self_lock``) with
        no intervening wait/await before the caller acts on the result.

        Returns True if the REMOVE may proceed; False if it must be aborted
        (this camera is the last locally-owned active camera, or ownership is
        unresolved).
        """
        try:
            owned = self._get_owned_camera_ids()
        except Exception as exc:
            logger.warning(
                "[PeerOrch] L1 ownership guard: ownership lookup failed: %s", exc
            )
            owned = set()

        # Removing a foreign (rescued/migrated-in) camera never reduces the
        # locally-owned-active count, so it may always proceed.
        if owned and camera_id not in owned:
            return True

        with self._self_lock:
            active = set(self._self_state.active_cameras)

        if not owned:
            # Foreign camera check when owned is empty: if camera_id is recorded
            # as rescued or migrated-in, it is definitely foreign.
            with self._lock:
                if camera_id in self._rescued_cameras:
                    return True
            # Fail closed: cannot prove another owned camera would remain.
            return False

        # Owned camera: proceed only if some OTHER owned camera stays active.
        owned_active = owned & active
        return bool(owned_active - {camera_id})

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
        # Carry RFO trigger snapshot metrics if available, else capture under _self_lock
        with self._lock:
            snap = self._rfo_snapshots.pop(camera_id, None)
        if snap is not None:
            trigger_load, trigger_fps = snap
        else:
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
                self._pending_started_at.pop(camera_id, None)
                curr_timeouts = self._peer_consecutive_timeouts.get(winner_node, 0) + 1
                self._peer_consecutive_timeouts[winner_node] = curr_timeouts
            if owned == winner_node:
                self._peer_inflight[winner_node] = max(
                    0, self._peer_inflight.get(winner_node, 0) - 1
                )
                logger.debug(
                    "[PeerOrch] Timeout released reservation for '%s' (winner='%s', inflight=%d)",
                    camera_id, winner_node, self._peer_inflight[winner_node],
                )
            base_cooldown = self._cfg.get("cooldown_s", 45.0)
            multiplier = min(2 ** (curr_timeouts - 1), 8)
            penalty_duration = max(base_cooldown * 2, base_cooldown * multiplier)
            penalty_until = time.time() + penalty_duration
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

        # Success — REMOVE from self.
        # Final atomic ownership guard: the decision-time check in
        # _pick_camera_to_offload ran against a snapshot that may now be
        # stale. A concurrent migration / reclaim / rebalance can have
        # removed every other owned camera while we were waiting for this
        # ACK, turning camera_id into the last owned stream. Block the
        # REMOVE rather than strip the node down to foreign-only streams.
        if not self._l1_remove_ownership_guard(camera_id):
            logger.error(
                "[PeerOrch] L1 REMOVE ABORTED for '%s' (winner='%s'): it is now "
                "the last locally-owned active camera. Keeping it local; rolling back winner.",
                camera_id, winner_node,
            )
            # Send targeted rollback REMOVE to winner so it does not keep playing the stream
            if winner_node and winner_node != self._node_id:
                try:
                    winner_sid: Optional[int] = None
                    with self._lock:
                        winner_p = self._peers.get(winner_node)
                        if winner_p and isinstance(winner_p.camera_configs, dict):
                            winner_c = winner_p.camera_configs.get(camera_id)
                            if isinstance(winner_c, dict) and "source_id" in winner_c:
                                try:
                                    winner_sid = int(winner_c["source_id"])
                                except (ValueError, TypeError):
                                    pass
                    rollback_cmd: dict = {"cmd": "REMOVE", "camera_id": camera_id}
                    if winner_sid is not None:
                        rollback_cmd["source_id"] = winner_sid
                    else:
                        rollback_cmd = self._build_remove_cmd(camera_id, context="l1_guard_abort_rollback")

                    winner_control_key = f"peers/control/{winner_node}"
                    if self._session is not None:
                        self._session.put(
                            winner_control_key,
                            msgpack.packb(rollback_cmd, use_bin_type=True),
                        )
                    logger.info(
                        "[PeerOrch] Rollback REMOVE sent to winner '%s' for '%s' following L1 ownership guard abort.",
                        winner_node, camera_id,
                    )
                except Exception as exc:
                    logger.error(
                        "[PeerOrch] Failed to send rollback REMOVE to winner '%s' for '%s': %s",
                        winner_node, camera_id, exc,
                    )

            with self._lock:
                stale_winner = self._pending_winner.pop(camera_id, None)
                self._pending_started_at.pop(camera_id, None)
            if stale_winner == winner_node:
                self._peer_inflight[winner_node] = max(
                    0, self._peer_inflight.get(winner_node, 0) - 1
                )
                logger.debug(
                    "[PeerOrch] L1 REMOVE abort released reservation for '%s' "
                    "(winner='%s', inflight=%d)",
                    camera_id, winner_node, self._peer_inflight[winner_node],
                )
            self._migration_log.log(
                self._node_id, winner_node, camera_id,
                "overload", trigger_load, trigger_fps,
                time.time() * 1000 - start_ms, "OWNERSHIP_GUARD_BLOCK",
            )
            self._cam_cooldown[camera_id] = time.time()
            return

        remove_cmd = self._build_remove_cmd(camera_id, context="l1_migration")
        self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
        logger.info(
            "[PeerOrch] REMOVE sent to self for '%s'. Migration complete.",
            camera_id,
        )

        # Commit reclaimable ownership only after ACK and REMOVE are sent.
        self._cam_cooldown[camera_id] = time.time()
        with self._lock:
            self._migrated_out[camera_id] = winner_node
            if camera_id not in self._cam_migration_history:
                self._cam_migration_history[camera_id] = []
            self._cam_migration_history[camera_id].append(time.time())
        self.set_offload_level(camera_id, 0)

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
        If timeout, log error and schedule a bounded exponential retry.
        """
        # Reclaim retry: re-validate holder state from latest heartbeat
        # before each retry. If holder dropped camera, clear stale state safely.
        now = time.time()
        heartbeat_timeout = float(self._cfg.get("heartbeat_timeout_s", 5.0))
        with self._lock:
            holder_peer = self._peers.get(holder_node)
            if holder_peer is not None and (now - holder_peer.last_seen <= heartbeat_timeout):
                if camera_id not in holder_peer.active_cameras:
                    with self._self_lock:
                        is_active_local = camera_id in self._self_state.active_cameras
                    if is_active_local:
                        logger.info(
                            "[PeerOrch][Reclaim] Holder '%s' no longer has '%s' and stream active locally; clearing reclaim mapping.",
                            holder_node, camera_id,
                        )
                        self._migrated_out.pop(camera_id, None)
                        self._reclaim_in_progress.discard(camera_id)
                        self._reclaim_retry_at.pop(camera_id, None)
                        self._reclaim_retry_count.pop(camera_id, None)
                        return

        with self._lock:
            event = self._pending_acks.get(camera_id)
            if event is None:
                event = threading.Event()
                self._pending_acks[camera_id] = event

        timeout = self._cfg.get("migration_timeout_s", 15.0)
        confirmed = event.wait(timeout=timeout)

        with self._lock:
            self._pending_acks.pop(camera_id, None)

        if not confirmed:
            base_retry_s = float(self._cfg.get("reclaim_retry_s", 5.0))
            cooldown_s = float(self._cfg.get("cooldown_s", 45.0))
            if cooldown_s <= 0.0:
                cooldown_s = float("inf")

            with self._lock:
                current_retries = self._reclaim_retry_count.get(camera_id, 0) + 1
                self._reclaim_retry_count[camera_id] = current_retries
                self._reclaim_in_progress.discard(camera_id)
                backoff_s = min(base_retry_s * (2 ** (current_retries - 1)), cooldown_s)
                self._reclaim_retry_at[camera_id] = time.time() + backoff_s

            logger.error(
                "[PeerOrch] Reclaim: TIMEOUT (%ds) waiting for local ADD ack of '%s' from holder '%s' "
                "(active=%d, retry=%d) — scheduling retry in %.1fs",
                int(timeout), camera_id, holder_node,
                len(self._self_state.active_cameras), current_retries, backoff_s,
            )
            return

        # Stream confirmed PLAYING on self
        # Check if holder is known offline/dead. If so, skip sending REMOVE.
        now = time.time()
        timeout = self._cfg.get("heartbeat_timeout_s", 5.0)
        grace_s = self._cfg.get("failover_grace_s", timeout)
        offline_threshold = timeout + grace_s

        with self._lock:
            holder_peer = self._peers.get(holder_node)
            is_dead = (
                holder_node in self._failover_triggered
                or holder_node in self._peer_offline_at
                or (holder_peer is not None and (now - holder_peer.last_seen > offline_threshold))
            )

        if is_dead:
            with self._lock:
                self._migrated_out.pop(camera_id, None)
                self._reclaim_in_progress.discard(camera_id)
                self._reclaim_retry_at.pop(camera_id, None)
                self._reclaim_retry_count.pop(camera_id, None)
                self._reclaim_attempts.pop(camera_id, None)
                self._reclaim_pending_remove.pop(camera_id, None)
            logger.info(
                "[PeerOrch] Reclaim: stream PLAYING on self — skipped REMOVE to '%s' (holder dead/offline). Reclaim complete.",
                holder_node,
            )
            self._migration_log.log(
                holder_node, self._node_id, camera_id,
                "reclaim", getattr(self._self_state, "load_score", 0.0), None,
                0.0, "RECLAIMED",
            )
            return

        # Re-check ownership immediately before REMOVE. Heartbeat state may
        # have changed while the local ADD was waiting for PLAYING.
        with self._lock:
            holder_peer = self._peers.get(holder_node)
            holder_still_has_camera = bool(
                holder_peer is None or camera_id in holder_peer.active_cameras
            )
        if not holder_still_has_camera:
            with self._lock:
                self._migrated_out.pop(camera_id, None)
                self._reclaim_in_progress.discard(camera_id)
                self._reclaim_retry_at.pop(camera_id, None)
                self._reclaim_retry_count.pop(camera_id, None)
                self._reclaim_attempts.pop(camera_id, None)
                self._reclaim_pending_remove.pop(camera_id, None)
            logger.info(
                "[PeerOrch] Reclaim: holder '%s' no longer owns '%s'; skipped REMOVE.",
                holder_node, camera_id,
            )
            self._migration_log.log(
                holder_node, self._node_id, camera_id,
                "reclaim", getattr(self._self_state, "load_score", 0.0), None,
                0.0, "RECLAIMED",
            )
            return

        # Holder is alive — safe to remove from holder
        # Note: on holder node, source_id cannot be locally resolved, so we query holder peer camera_configs
        holder_sid: Optional[int] = None
        holder_epoch: Optional[int] = None
        holder_mig_id: Optional[str] = None
        with self._lock:
            holder_p = self._peers.get(holder_node)
            if holder_p and isinstance(holder_p.camera_configs, dict):
                holder_c = holder_p.camera_configs.get(camera_id)
                if isinstance(holder_c, dict) and "source_id" in holder_c:
                    try:
                        holder_sid = int(holder_c["source_id"])
                    except (ValueError, TypeError):
                        pass
            holder_epoch = self._camera_epochs.get(camera_id)
            holder_mig_id = self._camera_migration_ids.get(camera_id)

        remove_cmd: dict = {"cmd": "REMOVE", "camera_id": camera_id}
        if holder_sid is not None:
            remove_cmd["source_id"] = holder_sid
        else:
            logger.debug(
                "[PeerOrch] Reclaim REMOVE for '%s' to '%s' emitted without source_id (holder config not reporting source_id).",
                camera_id, holder_node,
            )
        if holder_epoch is not None:
            remove_cmd["epoch"] = holder_epoch
        if holder_mig_id is not None:
            remove_cmd["migration_id"] = holder_mig_id

        # Register dedicated remove ACK event before sending REMOVE
        remove_ack_event = threading.Event()
        with self._lock:
            self._reclaim_remove_acks[camera_id] = remove_ack_event
            self._reclaim_pending_remove[camera_id] = holder_node
            if holder_epoch is not None:
                self._reclaim_pending_remove_epoch[camera_id] = holder_epoch
            if holder_mig_id is not None:
                self._reclaim_pending_remove_mig_id[camera_id] = holder_mig_id

        holder_control_key = f"peers/control/{holder_node}"
        remove_sent = False
        try:
            if self._session is not None:
                self._session.put(
                    holder_control_key,
                    msgpack.packb(remove_cmd, use_bin_type=True),
                )
                remove_sent = True
        except Exception as exc:
            logger.error("[PeerOrch] Reclaim: failed to send REMOVE to '%s' for '%s': %s", holder_node, camera_id, exc)

        remove_confirmed = False
        if remove_sent:
            remove_timeout = float(self._cfg.get("remove_ack_timeout_s", 10.0))
            remove_confirmed = remove_ack_event.wait(timeout=remove_timeout)

        with self._lock:
            self._reclaim_remove_acks.pop(camera_id, None)
            self._reclaim_pending_remove.pop(camera_id, None)
            self._reclaim_pending_remove_epoch.pop(camera_id, None)
            self._reclaim_pending_remove_mig_id.pop(camera_id, None)

        if remove_confirmed:
            with self._lock:
                self._migrated_out.pop(camera_id, None)
                self._reclaim_in_progress.discard(camera_id)
                self._reclaim_retry_at.pop(camera_id, None)
                self._reclaim_retry_count.pop(camera_id, None)
                self._reclaim_attempts.pop(camera_id, None)
            logger.info(
                "[PeerOrch] Reclaim: stream PLAYING on self and REMOVE ACK confirmed from '%s' for '%s'. Reclaim complete.",
                holder_node, camera_id,
            )
            self._migration_log.log(
                holder_node, self._node_id, camera_id,
                "reclaim", getattr(self._self_state, "load_score", 0.0), None,
                0.0, "RECLAIMED",
            )
        else:
            base_retry_s = float(self._cfg.get("reclaim_retry_s", 5.0))
            cooldown_s = float(self._cfg.get("cooldown_s", 45.0))
            if cooldown_s <= 0.0:
                cooldown_s = float("inf")
            with self._lock:
                current_retries = self._reclaim_retry_count.get(camera_id, 0) + 1
                self._reclaim_retry_count[camera_id] = current_retries
                self._reclaim_in_progress.discard(camera_id)
                backoff_s = min(base_retry_s * (2 ** (current_retries - 1)), cooldown_s)
                self._reclaim_retry_at[camera_id] = time.time() + backoff_s

            logger.error(
                "[PeerOrch] Reclaim: REMOVE unconfirmed (timeout or send failure) to '%s' for '%s' — scheduling retry in %.1fs (retry=%d)",
                holder_node, camera_id, backoff_s, current_retries,
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

            ack_epoch = payload.get("epoch")
            ack_mig_id = payload.get("migration_id")
            pending_epoch = self._pending_epochs.get(camera_id)
            pending_mig_id = self._pending_migration_ids.get(camera_id)

            if pending_epoch is not None and ack_epoch is not None:
                try:
                    if int(ack_epoch) != int(pending_epoch):
                        logger.warning(
                            "[PeerOrch] Ignoring stale ACK for '%s': ack epoch=%r != pending epoch=%r",
                            camera_id, ack_epoch, pending_epoch,
                        )
                        return
                except (ValueError, TypeError):
                    return
            if pending_mig_id is not None and ack_mig_id is not None:
                if str(ack_mig_id) != str(pending_mig_id):
                    logger.warning(
                        "[PeerOrch] Ignoring mismatched ACK for '%s': ack mig_id=%r != pending mig_id=%r",
                        camera_id, ack_mig_id, pending_mig_id,
                    )
                    return

            if expected_winner is not None and ack_node not in ("", expected_winner):
                # Wrong/forged/stale sender for an in-flight migration — fail closed.
                logger.debug(
                    "[PeerOrch] Ignoring ACK for '%s': sender='%s' != expected winner='%s'.",
                    camera_id, ack_node, expected_winner,
                )
                return
            if expected_winner is None and ack_node not in ("", self._node_id):
                # No pending migration but a foreign sender claims this camera —
                # ambiguous, fail closed (do not set event, do not release).
                logger.debug(
                    "[PeerOrch] Ignoring ACK for '%s': no pending migration but "
                    "sender='%s' != self.", camera_id, ack_node,
                )
                return

            # Authenticated — now atomically claim the reservation if we own it.
            winner_id = self._pending_winner.pop(camera_id, None)
            self._pending_started_at.pop(camera_id, None)
            self._pending_epochs.pop(camera_id, None)
            self._pending_migration_ids.pop(camera_id, None)
            if winner_id is not None:
                self._peer_inflight[winner_id] = max(
                    0, self._peer_inflight.get(winner_id, 0) - 1
                )
                self._peer_consecutive_timeouts.pop(winner_id, None)
            elif ack_node:
                self._peer_consecutive_timeouts.pop(ack_node, None)
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

    def _on_remove_ack(self, payload: dict) -> None:
        """
        Handle incoming REMOVE ACK on peers/remove/ack/{cam_id}.
        Validates camera_id, source_id, epoch, and migration_id against in-flight reclaim/remove requests.
        """
        cam_id = payload.get("camera_id") or payload.get("cam_id")
        if not cam_id:
            return
        ack_node = payload.get("node_id", "")
        event_type = payload.get("event")
        if event_type is not None and event_type != "REMOVED":
            logger.warning(
                "[PeerOrch] Ignoring remove ACK for '%s': event='%s' != 'REMOVED'.",
                cam_id, event_type,
            )
            return

        with self._lock:
            pending_holder = self._reclaim_pending_remove.get(cam_id)
            event = self._reclaim_remove_acks.get(cam_id)
            if event is None:
                logger.debug("[PeerOrch] No pending remove ACK event for '%s', ignoring.", cam_id)
                return

            if pending_holder is not None and ack_node not in ("", pending_holder):
                logger.warning(
                    "[PeerOrch] Ignoring remove ACK for '%s': sender='%s' != pending holder='%s'.",
                    cam_id, ack_node, pending_holder,
                )
                return

            ack_epoch = payload.get("epoch")
            ack_mig_id = payload.get("migration_id")
            pending_epoch = self._reclaim_pending_remove_epoch.get(cam_id)
            pending_mig_id = self._reclaim_pending_remove_mig_id.get(cam_id)

            if pending_epoch is not None and ack_epoch is not None:
                try:
                    if int(ack_epoch) != int(pending_epoch):
                        logger.warning(
                            "[PeerOrch] Ignoring stale remove ACK for '%s': ack epoch=%r != pending epoch=%r",
                            cam_id, ack_epoch, pending_epoch,
                        )
                        return
                except (ValueError, TypeError):
                    return
            if pending_mig_id is not None and ack_mig_id is not None:
                if str(ack_mig_id) != str(pending_mig_id):
                    logger.warning(
                        "[PeerOrch] Ignoring mismatched remove ACK for '%s': ack mig_id=%r != pending mig_id=%r",
                        cam_id, ack_mig_id, pending_mig_id,
                    )
                    return

            event.set()
            logger.info("[PeerOrch] Remove ACK confirmed for '%s' from '%s'", cam_id, ack_node)

    def _is_peer_ready_for_yield(self, peer: Optional[PeerState], camera_id: str) -> bool:
        """Predicate to check if the original owner is alive, ready, and running the camera.

        Conditions:
          - peer is known and heartbeat is fresh
          - peer status is not waiting/recovering
          - peer has valid positive FPS
          - peer is not overloaded
          - camera_id is in peer.active_cameras
        """
        if peer is None:
            return False
        now = time.time()
        timeout = float(self._cfg.get("heartbeat_timeout_s", 5.0))
        if (now - peer.last_seen) > timeout:
            return False
        peer_fps_valid = _has_valid_positive_fps(peer.fps_per_camera)
        peer_in_waiting = is_waiting_state(peer.fps_per_camera, peer.active_cameras, peer.status)
        peer_overloaded = self._is_overloaded(
            peer.load_score,
            peer.risk_index,
            peer.qos_state,
        )
        if not peer_fps_valid or peer_in_waiting or peer_overloaded:
            return False
        if camera_id not in peer.active_cameras:
            return False
        # Also ensure peer has capacity (active_cameras <= max_streams)
        if len(peer.active_cameras) > peer.max_streams:
            return False
        return True

    def _on_failover_claim(self, payload: dict) -> None:
        """Handle incoming rescue claim broadcast on peers/failover/claim.

        Used for contention resolution when multiple nodes detect the same dead peer,
        or for startup preemption claims when the true owner boots up.
        """
        dead_node_id = payload.get("dead_node_id", "")
        camera_id = payload.get("camera_id", "")
        claimer_node_id = payload.get("claimer_node_id") or payload.get("claimer", "")
        priority_weight = payload.get("priority_weight", 0)
        claim_type = payload.get("type", "")
        ts = payload.get("ts", time.time())

        # Check for startup preemption claim: original owner booted and claims its cameras
        if claim_type == "startup_claim" or payload.get("action") == "startup_preempt":
            claimed_cameras = payload.get("cameras", [])
            if not claimed_cameras and camera_id:
                claimed_cameras = [camera_id]
            logger.info(
                "[Failover] Received startup preemption announcement from '%s' for cameras: %s",
                claimer_node_id, claimed_cameras,
            )
            with self._lock:
                peer = self._peers.get(claimer_node_id)
                for cam in claimed_cameras:
                    # Validate static ownership of the claimer
                    claimer_owned = self._get_node_owned_cameras(claimer_node_id)
                    if claimer_owned is not None and cam not in claimer_owned:
                        logger.warning(
                            "[Failover] Rejected startup claim from '%s' for '%s': not statically owned by claimer.",
                            claimer_node_id, cam,
                        )
                        continue

                    # Check if holding dynamically (either in _rescued_cameras, or active on self but not statically owned)
                    self_owned = self._get_owned_camera_ids()
                    if cam in self_owned:
                        # Own home camera — do not yield
                        continue

                    is_rescued = (self._rescued_cameras.get(cam) == claimer_node_id or cam in self._rescued_cameras)
                    with self._self_lock:
                        is_active_local = cam in self._self_state.active_cameras

                    if not is_rescued and not is_active_local:
                        continue

                    if not self._is_peer_ready_for_yield(peer, cam):
                        logger.info(
                            "[Failover] Deferred yield of camera '%s' on startup claim from '%s': owner not ready/active.",
                            cam, claimer_node_id,
                        )
                        continue

                    if not self._l1_remove_ownership_guard(cam):
                        logger.info(
                            "[Failover] Aborted yield of '%s' on startup claim from '%s': last owned camera guard.",
                            cam, claimer_node_id,
                        )
                        continue

                    orig = self._rescued_cameras.pop(cam, None)
                    self._rescued_at.pop(cam, None)
                    remove_cmd = self._build_remove_cmd(cam, context="startup_announcement_yield")
                    if self._pubs.get("control") is not None:
                        self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
                    logger.info(
                        "[Failover] Yielded camera '%s' (orig owner '%s') due to startup announcement from '%s'.",
                        cam, orig or claimer_node_id, claimer_node_id,
                    )
            return

        if not dead_node_id or not camera_id or not claimer_node_id:
            return

        # If the claimer is the original owner claiming its camera back, yield only if ready
        with self._lock:
            if self._rescued_cameras.get(camera_id) == claimer_node_id:
                # Validate static ownership
                claimer_owned = self._get_node_owned_cameras(claimer_node_id)
                if claimer_owned is not None and camera_id not in claimer_owned:
                    logger.info(
                        "[Failover] Rejected claim from '%s' for '%s': not statically owned by claimer.",
                        claimer_node_id, camera_id,
                    )
                    return
                peer = self._peers.get(claimer_node_id)
                if not self._is_peer_ready_for_yield(peer, camera_id):
                    logger.info(
                        "[Failover] Deferred yield of rescued camera '%s' to original owner '%s' via failover_claim: owner not ready/active.",
                        camera_id, claimer_node_id,
                    )
                    return
                self._rescued_cameras.pop(camera_id, None)
                self._rescued_at.pop(camera_id, None)
                remove_cmd = self._build_remove_cmd(camera_id, context="failover_claim_yield")
                if self._pubs.get("control") is not None:
                    self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
                logger.info(
                    "[Failover] Yielded rescued camera '%s' to original owner '%s' via failover_claim.",
                    camera_id, claimer_node_id,
                )
                return

        local_now = time.time()
        with self._claims_lock:
            # Claims are only needed during the claim window / lease period. Bound
            # this cache using configured rescue_claim_lease_s (default 15s) or 4x window.
            # Local receipt time is used for lease freshness to prevent cross-node clock skew.
            claim_lease_s = float(self._cfg.get("rescue_claim_lease_s", max(15.0, float(self._cfg.get("rescue_claim_window_s", 0.5)) * 4.0)))
            cutoff = local_now - claim_lease_s
            self._failover_claims = {
                key: claim for key, claim in self._failover_claims.items()
                if claim[1] >= cutoff
            }
            key = (dead_node_id, camera_id)
            existing = self._failover_claims.get(key)
            if existing is None or priority_weight > existing[2]:
                self._failover_claims[key] = (claimer_node_id, local_now, priority_weight, float(ts))
                logger.info(
                    "[Failover] Recorded rescue claim for '%s' (dead='%s') by '%s' (weight=%d, remote_ts=%.3f)",
                    camera_id, dead_node_id, claimer_node_id, priority_weight, float(ts),
                )

    def _publish_startup_announcement(self) -> None:
        """One-shot startup announcement for statically owned cameras."""
        try:
            owned = list(self._get_owned_camera_ids())
            if not owned:
                return
            now_ts = time.time()
            claim_payload = {
                "type": "startup_claim",
                "action": "startup_preempt",
                "claimer_node_id": self._node_id,
                "cameras": owned,
                "timestamp": now_ts,
                "ts": now_ts,
            }
            if "failover_claim" in self._pubs and self._pubs["failover_claim"] is not None:
                self._pubs["failover_claim"].put(msgpack.packb(claim_payload, use_bin_type=True))
                logger.info(
                    "[PeerOrch] Published startup announcement for owned cameras: %s",
                    owned,
                )
        except Exception as exc:
            logger.warning("[PeerOrch] Failed to publish startup announcement: %s", exc)

    # ------------------------------------------------------------------
    # Leaderless failover (Phase 5)
    # ------------------------------------------------------------------

    @staticmethod
    def _consistent_hash(camera_id: str, peer_ids: List[str]) -> str:
        """
        Rendezvous / Highest Random Weight (HRW) hashing:
        Computes sha256(camera_id:peer_id) for each candidate peer; highest hash wins.
        
        Properties over modulo hashing:
          1. Minimal disruption on membership change: when a node leaves/fails,
             only its assigned cameras are reassigned; remaining cameras stay bound.
          2. Uniform distribution across alive peers.
          3. Deterministic across all nodes that see the same peer set.
        """
        if not peer_ids:
            return ""
        # ponytail: HRW rendezvous hash ensures true consistent hashing property
        best_peer = ""
        best_weight = -1
        for pid in sorted(peer_ids):
            combined = f"{camera_id}:{pid}".encode("utf-8")
            weight = int(hashlib.sha256(combined).hexdigest(), 16)
            if weight > best_weight:
                best_weight = weight
                best_peer = pid
        return best_peer

    def _leaderless_failover(self, dead_node_id: str, orphaned_cameras: List[str]) -> None:
        """
        Rescue orphaned cameras using consistent hash.

        Each surviving peer runs independently → same hash result.
        Winner executes ADD after jitter (0-2s) to avoid race.
        After jitter, check peers/status/+ to see if camera already rescued.

        Dead-owner filtering
        ────────────────────
        The dead peer's ``active_cameras`` can contain stale entries:
          * cameras that were being migrated TO the dead peer (in flight when it died)
          * cameras that the dead peer had previously rescued from another peer
          * cameras that were offloaded (L2/L3) to the dead peer

        Rescuing any of those creates duplicate streams — the original owner
        (or another alive peer) is already running them.  The dead peer's
        ``camera_configs`` (populated from its last heartbeat's
        ``pipeline.camera_configs``) is the authoritative "owned by the dead
        node" set: cameras it was configured to manage.  We intersect the
        orphan list with those keys before rescue.  If the dead peer never
        populated ``camera_configs`` (older firmware / missing field), we fall
        back to the full orphan list — never silently drop a mandatory rescue.

        Duplicate prevention (local)
        ────────────────────────────
        The consistent hash ensures only one node wins each camera across the
        cluster.  Within this node, we additionally guard against double-rescue:
          * camera already in ``_rescued_cameras`` (rescued in a prior run)
          * camera already in ``_self_state.active_cameras`` (we're running it)
        Both are checked under their respective locks.
        """
        cfg = self._cfg
        now = time.time()
        timeout = cfg.get("heartbeat_timeout_s", 5.0)

        cm = self._camera_manager
        default_max = cm.get_max_streams() if cm is not None else 4
        eps_max = int(self._cfg.get("eps_streams_max", default_max))
        # Configurable rescue ceiling, default eps_streams_max - 1 (e.g. 3 when eps_streams_max=4)
        rescue_ceiling = int(self._cfg.get("failover_rescue_max", eps_max - 1))

        # Read dead peer's camera configs AND build alive_peers in a single lock
        # acquisition to prevent torn reads between the two operations.
        with self._lock:
            dead_peer = self._peers.get(dead_node_id)
            peer_cam_configs = dict(dead_peer.camera_configs) if dead_peer else {}

            # Build alive candidate list — includes self so this node can rescue too
            # BUG-1 fix: read active_cameras from _self_state under _self_lock.
        with self._self_lock:
            self_streams = len(self._self_state.active_cameras)
        with self._lock:
            self_eligible = (
                self._node_id != dead_node_id
                and self_streams < rescue_ceiling
            )
            alive_peers = sorted([
                nid for nid, peer in self._peers.items()
                if nid != dead_node_id
                and now - peer.last_seen <= timeout
                and len(peer.active_cameras) < peer.max_streams
            ] + ([self._node_id] if self_eligible else []))

        # Dead-owner filtering: camera MUST be originally owned by dead_node_id.
        # Check static mapping / dead peer's camera_configs.
        # Under NO circumstance can a node rescue a camera that belongs to itself or an alive peer.
        dead_owned_set = self._get_node_owned_cameras(dead_node_id)
        if dead_owned_set is not None:
            before_n = len(orphaned_cameras)
            orphaned_cameras = [c for c in orphaned_cameras if c in dead_owned_set]
            filtered_out = before_n - len(orphaned_cameras)
            if filtered_out:
                logger.info(
                    "[Failover] Filtered %d non-owned/stale entries from '%s' orphan list "
                    "(only rescue cameras originally owned by '%s').",
                    filtered_out, dead_node_id, dead_node_id,
                )
        elif peer_cam_configs:
            owned_set = set(peer_cam_configs.keys())
            before_n = len(orphaned_cameras)
            orphaned_cameras = [c for c in orphaned_cameras if c in owned_set]
            filtered_out = before_n - len(orphaned_cameras)
            if filtered_out:
                logger.info(
                    "[Failover] Filtered %d stale/non-owned entries from '%s' orphan list "
                    "(active_cameras included offload/migrated-in cameras).",
                    filtered_out, dead_node_id,
                )

        # Strict exclusion: NEVER rescue cameras owned by self or by an alive peer
        self_owned = self._get_owned_camera_ids()
        alive_peer_owned = set()
        with self._lock:
            for nid, p in self._peers.items():
                if nid != dead_node_id and nid != self._node_id and (now - p.last_seen <= timeout):
                    p_owned = self._get_node_owned_cameras(nid)
                    if p_owned:
                        alive_peer_owned.update(p_owned)

        excluded_cams = self_owned | alive_peer_owned
        if excluded_cams:
            before_ex = len(orphaned_cameras)
            orphaned_cameras = [c for c in orphaned_cameras if c not in excluded_cams]
            if len(orphaned_cameras) < before_ex:
                logger.info(
                    "[Failover] Excluded %d cameras belonging to self or alive peers from '%s' rescue.",
                    before_ex - len(orphaned_cameras), dead_node_id,
                )

        if not orphaned_cameras:
            logger.info(
                "[Failover] No valid owned orphans from '%s' to rescue. Skipping.",
                dead_node_id,
            )
            return

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
                # Local-side duplicate guard: skip if WE already rescued this
                # camera in a prior failover run, or if we're already running it or have uncommitted migration.
                with self._lock:
                    if (camera_id in self._rescued_cameras
                            or camera_id in self._pending_acks
                            or camera_id in self._pending_winner
                            or camera_id in self._reclaim_in_progress):
                        logger.info(
                            "[Failover] Camera '%s' in uncommitted state or already in _rescued_cameras (owner='%s'). "
                            "Skipping duplicate rescue.",
                            camera_id, self._rescued_cameras.get(camera_id, "unknown"),
                        )
                        continue
                with self._self_lock:
                    if camera_id in self._self_state.active_cameras:
                        logger.info(
                            "[Failover] Camera '%s' already active on self. Skipping duplicate rescue.",
                            camera_id,
                        )
                        continue

                # Re-check capacity including cameras accepted earlier in this loop against rescue ceiling
                # BUG-1 fix: read active_cameras and load_score under _self_lock
                with self._self_lock:
                    current_streams = len(self._self_state.active_cameras)
                    current_load = self._self_state.load_score
                if current_streams + self_accepted >= rescue_ceiling:
                    logger.warning(
                        "[Failover] Cannot rescue '%s': at rescue ceiling (%d >= %d). Skipping.",
                        camera_id, current_streams + self_accepted, rescue_ceiling,
                    )
                    continue

                # Capacity load-score gate: do not rescue if node is already near/above overload
                overload_thresh = self._cfg.get("overload_threshold", 60.0)
                if current_load >= overload_thresh:
                    logger.warning(
                        "[Failover] Cannot rescue '%s': self load_score (%.1f) >= overload threshold (%.1f). Skipping.",
                        camera_id, current_load, overload_thresh,
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

                # Fix E: Broadcast rescue claim to resolve membership-divergence split-brain
                # Check if a fresh higher-priority claim lease already exists before publishing/claiming
                claim_lease_s = float(cfg.get("rescue_claim_lease_s", 15.0))
                with self._claims_lock:
                    existing_claim = self._failover_claims.get((dead_node_id, camera_id))
                    if (existing_claim is not None
                            and existing_claim[0] != self._node_id
                            and (time.time() - existing_claim[1]) < claim_lease_s):
                        logger.info(
                            "[Failover] Active rescue claim lease exists for '%s' by '%s' (age=%.1fs); skipping local claim.",
                            camera_id, existing_claim[0], time.time() - existing_claim[1],
                        )
                        continue

                # ponytail: mask to 63-bit int so msgpack integer serialization never overflows
                my_weight = int(hashlib.sha256(f"{camera_id}:{self._node_id}".encode()).hexdigest()[:15], 16)
                claim_now = time.time()
                claim_payload = {
                    "dead_node_id": dead_node_id,
                    "camera_id": camera_id,
                    "claimer_node_id": self._node_id,
                    "priority_weight": my_weight,
                    "timestamp": claim_now,
                    "ts": claim_now,
                }
                if "failover_claim" in self._pubs:
                    try:
                        self._pubs["failover_claim"].put(msgpack.packb(claim_payload, use_bin_type=True))
                    except Exception as e:
                        logger.warning("[Failover] Could not publish rescue claim: %s", e)

                # Record own claim locally
                with self._claims_lock:
                    self._failover_claims[(dead_node_id, camera_id)] = (self._node_id, claim_now, my_weight, claim_now)

                # Wait configured claim window to collect peer rescue claims
                claim_window_s = float(cfg.get("rescue_claim_window_s", 0.5))
                if claim_window_s > 0:
                    time.sleep(claim_window_s)

                # Check if a peer claimed with higher weight during the window
                with self._claims_lock:
                    best_claim = self._failover_claims.get((dead_node_id, camera_id))
                    if best_claim is not None and best_claim[0] != self._node_id and best_claim[2] > my_weight:
                        logger.info(
                            "[Failover] Yielding rescue of '%s' to '%s' (higher weight %d > %d)",
                            camera_id, best_claim[0], best_claim[2], my_weight,
                        )
                        continue

                # Stale holder guard before rescue ADD: verify dead holder is still stale
                # under current freshness rules (heartbeat timeout + grace) to avoid acting on transient missed heartbeat
                now_check = time.time()
                timeout = float(self._cfg.get("heartbeat_timeout_s", 5.0))
                grace_s = float(self._cfg.get("failover_grace_s", timeout))
                offline_threshold = timeout + grace_s
                with self._lock:
                    dead_peer_state = self._peers.get(dead_node_id)
                    if dead_peer_state is not None and (now_check - dead_peer_state.last_seen) <= offline_threshold:
                        logger.info(
                            "[Failover] Aborted rescue ADD of '%s': dead holder '%s' is no longer stale (last_seen %.1fs ago <= %.1fs threshold).",
                            camera_id, dead_node_id, now_check - dead_peer_state.last_seen, offline_threshold,
                        )
                        continue

                # Verify the camera RTSP source is reachable before adding.
                # If the source is hosted on the dead node, it's unreachable.
                cam_uri = cam_config.get("uri", "")
                rtt = self._measure_rtt(cam_uri)
                if rtt is None:
                    logger.info(
                        "[Failover] Camera '%s' source unreachable (%s). Skipping.",
                        camera_id, cam_uri,
                    )
                    continue

                # Alive-peer ownership guard: another surviving node may have
                # rescued this camera in an earlier round whose claim lease has
                # since expired. Never ADD a camera an alive peer currently
                # reports owning.
                alive_owner = None
                with self._lock:
                    for other_id, other_state in self._peers.items():
                        if other_id == dead_node_id:
                            continue
                        if ((time.time() - other_state.last_seen) <= timeout
                                and camera_id in other_state.active_cameras):
                            alive_owner = other_id
                            break
                if alive_owner is not None:
                    logger.info(
                        "[Failover] Camera '%s' already handled by alive peer '%s'. Skipping.",
                        camera_id, alive_owner,
                    )
                    continue

                # Re-check local active ownership immediately before publishing failover ADD
                with self._self_lock:
                    if camera_id in self._self_state.active_cameras:
                        logger.info(
                            "[Failover] Camera '%s' became active locally before publishing. Skipping.",
                            camera_id,
                        )
                        continue

                add_cmd = {**cam_config, "cmd": "ADD"}
                self._pubs["control"].put(msgpack.packb(add_cmd, use_bin_type=True))
                self_accepted += 1
                with self._lock:
                    self._rescued_cameras[camera_id] = dead_node_id
                    self._rescued_at[camera_id] = time.time()
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

        # A completed failover must not retain one claim per rescued camera.
        # Keep only claims that may still be observed by a peer in flight.
        with self._claims_lock:
            for camera_id in orphaned_cameras:
                self._failover_claims.pop((dead_node_id, camera_id), None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_local_source_id(self, camera_id: str) -> Optional[int]:
        """
        Resolve the source_id for a locally active/configured camera.
        Returns int source_id if resolved, or None if unknown/not found.
        """
        if self._camera_manager is not None:
            try:
                with self._camera_manager._lock:
                    cfg_obj = self._camera_manager._configs.get(camera_id)
                    if cfg_obj is not None and getattr(cfg_obj, "enabled", True):
                        return int(cfg_obj.source_id)
            except Exception as exc:
                logger.debug("[PeerOrch] Could not resolve source_id from CameraManager for '%s': %s", camera_id, exc)

        cfg = self._get_camera_config(camera_id)
        if cfg and "source_id" in cfg:
            try:
                return int(cfg["source_id"])
            except (ValueError, TypeError):
                pass
        return None

    def _build_remove_cmd(self, camera_id: str, context: str = "") -> dict:
        """
        Build a REMOVE command dict for camera_id, attaching resolved source_id, epoch, and migration_id if available.
        """
        cmd: dict = {"cmd": "REMOVE", "camera_id": camera_id}
        sid = self._resolve_local_source_id(camera_id)
        if sid is not None:
            cmd["source_id"] = sid
        else:
            logger.info(
                "[PeerOrch] REMOVE for '%s' (%s) emitted without source_id: could not resolve active source_id.",
                camera_id, context or "unknown",
            )
        epoch = self._camera_epochs.get(camera_id)
        if epoch is not None:
            cmd["epoch"] = epoch
        mig_id = self._camera_migration_ids.get(camera_id) if hasattr(self, "_camera_migration_ids") else None
        if mig_id is not None:
            cmd["migration_id"] = mig_id
        return cmd

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

        Ownership is resolved in order:
          1. Static node_camera_map in config: if configured for self._node_id,
             this static assignment strictly defines the node's owned cameras.
          2. Live CameraManager: enabled static cameras (is_dynamic=False).
          3. Fallback cameras.yml on disk.

        Ownership = cameras enabled in this node's local cameras.yml or node_camera_map.
        Rescued and migrated-in cameras are NOT owned by this node.
        """
        # Highest priority: static node_camera_map configured for this node
        node_cam_map = self._cfg.get("node_camera_map")
        if isinstance(node_cam_map, dict) and self._node_id in node_cam_map:
            cams = node_cam_map.get(self._node_id)
            if isinstance(cams, (list, tuple, set)):
                return set(cams)

        # Fast path: live CameraManager (hot-reloaded via inotify)
        cm = self._camera_manager
        if cm is not None:
            try:
                with cm._lock:
                    return {
                        c.camera_id for c in cm._configs.values()
                        if getattr(c, "enabled", False) and not getattr(c, "is_dynamic", False)
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

    def _get_node_owned_cameras(self, target_node_id: str) -> Optional[set]:
        """
        Return the set of cameras configured to be owned by target_node_id.
        Checks static node_camera_map in config first, then target peer's camera_configs.
        Returns None if ownership cannot be determined.
        """
        node_cam_map = self._cfg.get("node_camera_map")
        if isinstance(node_cam_map, dict) and target_node_id in node_cam_map:
            cams = node_cam_map.get(target_node_id)
            if isinstance(cams, (list, tuple, set)):
                return set(cams)
        
        with self._lock:
            peer = self._peers.get(target_node_id)
            if peer and peer.camera_configs:
                return set(peer.camera_configs.keys())
        return None

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

        # Bounce dampening: exclude cameras that have reached bounce_max migrations within bounce_window_s
        bounce_max = int(self._cfg.get("bounce_max", 3))
        bounce_window_s = float(self._cfg.get("bounce_window_s", 300.0))
        bounced_cameras = set()
        with self._lock:
            for cam_id, history in list(self._cam_migration_history.items()):
                # Filter out expired timestamps
                valid_history = [ts for ts in history if now - ts <= bounce_window_s]
                if len(valid_history) != len(history):
                    if valid_history:
                        self._cam_migration_history[cam_id] = valid_history
                    else:
                        self._cam_migration_history.pop(cam_id, None)
                if len(valid_history) >= bounce_max:
                    bounced_cameras.add(cam_id)

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

        # Split eligible cameras into foreign (not in cameras.yml) and owned
        owned_cam_ids = self._get_owned_camera_ids()
        foreign_eligible = {c: w for c, w in eligible.items() if c not in owned_cam_ids}
        owned_eligible = {c: w for c, w in eligible.items() if c in owned_cam_ids}

        if level == 1:
            # Bounce dampening: filter out bounced cameras from L1 candidates
            foreign_l1 = {c: w for c, w in foreign_eligible.items() if c not in bounced_cameras}
            owned_l1 = {c: w for c, w in owned_eligible.items() if c not in bounced_cameras}

            # L1 ownership guard: never migrate away the last owned camera.
            # Explicit L1 guard: never select an L1 candidate when <=1 locally-owned active camera.
            owned_active = owned_cam_ids & set(state.active_cameras)
            if len(owned_active) == 0:
                logger.info(
                    "[PeerOrch] L1 fail-safe: no locally-owned camera is active "
                    "(active=%d). Skipping migration to preserve ownership.",
                    len(state.active_cameras),
                )
                return None

            # ponytail: foreign cameras MUST NOT be L1-migrated — they can only
            # return to their original owner via the owner's reclaim path.
            # Forwarding a foreign camera to a third node via RFO creates
            # chain migration (B→A→C) that breaks owner reclaim.
            if foreign_l1:
                logger.info(
                    "[PeerOrch] L1: skipping %d foreign camera(s) — only owner can reclaim.",
                    len(foreign_l1),
                )

            # If owned active <= 1, guard against offloading owned camera
            if len(owned_active) <= 1:
                logger.info(
                    "[PeerOrch] L1 guard: <= 1 locally-owned camera active (owned_active=%d, active=%d) and no foreign candidates. Skipping L1 migration.",
                    len(owned_active), len(state.active_cameras),
                )
                return None

            # Then owned MIN workload (guard <=1 owned)
            if owned_l1:
                for c in sorted(owned_l1, key=lambda cam: owned_l1[cam]):
                    if (owned_active - {c}):
                        return c

            logger.info(
                "[PeerOrch] L1 fail-safe: every eligible candidate would leave "
                "zero owned cameras active (owned_active=%d).",
                len(owned_active),
            )
            return None

        # L2/L3 crop offload: heaviest camera = max workload. The crop
        # offload keeps the stream local; ownership guard does not apply.
        return max(eligible, key=lambda c: eligible[c])
