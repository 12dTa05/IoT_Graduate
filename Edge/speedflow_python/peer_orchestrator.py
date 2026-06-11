"""
Edge/speedflow_python/peer_orchestrator.py

Peer Orchestrator — Bộ não P2P, thay thế MasterOrchestrator.

Mỗi Edge Node chạy một instance PeerOrchestrator độc lập.
Các instance giao tiếp qua Zenoh key expressions:
  - peers/status/<node_id>  ← heartbeat từ mọi peer
  - peers/vote/request      ← RFO (Request for Offload) từ peer quá tải
  - peers/vote/proposal     ← bid từ peer có khả năng nhận
  - peers/vote/decision     ← kết quả bầu chọn
  - peers/vote/ack/{cam}    ← xác nhận stream đã PLAYING

Migration thực thi theo chiến lược Make-before-Break:
  1. Requester mở vote window → thu thập proposals (3s)
  2. Chọn winner = proposal có F(x) thấp nhất (ε-constraint)
  3. Publish decision → winner tự ADD camera vào pipeline
  4. Winner publish peers/vote/ack/{cam} khi stream PLAYING
  5. Requester nhận ack → REMOVE camera khỏi pipeline của mình
"""

from __future__ import annotations

import csv
import hashlib
import logging
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
try:
    from .settings import ROOT as _ROOT
except ImportError:
    # Standalone execution fallback (tests)
    _ROOT = Path(__file__).resolve().parents[1]

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
    """Trạng thái hiện tại của một Peer Node (thay thế NodeState)."""
    node_id: str
    load_score: float = 0.0
    gpu_percent: float = 0.0
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    gpu_temp_c: float = 0.0
    avg_fps: Optional[float] = None
    fps_per_camera: Dict[str, float] = field(default_factory=dict)
    active_cameras: List[str] = field(default_factory=list)
    camera_configs: Dict[str, dict] = field(default_factory=dict)
    max_streams: int = 8
    last_seen: float = field(default_factory=time.time)
    overload_since: Optional[float] = None
    penalty_until: float = 0.0


# ---------------------------------------------------------------------------
# Migration Log
# ---------------------------------------------------------------------------

class MigrationLogger:
    """Ghi log mỗi lần migration ra file CSV — copy từ master_orchestrator.py."""

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
    Phiên bản P2P của MasterOrchestrator — chạy trên mỗi Edge Node.

    Giao tiếp qua Zenoh (peer mode, key expressions).
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

        # Trạng thái peers
        self._peers: Dict[str, PeerState] = {}
        self._lock = threading.RLock()

        # Trạng thái của chính node này (cập nhật từ peers/status/+ của mình)
        self._self_state = PeerState(node_id=node_id)
        # BUG-1 fix: separate lock for _self_state so the Zenoh callback
        # thread and the decision loop never see a torn write.
        self._self_lock = threading.RLock()

        # Migration log — relative to Edge/logs/
        log_dir = _ROOT / "logs"
        self._migration_log = MigrationLogger(log_dir / "p2p_migrations.csv")

        # Cooldown per-camera: camera_id → timestamp migration gần nhất
        self._cam_cooldown: Dict[str, float] = {}

        # Vote windows: camera_id → list[proposal]
        self._vote_windows: Dict[str, List[dict]] = {}
        self._vote_timers: Dict[str, threading.Timer] = {}

        # Pending ack events cho Make-before-Break
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

        # Penalty timestamp for this node itself (set on migration timeout rollback)
        self._self_penalty_until: float = 0.0

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
        """Cập nhật PeerState từ heartbeat."""
        node_id = payload.get("node_id", "")
        if not node_id:
            return

        # Cập nhật trạng thái của chính mình
        if node_id == self._node_id:
            # BUG-1 fix: hold _self_lock while updating so the decision loop
            # always reads a consistent snapshot of _self_state.
            with self._self_lock:
                self._self_state.load_score = payload.get("load_score", 0.0)
                self._self_state.gpu_percent = payload.get("gpu_percent", 0.0)
                self._self_state.cpu_percent = payload.get("cpu_percent", 0.0)
                self._self_state.ram_percent = payload.get("ram_percent", 0.0)
                self._self_state.gpu_temp_c = payload.get("gpu_temp_c", 0.0)
                pipeline = payload.get("pipeline", {})
                self._self_state.avg_fps = pipeline.get("avg_fps")
                self._self_state.fps_per_camera = pipeline.get("fps_per_camera", {})
                self._self_state.active_cameras = pipeline.get("active_cameras", [])
                # Track overload onset
                if self._self_state.load_score > self._cfg.get("overload_threshold", 75.0):
                    if self._self_state.overload_since is None:
                        self._self_state.overload_since = time.time()
                else:
                    self._self_state.overload_since = None
            return

        # Cập nhật trạng thái peer khác
        # BUG-04: update ALL mutable fields inside the lock to prevent torn
        # reads from _decision_loop running on a separate thread.
        with self._lock:
            is_new = node_id not in self._peers
            if is_new:
                self._peers[node_id] = PeerState(node_id=node_id)
                logger.info("[PeerOrch] Discovered peer '%s' via Zenoh", node_id)
            peer = self._peers[node_id]

            peer.load_score = payload.get("load_score", 0.0)
            peer.gpu_percent = payload.get("gpu_percent", 0.0)
            peer.cpu_percent = payload.get("cpu_percent", 0.0)
            peer.ram_percent = payload.get("ram_percent", 0.0)
            peer.gpu_temp_c = payload.get("gpu_temp_c", 0.0)

            pipeline = payload.get("pipeline", {})
            peer.avg_fps = pipeline.get("avg_fps")
            peer.fps_per_camera = pipeline.get("fps_per_camera", {})
            peer.active_cameras = pipeline.get("active_cameras", [])
            peer.camera_configs = pipeline.get("camera_configs", peer.camera_configs)
            peer.last_seen = time.time()

            # Track overload onset
            if peer.load_score > self._cfg.get("overload_threshold", 75.0):
                if peer.overload_since is None:
                    peer.overload_since = time.time()
            else:
                peer.overload_since = None

    # ------------------------------------------------------------------
    # Decision loop (chạy mỗi 2s)
    # ------------------------------------------------------------------

    def _decision_loop(self) -> None:
        """Vòng lặp chính — kiểm tra overload + OFFLINE peers + rebalance."""
        logger.info("[PeerOrch] Decision loop started (interval=1s).")
        while self._running:
            time.sleep(1.0)
            try:
                self._check_offline_peers()
                self._check_rebalance()
                self._check_self_overload()
            except Exception as exc:
                logger.error("[PeerOrch] Decision loop error: %s", exc)

    def _check_offline_peers(self) -> None:
        """
        Phát hiện peer OFFLINE (heartbeat timeout).
        Nếu peer có active cameras → trigger leaderless failover.
        """
        now = time.time()
        timeout = self._cfg.get("heartbeat_timeout_s", 15.0)

        with self._lock:
            to_check = list(self._peers.items())

        for node_id, peer in to_check:
            if node_id == self._node_id:
                continue
            silent_s = now - peer.last_seen
            if silent_s > timeout:
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

    def _check_rebalance(self) -> None:
        """
        Return rescued cameras when their original owner comes back online.

        If peer X died and we rescued cam_01, cam_02, and then X restarts
        and reports cam_01, cam_02 in its active_cameras, we remove our
        rescued copies to avoid duplicate streams.
        """
        if not self._rescued_cameras:
            return

        now = time.time()
        timeout = self._cfg.get("heartbeat_timeout_s", 15.0)
        to_return: List[str] = []

        with self._lock:
            for camera_id, original_owner in list(self._rescued_cameras.items()):
                peer = self._peers.get(original_owner)
                if peer is None:
                    continue
                # Owner is back online AND is running this camera again
                if (now - peer.last_seen <= timeout
                        and camera_id in peer.active_cameras):
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
            )

        if state.overload_since is None:
            return
        if now - state.overload_since < cfg.get("overload_duration_s", 10.0):
            return

        load          = state.load_score
        thr3          = cfg.get("offload_level3_threshold", 65.0)
        thr2          = cfg.get("offload_level2_threshold", 75.0)
        thr1          = cfg.get("offload_level1_threshold", 85.0)
        level_cd      = cfg.get("offload_level_cooldown_s", 20.0)
        global_offload = cfg.get("offload_level", 0)

        # If offload is disabled in config, fall straight through to Level 1
        if global_offload == 0:
            self._trigger_level1_if_due(state, now, cfg)
            return

        cam_to_offload = self._pick_camera_to_offload(state)
        if not cam_to_offload:
            return

        # Check level-change cooldown for this camera
        last_change = self._offload_level_changed_at.get(cam_to_offload, 0.0)
        if now - last_change < level_cd:
            return

        current_level = self.get_offload_level(cam_to_offload)
        best_peer     = self._pick_best_peer()

        # Escalation ladder: 3 → 2 → 1
        if load >= thr1 and global_offload >= 1:
            # Full stream migration (Level 1) — existing RFO path
            logger.warning(
                "[PeerOrch] Load=%.1f ≥ L1 threshold=%.1f. Escalating to "
                "Level 1 stream migration for '%s'.",
                load, thr1, cam_to_offload,
            )
            self.set_offload_level(cam_to_offload, 0)   # clear fine-grained offload
            last_mig = self._cam_cooldown.get(cam_to_offload, 0.0)
            trigger  = "fps_drop" if (state.avg_fps and
                                      state.avg_fps < cfg.get("eps_fps_strict", 18.0)
                                      ) else "load_score"
            if now - last_mig >= cfg.get("cooldown_s", 45.0):
                logger.warning("[PeerOrch] RFO trigger: %s reason=%s", cam_to_offload, trigger)
                self._trigger_rfo(cam_to_offload, relaxation_tier=0)

        elif load >= thr2 and global_offload >= 2 and best_peer:
            if current_level != 2:
                logger.warning(
                    "[PeerOrch] Load=%.1f ≥ L2 threshold=%.1f. "
                    "Level 2 vehicle-crop offload for '%s' → '%s'.",
                    load, thr2, cam_to_offload, best_peer,
                )
                self.set_offload_level(cam_to_offload, 2, target_node=best_peer)

        elif load >= thr3 and global_offload >= 3 and best_peer:
            if current_level != 3:
                logger.warning(
                    "[PeerOrch] Load=%.1f ≥ L3 threshold=%.1f. "
                    "Level 3 plate-crop offload for '%s' → '%s'.",
                    load, thr3, cam_to_offload, best_peer,
                )
                self.set_offload_level(cam_to_offload, 3, target_node=best_peer)

        else:
            # Load dropped below all thresholds — clear fine-grained offload
            if current_level in (2, 3):
                logger.info(
                    "[PeerOrch] Load=%.1f below thresholds. Clearing offload for '%s'.",
                    load, cam_to_offload,
                )
                self.set_offload_level(cam_to_offload, 0)

    def _trigger_level1_if_due(self, state, now: float, cfg: dict) -> None:
        """Legacy Level-1 overload trigger (existing behaviour, unchanged).
        NOTE: `state` is already a consistent snapshot captured by _check_self_overload.
        """
        cam_to_offload = self._pick_camera_to_offload(state)
        if not cam_to_offload:
            return
        last_mig = self._cam_cooldown.get(cam_to_offload, 0.0)
        if now - last_mig < cfg.get("cooldown_s", 45.0):
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

    def _pick_best_peer(self) -> Optional[str]:
        """
        Return the node_id of the alive peer with the lowest load score,
        subject to not being at stream capacity and not being in cooldown.
        Returns None if no suitable peer is found.
        """
        now     = time.time()
        timeout = self._cfg.get("heartbeat_timeout_s", 15.0)
        best_id : Optional[str] = None
        best_load = float("inf")

        with self._lock:
            for nid, peer in self._peers.items():
                if nid == self._node_id:
                    continue
                if now - peer.last_seen > timeout:
                    continue
                if len(peer.active_cameras) >= peer.max_streams:
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
        Gửi Request for Offload (RFO) và mở vote window.

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

        payload = {
            "requester":      self._node_id,
            "camera_id":      camera_id,
            "load_score":     self._self_state.load_score,
            "avg_fps":        self._self_state.avg_fps,
            "eps_fps":        eps_fps,
            "eps_network_ms": eps_net,
            "tier":           relaxation_tier,
            "ts":             time.time(),
        }

        with self._lock:
            self._vote_windows[camera_id] = []

        self._pubs["vote_request"].put(msgpack.packb(payload, use_bin_type=True))
        logger.info("[PeerOrch] RFO sent for '%s' (tier=%d, eps_fps=%.1f, eps_net=%.0fms)",
                    camera_id, relaxation_tier, eps_fps, eps_net)

        # Timer đóng vote window
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
        Đóng vote window, chọn winner.

        Nếu không có proposal nào → escalate relaxation tier.
        Nếu max tier rồi vẫn không có → log CLUSTER_SATURATED.
        """
        with self._lock:
            proposals = self._vote_windows.pop(camera_id, [])
            self._vote_timers.pop(camera_id, None)

        if not proposals:
            if relaxation_tier < 2:
                logger.warning(
                    "[PeerOrch] Zero bids for '%s' (tier=%d). Relaxing ε...",
                    camera_id, relaxation_tier,
                )
                self._trigger_rfo(camera_id, relaxation_tier=relaxation_tier + 1)
            else:
                logger.error(
                    "[PeerOrch] CLUSTER_SATURATED: no peer can accept '%s'. "
                    "Continuing with current load.",
                    camera_id,
                )
            return

        # Winner = proposal có F(x) thấp nhất
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

        self._pubs["vote_decision"].put(msgpack.packb(decision, use_bin_type=True))
        self._cam_cooldown[camera_id] = time.time()
        logger.info(
            "[PeerOrch] Election won by '%s' for '%s' (score=%.1f, fps_pred=%.1f)",
            winner["bidder"], camera_id, winner["score"], winner.get("fps_predicted", 0),
        )

    # ------------------------------------------------------------------
    # Voting — Bidder side
    # ------------------------------------------------------------------

    def _on_vote_request(self, payload: dict) -> None:
        """
        Nhận RFO từ peer khác.
        Kiểm tra ε-constraints, nếu pass → gửi proposal.

        RTT measurement runs in a thread pool so we never block
        the Zenoh subscriber callback thread.
        """
        requester = payload.get("requester", "")
        if requester == self._node_id:
            return  # Bỏ qua RFO của chính mình

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

        # ε1 — Capacity constraint
        eps_streams_max = self._cfg.get("eps_streams_max", 4)
        if current_streams >= eps_streams_max:
            logger.info(
                "[PeerOrch] RFO rejected for '%s': ε1 (capacity) — "
                "current=%d, max=%d",
                camera_id, current_streams, eps_streams_max,
            )
            return

        # ε2 — FPS prediction
        # YAML parses bare integer keys as int; look up both int and str forms
        fps_model = self._cfg.get("fps_model", {})
        streams_after = current_streams + 1
        predicted_fps = fps_model.get(streams_after,
                        fps_model.get(str(streams_after), 0.0))
        if predicted_fps < eps_fps:
            logger.info(
                "[PeerOrch] RFO rejected for '%s': ε2 (FPS) — "
                "predicted=%.1f, required=%.1f",
                camera_id, predicted_fps, eps_fps,
            )
            return

        # ε3 — Network RTT to camera RTSP origin (blocking — safe here in thread pool)
        cam_uri = self._get_camera_uri(camera_id)
        if cam_uri is None:
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

    def _on_vote_proposal(self, payload: dict) -> None:
        """Thu thập proposals — chỉ requester mới xử lý."""
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
        Nhận kết quả bầu chọn.

        Nếu mình là winner → ADD camera.
        Nếu mình là requester → chờ ack rồi REMOVE.
        """
        winner    = payload.get("winner", "")
        camera_id = payload.get("camera_id", "")
        from_node = payload.get("from_node", "")

        if winner == self._node_id:
            # --- MÌNH THẮNG: ADD camera vào pipeline ---
            cam_config = payload.get("cam_config", {})
            if not cam_config:
                logger.error("[PeerOrch] Decision missing cam_config for '%s'", camera_id)
                return
            add_cmd = {**cam_config, "cmd": "ADD"}
            # BUG-13 fix: publish on the winner's control key, not our own.
            # When we are the winner, winner == self._node_id so
            # peers/control/{winner} == peers/control/{self._node_id} and the
            # pre-declared publisher is the right one.  When another node is
            # the winner the ADD must go to peers/control/{winner} — we cannot
            # reuse the local publisher for that, so use session.put() directly.
            winner_key = f"peers/control/{winner}"
            self._session.put(winner_key, msgpack.packb(add_cmd, use_bin_type=True))
            logger.info("[PeerOrch] ADD command sent to '%s' for '%s'", winner, camera_id)

        elif from_node == self._node_id:
            # --- MÌNH LÀ REQUESTER: chờ ack rồi REMOVE ---
            threading.Thread(
                target=self._wait_and_remove,
                args=(camera_id, winner),
                daemon=True,
            ).start()

    def _wait_and_remove(self, camera_id: str, winner_node: str) -> None:
        """
        Make-before-Break: chờ winner xác nhận PLAYING → REMOVE từ mình.

        Nếu timeout → rollback (đánh penalty winner node).
        """
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

        # Success — REMOVE từ mình
        remove_cmd = {"cmd": "REMOVE", "camera_id": camera_id}
        self._pubs["control"].put(msgpack.packb(remove_cmd, use_bin_type=True))
        logger.info(
            "[PeerOrch] REMOVE sent to self for '%s'. Migration complete.",
            camera_id,
        )

        # Update cooldown
        self._cam_cooldown[camera_id] = time.time()

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

        logger.info(
            "[PeerOrch] Migration DONE in %.0fms: '%s' → %s",
            elapsed_ms, camera_id, winner_node,
        )

    # ------------------------------------------------------------------
    # Vote ack
    # ------------------------------------------------------------------

    def _on_vote_ack(self, payload: dict) -> None:
        """Nhận ack rằng stream đã PLAYING trên winner node."""
        camera_id = payload.get("camera_id", "")
        if not camera_id:
            return
        with self._lock:
            event = self._pending_acks.get(camera_id)
        if event:
            event.set()
            logger.info("[PeerOrch] Ack received for '%s' — stream is PLAYING.", camera_id)

    # ------------------------------------------------------------------
    # Leaderless failover (Phase 5)
    # ------------------------------------------------------------------

    @staticmethod
    def _consistent_hash(camera_id: str, peer_ids: List[str]) -> str:
        """
        Deterministic hash: tất cả nodes dùng sorted(peer_ids) → cùng input → cùng output.
        """
        alive = sorted(peer_ids)
        key = int(hashlib.sha256(camera_id.encode()).hexdigest(), 16)
        return alive[key % len(alive)]

    def _leaderless_failover(self, dead_node_id: str, orphaned_cameras: List[str]) -> None:
        """
        Rescue orphaned cameras bằng consistent hash.

        Mỗi peer sống chạy độc lập → cùng kết quả hash.
        Winner thực thi ADD sau jitter (0-2s) để tránh race.
        Sau jitter, kiểm tra peers/status/+ xem camera đã được rescue chưa.
        """
        cfg = self._cfg
        now = time.time()
        timeout = cfg.get("heartbeat_timeout_s", 15.0)

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
                if current_streams + self_accepted >= self._cfg.get("eps_streams_max", 8):
                    logger.warning(
                        "[Failover] Cannot rescue '%s': at stream capacity (%d). Skipping.",
                        camera_id, current_streams + self_accepted,
                    )
                    continue

                # Double-check: camera đã được rescue bởi peer khác chưa?
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
        Đọc cấu hình camera từ cameras.yml.

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

    def _get_camera_uri(self, camera_id: str) -> Optional[str]:
        """Lấy RTSP URI của camera từ cameras.yml."""
        cfg = self._get_camera_config(camera_id)
        if cfg:
            return cfg.get("uri")
        return None

    def _pick_camera_to_offload(self, state: PeerState) -> Optional[str]:
        """
        Chọn camera để offload — ưu tiên camera có FPS cao nhất.

        Copy từ MasterOrchestrator._pick_camera_to_offload() (master_orchestrator.py:420).
        """
        if not state.active_cameras:
            return None
        if state.fps_per_camera:
            return max(
                (c for c in state.active_cameras if c in state.fps_per_camera),
                key=lambda c: state.fps_per_camera.get(c, 0),
                default=state.active_cameras[-1],
            )
        return state.active_cameras[-1]
