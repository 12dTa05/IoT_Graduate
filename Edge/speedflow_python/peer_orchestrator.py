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
    max_streams: int = 4
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
        "migration_time_ms", "result",
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
    ) -> None:
        row = [
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            from_node, to_node, camera_id,
            trigger_reason,
            round(trigger_load, 1),
            round(trigger_fps, 1) if trigger_fps is not None else "",
            round(migration_time_ms, 0),
            result,
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

        # Stop event for cleanly blocking start()
        self._stop_event = threading.Event()

        # Thread pool for blocking I/O (RTT measurement) off the Zenoh callback thread
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="PeerOrch-IO")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open Zenoh session, declare pubs/subs, start decision thread."""
        import zenoh

        self._session = make_session()
        logger.info("[PeerOrch] Zenoh session opened (peer mode).")

        # Declare publishers once
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

        self._decision_thread = threading.Thread(
            target=self._decision_loop,
            name=f"PeerDecision-{self._node_id}",
            daemon=True,
        )
        self._decision_thread.start()

        # Park — Zenoh peer mode needs no blocking loop
        self._stop_event.wait()

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
        """Vòng lặp chính — kiểm tra overload + OFFLINE peers."""
        while self._running:
            time.sleep(2.0)
            try:
                self._check_offline_peers()
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
            if now - peer.last_seen > timeout:
                if peer.active_cameras:
                    orphans = list(peer.active_cameras)
                    # Prevent re-triggering
                    with self._lock:
                        if node_id in self._peers:
                            self._peers[node_id].active_cameras = []
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
                # Peer is alive — clear the notified flag so we log again if it goes offline
                self._notified_offline.discard(node_id)

    def _check_self_overload(self) -> None:
        """
        Kiểm tra node này có quá tải không.
        Nếu overload kéo dài hơn overload_duration_s → publish RFO.
        """
        cfg = self._cfg
        now = time.time()
        state = self._self_state

        # Cooldown tổng thể: bỏ qua nếu vừa migration xong (30s global)
        # (cooldown per-camera được check trong _close_vote_window)

        if state.overload_since is None:
            return
        if now - state.overload_since < cfg.get("overload_duration_s", 10.0):
            return

        # Chọn camera để offload
        cam_to_offload = self._pick_camera_to_offload(state)
        if not cam_to_offload:
            return

        # Check per-camera cooldown
        last_mig = self._cam_cooldown.get(cam_to_offload, 0.0)
        if now - last_mig < cfg.get("cooldown_s", 45.0):
            return

        trigger_reason = "fps_drop" if (state.avg_fps and state.avg_fps < cfg.get("eps_fps_strict", 18.0)) else "load_score"
        logger.warning(
            "[PeerOrch] OVERLOADED (%.1f%%, FPS=%s). Triggering RFO for '%s' (reason: %s)",
            state.load_score, state.avg_fps, cam_to_offload, trigger_reason,
        )

        self._trigger_rfo(cam_to_offload, relaxation_tier=0)

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

        with self._lock:
            current_streams = len(self._self_state.active_cameras)
            self_load = self._self_state.load_score

        # ε1 — Capacity constraint
        if current_streams >= self._cfg.get("eps_streams_max", 4):
            return

        # ε2 — FPS prediction
        # YAML parses bare integer keys as int; look up both int and str forms
        fps_model = self._cfg.get("fps_model", {})
        streams_after = current_streams + 1
        predicted_fps = fps_model.get(streams_after,
                        fps_model.get(str(streams_after), 0.0))
        if predicted_fps < eps_fps:
            return

        # ε3 — Network RTT to camera RTSP origin (blocking — safe here in thread pool)
        cam_uri = self._get_camera_uri(camera_id)
        if cam_uri is None:
            return
        rtt_ms = self._measure_rtt(cam_uri)
        if rtt_ms is None or rtt_ms > eps_net_ms:
            return

        # ε4 — Per-camera cooldown
        last_mig = self._cam_cooldown.get(camera_id, 0.0)
        if time.time() - last_mig < self._cfg.get("cooldown_s", 45.0):
            return

        # ε5 — Penalty check
        now = time.time()
        with self._lock:
            peer_self = self._peers.get(self._node_id)
            if peer_self and now < peer_self.penalty_until:
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
            "[PeerOrch] Bid for '%s': score=%.1f, fps_pred=%.1f, rtt=%.0fms",
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
            self._pubs["control"].put(msgpack.packb(add_cmd, use_bin_type=True))
            logger.info("[PeerOrch] ADD command sent to self for '%s'", camera_id)

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
        trigger_load = self._self_state.load_score
        trigger_fps = self._self_state.avg_fps

        confirmed = event.wait(timeout=timeout)

        with self._lock:
            self._pending_acks.pop(camera_id, None)

        if not confirmed:
            # Timeout — rollback
            logger.error(
                "[PeerOrch] TIMEOUT (%ds) waiting for ack from '%s' for '%s'. Rolling back.",
                int(timeout), winner_node, camera_id,
            )
            with self._lock:
                if winner_node in self._peers:
                    self._peers[winner_node].penalty_until = time.time() + self._cfg.get("cooldown_s", 45.0) * 2
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
        self._migration_log.log(
            self._node_id, winner_node, camera_id,
            "overload", trigger_load, trigger_fps,
            elapsed_ms, "SUCCESS",
        )
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

        with self._lock:
            # Build alive candidate list — includes self so this node can rescue too
            self_streams = len(self._self_state.active_cameras)
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

        for camera_id in orphaned_cameras:
            if not alive_peers:
                logger.error("[Failover] No alive peers to rescue '%s'", camera_id)
                continue

            winner = self._consistent_hash(camera_id, alive_peers)

            if winner == self._node_id:
                # Jitter để tránh race
                jitter = random.uniform(0, cfg.get("failover_jitter_max_s", 2.0))
                time.sleep(jitter)

                # Double-check: camera đã được rescue bởi peer khác chưa?
                with self._lock:
                    already_handled = any(
                        camera_id in peer.active_cameras
                        for nid, peer in self._peers.items()
                        if nid != self._node_id
                    )
                if already_handled:
                    logger.info(
                        "[Failover] Camera '%s' already rescued. Skipping.",
                        camera_id,
                    )
                    continue

                cam_config = self._get_camera_config(camera_id)
                if cam_config is None:
                    logger.error("[Failover] No config for '%s'. Skipping.", camera_id)
                    continue

                add_cmd = {**cam_config, "cmd": "ADD"}
                self._pubs["control"].put(msgpack.packb(add_cmd, use_bin_type=True))
                logger.info("[Failover] Rescue ADD sent: '%s' → me", camera_id)

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
        Đo RTT đến camera RTSP origin bằng TCP connect.

        Args:
            rtsp_uri: e.g. "rtsp://192.168.1.155:8554/cam1"

        Returns:
            RTT in milliseconds, hoặc None nếu không reachable.
        """
        try:
            parsed = urllib.parse.urlparse(rtsp_uri)
            host = parsed.hostname
            port = parsed.port or 554
            t0 = time.monotonic()
            with socket.create_connection((host, port), timeout=0.1):
                pass
            return (time.monotonic() - t0) * 1000.0
        except Exception:
            return None

    def _get_camera_config(self, camera_id: str) -> Optional[dict]:
        """
        Đọc cấu hình camera từ cameras.yml.

        Copy từ MasterOrchestrator._get_camera_config() (master_orchestrator.py:604).
        """
        try:
            import yaml
            yml_path = self._camera_configs_dir / "cameras.yml"
            with open(yml_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            cameras = raw.get("cameras", {})
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
