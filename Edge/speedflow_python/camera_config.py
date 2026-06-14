#!/usr/bin/env python3
# speedflow_python/camera_config.py
"""
CameraManager — Manage multi-camera configuration for Multi-Stream system.

Provides:
  - Read/parse cameras.yml file
  - Pre-compute Homography matrix for each camera
  - Fast lookup API by source_id
  - Low-latency watcher (inotify via watchdog) + 100ms debounce
  - REST API (FastAPI) for programmatic add/remove
  - Thread-safe delta queue → GLib.idle_add() to ensure GStreamer ops
    always run on GLib Main Loop thread.

Requirements:
    pip install watchdog fastapi uvicorn
"""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np
import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CameraConfig:
    """Complete configuration information for a camera."""
    camera_id: str
    source_id: int
    uri: str
    enabled: bool
    name: str
    fps: float
    speed_limit_kmh: float

    # Homography
    source_points: np.ndarray          # shape (4, 2) float32
    target_points: np.ndarray          # shape (4, 2) float32
    homo_matrix: np.ndarray            # shape (3, 3) float64 — pre-computed

    # ROI polygon (pixel coords) for ROI filter probe
    roi_polygon: np.ndarray            # shape (N, 2) int32

    # Output
    record: bool
    record_path: str

    # --- Derived speed validation params (from fps) ---
    @property
    def min_track_age_frames(self) -> int:
        return int(self.fps * 0.5)


@dataclass
class StreamDelta:
    """Changes detected between two config file reads."""
    to_add: List[CameraConfig] = field(default_factory=list)
    to_remove: List[int] = field(default_factory=list)   # list of source_id


# ---------------------------------------------------------------------------
# Config Parser
# ---------------------------------------------------------------------------

def _parse_cameras_yml(yml_path: Path) -> Dict[str, CameraConfig]:
    """Read cameras.yml, return dict camera_id -> CameraConfig."""
    with open(yml_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    cameras = raw.get("cameras", {})
    result: Dict[str, CameraConfig] = {}

    for cam_id, cfg in cameras.items():
        if cfg is None:
            continue

        src_pts_raw = cfg["homography"]["source_points"]
        tw = cfg["homography"]["target_width"]
        th = cfg["homography"]["target_height"]

        source_pts = np.array(src_pts_raw, dtype=np.float32)  # (4,2)
        target_pts = np.array(
            [[0, 0], [tw, 0], [tw, th], [0, th]], dtype=np.float32
        )
        # Pre-compute homography matrix when reading config
        homo_matrix, _ = cv2.findHomography(source_pts, target_pts)
        if homo_matrix is None:
            # Fallback: getPerspectiveTransform requires exactly 4 points
            homo_matrix = cv2.getPerspectiveTransform(source_pts, target_pts)

        roi_raw = cfg.get("roi_polygon", [])
        # roi_polygon: [x1,y1, x2,y2, x3,y3, x4,y4] → reshape to (N,2)
        roi_arr = np.array(roi_raw, dtype=np.int32).reshape(-1, 2)

        out_cfg = cfg.get("output", {})

        result[cam_id] = CameraConfig(
            camera_id=cam_id,
            source_id=int(cfg["source_id"]),
            uri=cfg["uri"],
            enabled=bool(cfg.get("enabled", True)),
            name=cfg.get("name", cam_id),
            fps=float(cfg.get("fps", 25.0)),
            speed_limit_kmh=float(cfg.get("speed_limit_kmh", 80.0)),
            source_points=source_pts,
            target_points=target_pts,
            homo_matrix=homo_matrix,
            roi_polygon=roi_arr,
            record=bool(out_cfg.get("record", False)),
            record_path=str(out_cfg.get("record_path", f"output/{cam_id}.mp4")),
        )

    return result


# ---------------------------------------------------------------------------
# Tiler layout helper
# ---------------------------------------------------------------------------

def compute_tiler_layout(num_streams: int) -> tuple[int, int]:
    """
    Compute optimal rows × cols for nvmultistreamtiler.
    Prefer square (or near-square) layout.
    """
    if num_streams <= 0:
        return 1, 1
    cols = math.ceil(math.sqrt(num_streams))
    rows = math.ceil(num_streams / cols)
    return rows, cols


# ---------------------------------------------------------------------------
# CameraManager
# ---------------------------------------------------------------------------

class CameraManager:
    """
    Manage the complete lifecycle of camera configuration.

    Usage:
        manager = CameraManager("configs/cameras.yml")
        manager.start(on_add_callback, on_remove_callback, glib_idle_add_fn)
        ...
        cfg = manager.get_config(source_id=0)
        ...
        manager.stop()
    """

    def __init__(self, yml_path: str | Path) -> None:
        self.yml_path = Path(yml_path).resolve()
        if not self.yml_path.exists():
            raise FileNotFoundError(f"Camera config not found: {self.yml_path}")

        # Current state: camera_id -> CameraConfig
        self._configs: Dict[str, CameraConfig] = {}
        # Fast lookup by source_id (immutable view, rebuild on reload)
        self._by_source_id: Dict[int, CameraConfig] = {}
        self._lock = threading.RLock()

        # Delta queue: [StreamDelta, ...] — thread-safe
        self._delta_q: queue.Queue[StreamDelta] = queue.Queue()

        # Callbacks (set when start() is called)
        self._on_add: Optional[Callable[[CameraConfig], None]] = None
        self._on_remove: Optional[Callable[[int], None]] = None
        self._glib_idle_add: Optional[Callable] = None

        # Control flags
        self._running = False
        self._watcher_thread: Optional[threading.Thread] = None
        self._processor_thread: Optional[threading.Thread] = None
        self._observer = None   # watchdog Observer

        # Initial load
        self._load_initial()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        on_add: Callable[[CameraConfig], None],
        on_remove: Callable[[int], None],
        glib_idle_add: Callable,
    ) -> None:
        """
        Start watcher and processor.

        BUG-11 fix: this method is now safe to call multiple times (e.g. on
        pipeline restart inside run_rtsp_push_mode).  On a re-call we update
        the callbacks and restart only the threads/observer that have stopped;
        the CameraManager's in-memory config state is preserved across calls.

        Args:
            on_add:         Called when a new stream needs to be added to GStreamer pipeline.
            on_remove:      Called when a stream (source_id) needs to be removed from pipeline.
            glib_idle_add:  GLib.idle_add function to ensure GStreamer ops
                            run on GLib Main Loop thread.
        """
        self._on_add = on_add
        self._on_remove = on_remove
        self._glib_idle_add = glib_idle_add
        self._running = True

        # Restart processor thread if it has exited
        if (self._processor_thread is None
                or not self._processor_thread.is_alive()):
            self._processor_thread = threading.Thread(
                target=self._processor_loop,
                name="CameraManager-Processor",
                daemon=True,
            )
            self._processor_thread.start()

        # Restart watchdog observer if it has stopped
        if self._observer is None or not self._observer.is_alive():
            self._start_watchdog()

        logger.info("[CameraManager] Started. Watching: %s", self.yml_path)

    def stop(self) -> None:
        """Stop watcher and processor."""
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join()
        # Unblock processor
        self._delta_q.put(None)  # type: ignore[arg-type]
        if self._processor_thread:
            self._processor_thread.join(timeout=3)
        logger.info("[CameraManager] Stopped.")

    def get_config(self, source_id: int) -> Optional[CameraConfig]:
        """Look up CameraConfig by source_id. Thread-safe."""
        with self._lock:
            return self._by_source_id.get(source_id)

    def handle_add_command(self, cmd: dict) -> bool:
        """
        Build a CameraConfig from an ADD command dict and enqueue it for
        dynamic addition to the running pipeline.

        Used by PeerOrchestrator's direct-dispatch path (when this node wins a
        migration) so the ADD is applied even if no ZenohCommandSubscriber is
        running.  Mirrors the config-building logic in
        ZenohCommandSubscriber._handle_add (without the Zenoh status/ack
        publishing — those belong to the subscriber path).

        cmd keys: camera_id, source_id, uri, homography{source_points,
        target_width, target_height}, roi_polygon, name, fps, speed_limit_kmh,
        output{record, record_path}.

        Returns True if the ADD was queued, False if it was a no-op
        (e.g. source_id already active).
        """
        cam_id    = cmd["camera_id"]
        source_id = int(cmd["source_id"])
        uri       = cmd["uri"]

        existing = self.get_config(source_id)
        if existing and existing.enabled:
            logger.warning(
                "[CameraManager] ADD ignored: source_id=%d ('%s') already active.",
                source_id, existing.camera_id,
            )
            return False

        homo_cfg = cmd["homography"]
        src_pts  = np.array(homo_cfg["source_points"], dtype=np.float32)
        tw       = int(homo_cfg["target_width"])
        th       = int(homo_cfg["target_height"])
        tgt_pts  = np.array([[0, 0], [tw, 0], [tw, th], [0, th]], dtype=np.float32)
        homo_mat, _ = cv2.findHomography(src_pts, tgt_pts)
        if homo_mat is None:
            homo_mat = cv2.getPerspectiveTransform(src_pts, tgt_pts)

        roi_raw = cmd.get("roi_polygon", [])
        roi_arr = (np.array(roi_raw, dtype=np.int32).reshape(-1, 2)
                   if roi_raw else np.zeros((0, 2), dtype=np.int32))

        out_cfg = cmd.get("output", {})

        cam_cfg = CameraConfig(
            camera_id=cam_id,
            source_id=source_id,
            uri=uri,
            enabled=True,
            name=cmd.get("name", cam_id),
            fps=float(cmd.get("fps", 25.0)),
            speed_limit_kmh=float(cmd.get("speed_limit_kmh", 80.0)),
            source_points=src_pts,
            target_points=tgt_pts,
            homo_matrix=homo_mat,
            roi_polygon=roi_arr,
            record=bool(out_cfg.get("record", False)),
            record_path=str(out_cfg.get("record_path", f"output/{cam_id}.mp4")),
        )

        with self._lock:
            self._configs[cam_id] = cam_cfg
            self._rebuild_lookup()

        self._delta_q.put(StreamDelta(to_add=[cam_cfg]))
        logger.info(
            "[CameraManager] ADD queued via handle_add_command: "
            "camera_id='%s', source_id=%d", cam_id, source_id,
        )
        return True

    def get_enabled_configs(self) -> List[CameraConfig]:
        """Return list of all enabled cameras."""
        with self._lock:
            return [c for c in self._configs.values() if c.enabled]

    def get_max_streams(self) -> int:
        """Read max_streams from yml file (cached on init)."""
        return self._max_streams

    def get_tiler_layout(self) -> tuple[int, int]:
        """rows, cols for nvmultistreamtiler based on enabled camera count."""
        n = len(self.get_enabled_configs())
        return compute_tiler_layout(n)

    # ------------------------------------------------------------------
    # Internal — Load & Diff
    # ------------------------------------------------------------------

    def _load_initial(self) -> None:
        """Initial load, no delta creation."""
        try:
            raw = yaml.safe_load(self.yml_path.read_text(encoding="utf-8"))
            self._max_streams = int(raw.get("max_streams", 4))
            new_configs = _parse_cameras_yml(self.yml_path)
            with self._lock:
                self._configs = new_configs
                self._rebuild_lookup()
            enabled = self.get_enabled_configs()
            logger.info(
                "[CameraManager] Loaded %d cameras (%d enabled): %s",
                len(new_configs),
                len(enabled),
                [c.camera_id for c in enabled],
            )
        except Exception as exc:
            logger.error("[CameraManager] Failed to load config: %s", exc)
            raise

    def _reload_and_diff(self) -> Optional[StreamDelta]:
        """
        Re-read YAML file, compare with current state.
        Return StreamDelta if changed, None otherwise.
        """
        try:
            new_configs = _parse_cameras_yml(self.yml_path)
        except Exception as exc:
            logger.warning("[CameraManager] Reload failed (skipped): %s", exc)
            return None

        with self._lock:
            old_enabled = {
                c.source_id: c
                for c in self._configs.values()
                if c.enabled
            }
            new_enabled = {
                c.source_id: c
                for c in new_configs.values()
                if c.enabled
            }

            to_add_ids = set(new_enabled) - set(old_enabled)
            to_remove_ids = set(old_enabled) - set(new_enabled)

            # Detect URI or config changes of running cameras
            # → remove then re-add to restart stream
            for sid in set(old_enabled) & set(new_enabled):
                old_c = old_enabled[sid]
                new_c = new_enabled[sid]
                if old_c.uri != new_c.uri or old_c.fps != new_c.fps:
                    logger.info(
                        "[CameraManager] source_id=%d config changed → restart", sid
                    )
                    to_remove_ids.add(sid)
                    to_add_ids.add(sid)

            # Commit new state
            self._configs = new_configs
            self._rebuild_lookup()

        if not to_add_ids and not to_remove_ids:
            return None

        delta = StreamDelta(
            to_add=[new_enabled[sid] for sid in to_add_ids if sid in new_enabled],
            to_remove=list(to_remove_ids),
        )
        logger.info(
            "[CameraManager] Delta detected — add: %s, remove: %s",
            [c.camera_id for c in delta.to_add],
            delta.to_remove,
        )
        return delta

    def _rebuild_lookup(self) -> None:
        """Rebuild _by_source_id from _configs. Call while holding lock."""
        self._by_source_id = {
            c.source_id: c
            for c in self._configs.values()
            if c.enabled
        }

    # ------------------------------------------------------------------
    # Internal — Watchdog (inotify)
    # ------------------------------------------------------------------

    def _start_watchdog(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            manager = self   # capture for closure

            class _Handler(FileSystemEventHandler):
                _debounce_timer: Optional[threading.Timer] = None
                _debounce_lock = threading.Lock()

                def on_modified(self, event):
                    if Path(event.src_path).resolve() != manager.yml_path:
                        return
                    with self._debounce_lock:
                        if self._debounce_timer:
                            self._debounce_timer.cancel()
                        # Debounce 100ms — avoid reading file while still writing
                        self._debounce_timer = threading.Timer(
                            0.1, manager._trigger_reload
                        )
                        self._debounce_timer.start()

            self._observer = Observer()
            self._observer.schedule(
                _Handler(), str(self.yml_path.parent), recursive=False
            )
            self._observer.start()
            logger.info("[CameraManager] inotify watcher active (debounce=100ms)")

        except ImportError:
            logger.warning(
                "[CameraManager] 'watchdog' not installed. "
                "Falling back to 1s polling. Run: pip install watchdog"
            )
            # Fallback: polling thread
            self._watcher_thread = threading.Thread(
                target=self._polling_loop,
                name="CameraManager-Poller",
                daemon=True,
            )
            self._watcher_thread.start()

    def _trigger_reload(self) -> None:
        """Called when file changes — compute delta and push to queue."""
        delta = self._reload_and_diff()
        if delta:
            self._delta_q.put(delta)

    def _polling_loop(self) -> None:
        """Fallback when watchdog unavailable: poll file every 1s."""
        last_mtime = self.yml_path.stat().st_mtime
        while self._running:
            time.sleep(1.0)
            try:
                mtime = self.yml_path.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    time.sleep(0.1)   # debounce
                    self._trigger_reload()
            except Exception as exc:
                logger.debug("[CameraManager] Polling error: %s", exc)

    # ------------------------------------------------------------------
    # Internal — Processor (consume delta_q)
    # ------------------------------------------------------------------

    def _processor_loop(self) -> None:
        """
        Consume StreamDelta from queue and schedule GStreamer ops
        via GLib.idle_add to ensure thread safety.
        """
        while self._running:
            try:
                delta = self._delta_q.get(timeout=5.0)
            except queue.Empty:
                continue

            if delta is None:   # stop signal
                break

            # Remove first, add second (avoid source_id conflict)
            for source_id in delta.to_remove:
                sid = source_id  # capture for lambda
                if self._glib_idle_add and self._on_remove:
                    self._glib_idle_add(self._on_remove, sid)
                    logger.info(
                        "[CameraManager] Scheduled REMOVE source_id=%d on GLib loop", sid
                    )

            # Wait one GLib cycle before add (prevent race condition)
            if delta.to_remove and delta.to_add:
                time.sleep(0.05)

            for cam_cfg in delta.to_add:
                cfg = cam_cfg  # capture for lambda
                if self._glib_idle_add and self._on_add:
                    self._glib_idle_add(self._on_add, cfg)
                    logger.info(
                        "[CameraManager] Scheduled ADD camera=%s source_id=%d on GLib loop",
                        cfg.camera_id, cfg.source_id,
                    )

    # ------------------------------------------------------------------
    # REST API (optional — Phase 3)
    # ------------------------------------------------------------------

    def start_rest_api(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        """
        Start REST API server (FastAPI + uvicorn) on separate thread.
        Endpoints:
          POST   /cameras/add    body: CameraConfig JSON
          DELETE /cameras/{camera_id}
          GET    /cameras        list all cameras
        """
        try:
            import uvicorn
            from fastapi import FastAPI

            app = FastAPI(title="CameraManager API")
            manager = self

            @app.get("/cameras")
            def list_cameras():
                with manager._lock:
                    return {
                        cam_id: {
                            "source_id": c.source_id,
                            "uri": c.uri,
                            "enabled": c.enabled,
                            "name": c.name,
                        }
                        for cam_id, c in manager._configs.items()
                    }

            @app.delete("/cameras/{camera_id}")
            def remove_camera(camera_id: str):
                with manager._lock:
                    cfg = manager._configs.get(camera_id)
                    if not cfg or not cfg.enabled:
                        return {"status": "not_running", "camera_id": camera_id}
                    # Disable and push delta
                    cfg.enabled = False
                    manager._rebuild_lookup()
                    delta = StreamDelta(to_remove=[cfg.source_id])
                manager._delta_q.put(delta)
                return {"status": "removing", "source_id": cfg.source_id}

            def _run():
                uvicorn.run(app, host=host, port=port, log_level="warning")

            api_thread = threading.Thread(
                target=_run, name="CameraManager-API", daemon=True
            )
            api_thread.start()
            logger.info("[CameraManager] REST API listening on %s:%d", host, port)

        except ImportError:
            logger.warning(
                "[CameraManager] FastAPI/uvicorn not installed. "
                "REST API disabled. Run: pip install fastapi uvicorn"
            )
