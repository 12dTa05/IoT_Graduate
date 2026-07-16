# speedflow_python/probes.py
# -*- coding: utf-8 -*-
"""
GStreamer pad-probe callbacks.

Performance changes vs the original:
  - ROIFilterProbe._check_obj_in_roi  → sf.point_in_polygon  (C, no cv2 call)
  - SpeedProbe: homography applied as ONE batched call per camera per frame
    instead of one numpy array allocation + cv2.perspectiveTransform per object
  - _compute_speed_kmh     → sf.compute_speed_kmh     (C)
  - _valid_measurement_full → sf.valid_measurement     (C)
  - np.median on deque     → sf.median_speed           (C, no full sort)
  - _center_distance       → sf.center_distance        (C)
  - _calculate_plate_quality → sf.plate_quality        (C)
  - PlatePreprocessorProbe.preprocess_image → sf.enhance_bgr_inplace  (C/OpenCV)
"""

import base64
import json
import logging
import os
import queue
import time
import threading
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

import numpy as np
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
import pyds
import cv2

from . import speedflow_c as sf
from .settings import (
    VEHICLE_CLASS_IDS, SPEED_LOG,
    JPEG_QUALITY, SNAP_DIR, MAX_SNAPSHOT_PER_ID,
    MIN_WORLD_DISPL_M, MAX_ABS_KMH,
    BBOX_AREA_JUMP, MIN_DET_CONF, MEDIAN_WINDOW, LICENSE_PLATE_CLASS_IDS,
    FPS_STATS_FILE, NODE_ID,
)
from .draw import add_polygon_display
from .camera_config import CameraManager, CameraConfig

logger = logging.getLogger(__name__)


class CSVLogger:
    """Lightweight CSV appender — optional, non-critical path.

    Opens the header file lazily and holds a persistent append handle so we
    don't pay open/close cost on every row.  close() must be called on
    shutdown to flush and release the fd.
    """
    def __init__(self, path, header):
        self.path = path
        self.header = header
        self._f = None
        self._lock = threading.Lock()
        try:
            needs_header = not os.path.exists(path)
            # r+ would fail if missing; use a+ and seek to decide header
            self._f = open(path, "a", encoding="utf-8")
            if needs_header or os.path.getsize(path) == 0:
                self._f.write(",".join(header) + "\n")
                self._f.flush()
        except OSError as exc:
            logger.warning("[CSVLogger] cannot open %s: %s", path, exc)
            self._f = None

    def write(self, row):
        if self._f is None:
            return
        try:
            with self._lock:
                self._f.write(",".join(map(str, row)) + "\n")
                self._f.flush()
        except OSError as exc:
            logger.warning("[CSVLogger] write failed to %s: %s", self.path, exc)

    def close(self):
        if self._f is not None:
            try:
                self._f.flush()
                self._f.close()
            except OSError:
                pass
            self._f = None


# ---------------------------------------------------------------------------
# ROI filter probe
# ---------------------------------------------------------------------------

class ROIFilterProbe:
    """
    Filters NvDs objects that are outside their camera's ROI polygon.

    The point-in-polygon test is now handled by sf.point_in_polygon (C),
    removing both the cv2.pointPolygonTest overhead and the Python dispatch.
    """

    def __init__(self, camera_manager: CameraManager):
        self.camera_manager = camera_manager

    def analytics_src_pad_buffer_probe(self, pad, info, u_data):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list

        while l_frame:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            source_id  = frame_meta.source_id

            cam_cfg = self.camera_manager.get_config(source_id)
            if not cam_cfg:
                l_frame = l_frame.next
                continue

            roi = cam_cfg.roi_polygon          # (N,2) int32 ndarray or None
            # Fix #13: if no ROI is configured, keep all objects (no filter).
            if roi is None or len(roi) == 0:
                l_frame = l_frame.next
                continue
            objects_to_remove = []
            l_obj = frame_meta.obj_meta_list

            while l_obj:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)

                cx       = obj_meta.rect_params.left + obj_meta.rect_params.width  * 0.5
                bottom_y = obj_meta.rect_params.top  + obj_meta.rect_params.height

                # C extension — no cv2 import on the hot path
                if not sf.point_in_polygon(roi, cx, bottom_y):
                    objects_to_remove.append(obj_meta)

                l_obj = l_obj.next

            for obj_meta in objects_to_remove:
                pyds.nvds_remove_obj_meta_from_frame(frame_meta, obj_meta)

            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK


# ---------------------------------------------------------------------------
# Speed + LPR probe
# ---------------------------------------------------------------------------

class SpeedProbe:
    """
    Multi-stream speed measurement and license-plate tracking probe.

    State is keyed by (source_id, track_id) — one entry per tracked vehicle.

    Hot-path compute is delegated to the C extension (speedflow_c.py):
      • Perspective transform: one batched call per camera per frame
        instead of N individual calls with N NumPy array allocations.
      • Speed, validation, median, plate quality, center distance: all C.
    """

    def __init__(self, camera_manager: CameraManager, cooldown_s: float = 2.5,
                 peer_orch=None):
        self.camera_manager = camera_manager
        self._node_id       = NODE_ID
        # PeerOrchestrator reference — used to query offload level per camera.
        # None means all cameras run local inference (default / backward-compat).
        self._peer_orch = peer_orch

        # Per-track state  (stid = (source_id, track_id))
        self.history_positions  = defaultdict(deque)   # world-Y history
        self.last_speed_text    = defaultdict(str)
        self.last_update_frame  = defaultdict(lambda: -1000)
        self.last_alert_ts      = defaultdict(float)
        self.cooldown_s         = float(cooldown_s)
        self.snap_count         = defaultdict(int)
        self.speed_history      = defaultdict(lambda: deque(maxlen=MEDIAN_WINDOW))
        self.track_birth_frame  = {}
        self.last_area          = {}

        # Zenoh publisher (set externally via set_publisher)
        self.publisher     = None
        # OffloadPublisher for Level 2/3 crop sending (set via set_offload_publisher)
        self._offload_pub  = None

        try:
            os.makedirs(str(SNAP_DIR), exist_ok=True)
        except OSError as exc:
            logger.warning("[SpeedProbe] cannot create SNAP_DIR %s: %s", SNAP_DIR, exc)

        # License plate collection (20 raw frames ≈ 5 SGIE runs × 4 frames)
        self.PLATE_DETECTION_FRAMES      = 20
        self.plate_detection_start_frame = {}
        self.plate_candidates            = defaultdict(list)
        self.plate_locked                = {}
        self.plate_detection_attempts    = defaultdict(int)

        self.last_cleanup_time = time.time()

        # ── Step-3: warmup timing ──────────────────────────────────────────
        # Set once by run_python.py after pipeline.set_state(PLAYING).
        # health_agent reads it on the next heartbeat cycle.
        self._warmup_ms: Optional[float] = None

        # ── Step-7: Δτ measurement ─────────────────────────────────────────
        # Records wall-clock time of the FIRST valid speed measurement per
        # camera after it was added (used to compute Application Blind-spot).
        # Key: camera_id (str), Value: unix timestamp (float)
        self._first_valid_speed_ts: dict = {}

        # ── Offload result injector ────────────────────────────────────────
        # offload_receiver.py feeds decoded plate results into this queue.
        # Drained at the top of every osd_sink_pad_buffer_probe call so results
        # appear in the OSD overlay on the next frame after they arrive.
        # Queue is thread-safe; maxsize prevents unbounded growth if results
        # arrive faster than frames.
        self._offload_result_q: queue.Queue = queue.Queue(maxsize=256)

        # FPS counter — sliding 1-second window per camera_id
        self._fps_timestamps: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=300)
        )
        self._fps_stats_lock  = threading.Lock()
        self._fps_stats_cache: Dict[str, float] = {}

        # ── Proactive feature cache (per camera, updated every frame) ──────
        # Written on the GLib/GStreamer thread; read by the FPS writer thread
        # under _feature_lock.  Values are per-frame instantaneous counts that
        # the writer loop time-averages before flushing.
        #
        # Per-camera accumulators:
        #   n_track_sum         — sum of active vehicle tracks seen this window
        #   n_plate_sum         — sum of plate detections seen this window
        #   n_stationary_sum    — sum of stationary (≈stopped) vehicle counts
        #   frame_count         — number of frames accumulated since last flush
        #
        # "Stationary" threshold: speed_history median < STATIONARY_KMH_THRESH.
        # Any vehicle whose smoothed speed is below the threshold (or whose
        # speed history is empty, i.e., not yet computed) is counted as stopped.
        self._feature_lock    = threading.Lock()
        self._feature_acc: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"n_track_sum": 0.0, "n_plate_sum": 0.0,
                     "n_stationary_sum": 0.0, "frame_count": 0.0}
        )
        # Published snapshot — what the writer thread serialises to disk
        self._feature_cache: Dict[str, Dict[str, float]] = {}

        self._fps_writer_running = True
        self._fps_writer_thread  = threading.Thread(
            target=self._fps_writer_loop, name="FPSStatsWriter", daemon=True
        )
        self._fps_writer_thread.start()

    # ------------------------------------------------------------------
    # Publisher
    # ------------------------------------------------------------------

    def set_publisher(self, publisher) -> None:
        self.publisher = publisher

    def record_warmup_ms(self, warmup_ms: float) -> None:
        """
        Called by run_python.py immediately after pipeline.set_state(PLAYING).
        The value is forwarded to the health_agent on the next heartbeat so it
        appears in the peers/status heartbeat as 'warmup_ms' (paper §D_setup).
        """
        self._warmup_ms = warmup_ms

    def set_offload_publisher(self, pub) -> None:
        """
        Set the OffloadPublisher instance used for Level 2/3 crop sending.
        Called by run_python_mode after the orchestrator and publisher are ready.
        """
        self._offload_pub = pub

    def inject_offload_result(self, result: dict) -> None:
        """
        Thread-safe entry point for OffloadReceiver to push decoded plate text
        back into this probe.  Called from the offload receiver worker thread.
        result: {stid, camera_id, frame_no, plate_text, confidence, ts}
        """
        try:
            self._offload_result_q.put_nowait(result)
        except queue.Full:
            pass   # discard stale result — next frame will get a fresher one

    # ------------------------------------------------------------------
    # FPS counter
    # ------------------------------------------------------------------

    def _tick_fps(self, camera_id: str) -> None:
        now    = time.monotonic()
        dq     = self._fps_timestamps[camera_id]
        dq.append(now)
        cutoff = now - 1.0
        while dq and dq[0] < cutoff:
            dq.popleft()
        with self._fps_stats_lock:
            self._fps_stats_cache[camera_id] = float(len(dq))

    def get_fps_stats(self) -> Dict[str, float]:
        with self._fps_stats_lock:
            return dict(self._fps_stats_cache)

    # ------------------------------------------------------------------
    # Proactive feature counter
    # ------------------------------------------------------------------

    # Vehicles with smoothed speed below this are counted as stationary
    # (stopped at red light).  3 km/h tolerates GPS/homography noise.
    _STATIONARY_KMH_THRESH: float = 3.0

    def _tick_features(self, camera_id: str, source_id: int,
                       vehicles_in_frame: dict) -> None:
        """
        Accumulate per-frame feature counts for camera_id.

        Called once per frame inside osd_sink_pad_buffer_probe, after Pass 1
        (vehicles_in_frame is populated) and Pass 2 (plate counts available via
        plates_in_frame length — passed through n_plate argument).

        This method only handles vehicle-side counts; plate count is injected
        separately via _tick_features_plates so the call site stays clean.
        """
        n_track = len(vehicles_in_frame)
        n_stationary = 0
        for tid in vehicles_in_frame:
            stid = (source_id, tid)
            sh = self.speed_history.get(stid)
            if sh is None or len(sh) == 0:
                # No speed history yet (new track) — assume stopped
                n_stationary += 1
            else:
                smoothed = sf.median_speed(list(sh))
                if smoothed < self._STATIONARY_KMH_THRESH:
                    n_stationary += 1

        with self._feature_lock:
            acc = self._feature_acc[camera_id]
            acc["n_track_sum"]      += n_track
            acc["n_stationary_sum"] += n_stationary
            acc["frame_count"]      += 1.0

    def _tick_features_plates(self, camera_id: str, n_plate: int) -> None:
        """Add plate count for this frame (called after Pass 2)."""
        with self._feature_lock:
            self._feature_acc[camera_id]["n_plate_sum"] += n_plate

    def get_feature_stats(self) -> Dict[str, Dict[str, float]]:
        """Return the last-published per-camera feature snapshot."""
        with self._feature_lock:
            return dict(self._feature_cache)

    def _fps_writer_loop(self) -> None:
        while self._fps_writer_running:
            time.sleep(2.0)
            try:
                fps   = self.get_fps_stats()
                feats = self._flush_features()

                # Build unified payload: fps_per_camera + per-camera features
                out: Dict = {"_updated_at": time.time()}
                # FPS entries (bare floats, backward-compat)
                for cam_id, f in fps.items():
                    out[cam_id] = f
                # Feature entries nested under a "_features" key so readers
                # that only care about FPS are unaffected.
                out["_features"] = feats

                with open(FPS_STATS_FILE, "w") as f:
                    json.dump(out, f)
            except Exception as exc:
                logger.debug(
                    "[FPSWriter] write failed — retrying next cycle: %s",
                    exc,
                    exc_info=True,
                )

    def _flush_features(self) -> Dict[str, Dict[str, float]]:
        """
        Drain accumulators, compute per-camera averages, update cache.
        Returns a snapshot dict {camera_id: {n_track, n_plate, stationary_fraction}}.
        """
        snapshot: Dict[str, Dict[str, float]] = {}
        with self._feature_lock:
            for cam_id, acc in self._feature_acc.items():
                fc = acc["frame_count"]
                if fc > 0:
                    n_track  = acc["n_track_sum"]      / fc
                    n_plate  = acc["n_plate_sum"]       / fc
                    n_stat   = acc["n_stationary_sum"]  / fc
                    stat_frac = n_stat / max(1.0, n_track)
                else:
                    n_track = n_plate = stat_frac = 0.0
                snapshot[cam_id] = {
                    "n_track":             round(n_track,  2),
                    "n_plate":             round(n_plate,  2),
                    "stationary_fraction": round(stat_frac, 3),
                }
                # Reset accumulator for next window
                acc["n_track_sum"] = acc["n_plate_sum"] = \
                    acc["n_stationary_sum"] = acc["frame_count"] = 0.0
            self._feature_cache = dict(snapshot)
        return snapshot

    def stop_fps_writer(self) -> None:
        self._fps_writer_running = False
        if self._fps_writer_thread.is_alive():
            self._fps_writer_thread.join(timeout=2.5)

    def _get_frame_bgr_cached(self, gst_buffer, frame_meta, cache: dict):
        """Return CPU BGR frame for this batch/frame, copying GPU surface once."""
        key = (frame_meta.batch_id, frame_meta.frame_num)
        if key not in cache:
            cache[key] = self._frame_bgr_from_gst_buffer(gst_buffer, frame_meta)
        return cache[key]

    # ------------------------------------------------------------------
    # Plate helpers
    # ------------------------------------------------------------------

    def _select_best_plate_from_candidates(self, candidates):
        if not candidates:
            return None
        valid = [c for c in candidates if c.get("text")]
        if not valid:
            return None

        text_groups: Dict[str, list] = defaultdict(list)
        for c in valid:
            text_groups[c["text"]].append(c)

        best_text = max(text_groups, key=lambda t: len(text_groups[t]))
        best_entry = max(text_groups[best_text], key=lambda x: x.get("quality", 0))
        return best_text

    @staticmethod
    def _extract_lpr_text(obj_meta) -> Optional[str]:
        try:
            class_meta_list = obj_meta.classifier_meta_list
            while class_meta_list is not None:
                class_meta = pyds.NvDsClassifierMeta.cast(class_meta_list.data)
                if class_meta and class_meta.unique_component_id == 3:
                    label_info_list = class_meta.label_info_list
                    if label_info_list is not None:
                        label_info = pyds.NvDsLabelInfo.cast(label_info_list.data)
                        if label_info:
                            text = getattr(label_info, "result_label", None)
                            if text:
                                return text
                            text = getattr(label_info, "result_class_label", None)
                            if text:
                                return text
                class_meta_list = class_meta_list.next
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Vehicle association helper (plate → vehicle)
    # ------------------------------------------------------------------

    def _associate_plate_to_vehicle(
        self,
        plate_bbox: dict,
        vehicles_in_frame: dict,
    ) -> Optional[int]:
        """
        Returns the track_id of the closest vehicle whose bounding box
        contains the plate horizontally, or None if no match within 300 px.
        Uses sf.center_distance (C) instead of np.sqrt.
        """
        pl, pt, pw, ph = (plate_bbox["left"], plate_bbox["top"],
                          plate_bbox["width"], plate_bbox["height"])
        plate_cx = pl + pw * 0.5

        best_vid  = None
        best_dist = float("inf")

        for vid, vbox in vehicles_in_frame.items():
            vl, vt, vw, vh = (vbox["left"], vbox["top"],
                               vbox["width"], vbox["height"])
            dist = sf.center_distance(pl, pt, pw, ph, vl, vt, vw, vh)
            if dist < best_dist and dist < 300:
                h_tol   = vw * 0.5
                v_right = vl + vw
                if vl - h_tol <= plate_cx <= v_right + h_tol:
                    best_dist = dist
                    best_vid  = vid

        return best_vid

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _frame_bgr_from_gst_buffer(gst_buffer, frame_meta):
        surface = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
        img = np.array(surface, copy=True, order="C")
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        return img

    @staticmethod
    def _crop_bbox(image_bgr, obj_meta):
        h, w = image_bgr.shape[:2]
        x  = max(0, int(round(obj_meta.rect_params.left)))
        y  = max(0, int(round(obj_meta.rect_params.top)))
        x2 = min(w, x + max(1, int(round(obj_meta.rect_params.width))))
        y2 = min(h, y + max(1, int(round(obj_meta.rect_params.height))))
        if x >= x2 or y >= y2:
            return None
        return image_bgr[y:y2, x:x2]

    @staticmethod
    def _jpg_b64_and_bytes(image_bgr, quality=85):
        ok, buf = cv2.imencode(".jpg", image_bgr,
                               [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None, None
        b = buf.tobytes()
        return base64.b64encode(b).decode("ascii"), b

    def _maybe_publish_and_save(self, stid, cam_id, frame_iso_ts,
                                 speed_kmh, crop_bgr):
        now = time.time()
        image_b64 = None
        if crop_bgr is not None:
            image_b64, _ = self._jpg_b64_and_bytes(crop_bgr, JPEG_QUALITY)

        if self.publisher and (now - self.last_alert_ts[stid] >= self.cooldown_s):
            self.last_alert_ts[stid] = now
            license_plate = self.plate_locked.get(stid, None)
            payload = {
                "type":          "overspeed",
                "node_id":       self._node_id,
                "camera_id":     cam_id,
                "ts":            frame_iso_ts,
                "track_id":      int(stid[1]),
                "speed_kmh":     float(speed_kmh),
                "license_plate": license_plate,
                "image_b64":     image_b64,
                "dedup_key":     f"{int(stid[1])}_{frame_iso_ts}",
            }
            try:
                if hasattr(self.publisher, "put"):
                    self.publisher.put(payload)
                else:
                    self.publisher(payload)
            except Exception as exc:
                logger.warning("[SpeedProbe] overspeed publish failed: %s", exc)

        if self.snap_count[stid] < MAX_SNAPSHOT_PER_ID and image_b64 is not None:
            self.snap_count[stid] += 1

    # ------------------------------------------------------------------
    # Stale-track cleanup  (every 30 s)
    # ------------------------------------------------------------------

    def _periodic_cleanup(self, current_time: float, current_frame: int) -> None:
        if current_time - self.last_cleanup_time < 30.0:
            return
        self.last_cleanup_time = current_time

        all_stids: set = set()
        all_stids.update(self.history_positions.keys())
        all_stids.update(self.track_birth_frame.keys())
        all_stids.update(self.last_area.keys())
        all_stids.update(self.last_speed_text.keys())
        all_stids.update(self.plate_locked.keys())

        # BUG-5 fix: use last_update_frame age to detect stale tracks, not
        # last_alert_ts (which is only set on overspeed events and remains 0
        # for slow vehicles).  A track is stale if it hasn't produced a valid
        # speed reading for >60 s worth of frames AND its history deque is
        # empty (vehicle has left the scene).  Using history being non-empty
        # alone was wrong — the deque retains up to 1.5 s of old positions
        # even after the vehicle leaves the ROI.
        # We use a 60-second wall-clock staleness guard via last_alert_ts as
        # a secondary gate only when last_update_frame was never set (i.e.,
        # the track never produced a valid speed).
        max_fps_estimate = 30  # conservative upper bound
        stale_frame_age  = int(60 * max_fps_estimate)  # 60 s × 30 fps = 1800 frames
        stale_cutoff_ts  = current_time - 60.0

        stale_keys = []
        for stid in all_stids:
            last_frame = self.last_update_frame.get(stid, -1000)
            age_frames = current_frame - last_frame
            if age_frames < stale_frame_age:
                # Recently updated — keep
                continue
            # Old enough; also verify history is empty (vehicle gone)
            hist = self.history_positions.get(stid)
            if hist and len(hist) > 0:
                # Still has positional history: vehicle may still be in frame.
                # Only evict if the alert timestamp is also stale (>60 s).
                if self.last_alert_ts.get(stid, 0.0) > stale_cutoff_ts:
                    continue
            stale_keys.append(stid)

        for stid in stale_keys:
            self.history_positions.pop(stid, None)
            self.last_speed_text.pop(stid, None)
            self.last_update_frame.pop(stid, None)
            self.last_alert_ts.pop(stid, None)
            self.snap_count.pop(stid, None)
            self.speed_history.pop(stid, None)
            self.track_birth_frame.pop(stid, None)
            self.last_area.pop(stid, None)
            self.plate_detection_start_frame.pop(stid, None)
            self.plate_candidates.pop(stid, None)
            self.plate_locked.pop(stid, None)
            self.plate_detection_attempts.pop(stid, None)

    # ------------------------------------------------------------------
    # Main probe callback
    # ------------------------------------------------------------------

    def osd_sink_pad_buffer_probe(self, pad, info, u_data):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame    = batch_meta.frame_meta_list

        _NTP_UNIX_OFFSET_NS = 2_208_988_800 * 1_000_000_000
        _MIN_VALID_UNIX_NS  = 946_684_800   * 1_000_000_000

        # ── Drain offload results from peer receiver ───────────────────────
        # Must happen before the frame loop so results land in OSD this frame.
        while True:
            try:
                res    = self._offload_result_q.get_nowait()
                stid_r = tuple(res.get("stid", []))
                text_r = res.get("plate_text", "")
                if stid_r and text_r:
                    self.plate_locked[stid_r] = text_r
            except queue.Empty:
                break

        while l_frame:
            frame_meta   = pyds.NvDsFrameMeta.cast(l_frame.data)
            frame_number = frame_meta.frame_num
            source_id    = frame_meta.source_id
            frame_bgr_cache = {}

            cam_cfg = self.camera_manager.get_config(source_id)
            if not cam_cfg:
                l_frame = l_frame.next
                continue

            # ── Offload level for this camera ──────────────────────────────
            # 0 = local, 2 = vehicle crops → peer, 3 = plate crops → peer
            offload_level  = 0
            offload_target = ""
            if self._peer_orch is not None:
                offload_level  = self._peer_orch.get_offload_level(cam_cfg.camera_id)
                offload_target = self._peer_orch.get_offload_target(cam_cfg.camera_id)

            # ── Timestamp ─────────────────────────────────────────────────
            ts_ns = getattr(frame_meta, "ntp_timestamp", 0)
            if ts_ns and ts_ns > _NTP_UNIX_OFFSET_NS:
                unix_ns = ts_ns - _NTP_UNIX_OFFSET_NS
            else:
                unix_ns = int(time.time() * 1e9)
            if unix_ns < _MIN_VALID_UNIX_NS:
                unix_ns = int(time.time() * 1e9)
            ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(unix_ns / 1e9))

            self._tick_fps(cam_cfg.camera_id)

            # ── Pass 1: collect all vehicles and plates into flat lists ────
            # We accumulate vehicle bottom-center points for a SINGLE batched
            # perspective transform call — one C call instead of N Python calls.
            vehicles_in_frame: Dict[int, dict] = {}
            plates_in_frame = []

            # Working buffers for the batch transform (pre-allocated per frame)
            track_ids_ordered = []       # vehicle track IDs, same order as pts
            raw_pts = []                 # [cx, by] pairs — filled below

            l_obj = frame_meta.obj_meta_list
            while l_obj:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)

                if obj_meta.class_id in VEHICLE_CLASS_IDS:
                    tid = obj_meta.object_id
                    r   = obj_meta.rect_params
                    cx       = r.left + r.width  * 0.5
                    bottom_y = r.top  + r.height

                    vehicles_in_frame[tid] = {
                        "left": r.left, "top": r.top,
                        "width": r.width, "height": r.height,
                        "obj_meta": obj_meta,
                    }
                    track_ids_ordered.append(tid)
                    raw_pts.extend([cx, bottom_y])

                elif obj_meta.class_id in LICENSE_PLATE_CLASS_IDS:
                    obj_meta.text_params.display_text = "license_plate"
                    r = obj_meta.rect_params
                    plates_in_frame.append({
                        "bbox": {
                            "left": r.left, "top": r.top,
                            "width": r.width, "height": r.height,
                        },
                        "obj_meta": obj_meta,
                        "conf": getattr(obj_meta, "confidence", 0.0),
                    })

                l_obj = l_obj.next

            # ── Batch perspective transform (ONE C call for all vehicles) ──
            n_veh = len(track_ids_ordered)
            if n_veh > 0:
                in_arr   = np.array(raw_pts, dtype=np.float32).reshape(n_veh, 2)
                world_xy = sf.perspective_batch(cam_cfg.homo_matrix, in_arr)
                # world_xy[i] = (world_x, world_y) for track_ids_ordered[i]
                y_world_by_tid = {
                    track_ids_ordered[i]: float(world_xy[i, 1])
                    for i in range(n_veh)
                }
            else:
                y_world_by_tid = {}

            # ── Proactive feature tick (vehicle counts) ────────────────────
            # Called after Pass 1 so vehicles_in_frame is complete.
            # speed_history is still valid from previous frames — stationary
            # classification uses cached median, free of the batch-transform result.
            self._tick_features(cam_cfg.camera_id, source_id, vehicles_in_frame)

            # ── Pass 2: License plate accumulation or Level-3 offload ────────
            # Level 3: plate crops are sent to the peer; local accumulation skipped.
            if offload_level == 3 and self._offload_pub is not None and offload_target:
                for plate_info in plates_in_frame:
                    vehicle_id = self._associate_plate_to_vehicle(
                        plate_info["bbox"], vehicles_in_frame
                    )
                    if vehicle_id is None:
                        continue
                    stid = (source_id, vehicle_id)
                    if stid in self.plate_locked:
                        continue
                    try:
                        frame_bgr_off = self._get_frame_bgr_cached(
                            gst_buffer, frame_meta, frame_bgr_cache)
                        if frame_bgr_off is not None:
                            bb = plate_info["bbox"]
                            px = max(0, int(bb["left"]))
                            py = max(0, int(bb["top"]))
                            pw = max(1, int(bb["width"]))
                            ph = max(1, int(bb["height"]))
                            plate_crop = frame_bgr_off[py:py+ph, px:px+pw]
                            if plate_crop.size > 0:
                                self._offload_pub.put_plate(
                                    target_node=offload_target,
                                    stid=stid,
                                    camera_id=cam_cfg.camera_id,
                                    frame_no=frame_number,
                                    crop_bgr=plate_crop,
                                    confidence=plate_info.get("conf", 0.0),
                                )
                    except Exception as exc:
                        logger.debug(
                            "[SpeedProbe] L3 plate crop offload error for camera=%s frame=%s: %s",
                            cam_cfg.camera_id, frame_number, exc, exc_info=True,
                        )

            else:
                # Normal local plate accumulation
                for plate_info in plates_in_frame:
                    vehicle_id = self._associate_plate_to_vehicle(
                        plate_info["bbox"], vehicles_in_frame
                    )
                    if vehicle_id is None:
                        continue

                    stid = (source_id, vehicle_id)
                    if stid in self.plate_locked:
                        continue

                    if stid not in self.plate_detection_start_frame:
                        self.plate_detection_start_frame[stid] = frame_number

                    frames_in_window = (frame_number
                                        - self.plate_detection_start_frame[stid])

                    if frames_in_window < self.PLATE_DETECTION_FRAMES:
                        plate_text = self._extract_lpr_text(plate_info["obj_meta"])
                        if plate_text:
                            bb = plate_info["bbox"]
                            quality = sf.plate_quality(
                                bb["width"], bb["height"], plate_info["conf"]
                            )
                            self.plate_candidates[stid].append({
                                "text":    plate_text,
                                "conf":    plate_info["conf"],
                                "bbox":    bb,
                                "quality": quality,
                                "frame":   frame_number,
                            })
                    else:
                        best = self._select_best_plate_from_candidates(
                            self.plate_candidates[stid]
                        )
                        if best:
                            self.plate_locked[stid] = best
                        else:
                            self.plate_detection_attempts[stid] += 1
                            if self.plate_detection_attempts[stid] < 3:
                                self.plate_detection_start_frame[stid] = frame_number
                                self.plate_candidates[stid] = []
                            else:
                                self.plate_locked[stid] = None

            # ── Proactive feature tick (plate count) ───────────────────────
            # plates_in_frame is populated in Pass 1 regardless of offload level;
            # tick here so n_plate reflects the true detector output.
            self._tick_features_plates(cam_cfg.camera_id, len(plates_in_frame))

            # ── Pass 3: Speed & OSD display ───────────────────────────────
            fps_int = int(cam_cfg.fps)

            # Level 2: send full vehicle crops to peer for LPD/LPR, while the
            # origin node keeps local speed/event processing below.
            if offload_level == 2 and self._offload_pub is not None and offload_target:
                try:
                    frame_bgr_l2 = self._get_frame_bgr_cached(
                        gst_buffer, frame_meta, frame_bgr_cache)
                    if frame_bgr_l2 is not None:
                        for tid, veh_info in vehicles_in_frame.items():
                            stid = (source_id, tid)
                            veh_crop = self._crop_bbox(frame_bgr_l2, veh_info["obj_meta"])
                            if veh_crop is not None and veh_crop.size > 0:
                                self._offload_pub.put_vehicle(
                                    target_node=offload_target,
                                    stid=stid,
                                    camera_id=cam_cfg.camera_id,
                                    frame_no=frame_number,
                                    crop_bgr=veh_crop,
                                    bbox_world_y=y_world_by_tid.get(tid, 0.0),
                                )
                except Exception as exc:
                    logger.debug(
                        "[SpeedProbe] L2 vehicle crop offload error for camera=%s frame=%s: %s",
                        cam_cfg.camera_id, frame_number, exc, exc_info=True,
                    )
                # Fall through to Pass 3 so the origin node keeps speed history,
                # OSD, and overspeed events while the peer handles LPD/LPR.

            for tid, veh_info in vehicles_in_frame.items():
                stid     = (source_id, tid)
                obj_meta = veh_info["obj_meta"]

                y_world = y_world_by_tid.get(tid, None)
                if y_world is None:
                    # Edge case: track appeared but was not in the batch
                    continue

                hist = self.history_positions[stid]
                hist.append(y_world)

                # Trim history to ~1.5 s — O(1) with deque
                max_hist_len = int(cam_cfg.fps * 1.5)
                while len(hist) > max_hist_len:
                    hist.popleft()

                if stid not in self.track_birth_frame:
                    self.track_birth_frame[stid] = frame_number

                area_now  = max(1.0, obj_meta.rect_params.width) * \
                             max(1.0, obj_meta.rect_params.height)
                area_prev = self.last_area.get(stid, None)
                det_conf  = getattr(obj_meta, "confidence", -1.0)
                if det_conf is None:
                    det_conf = -1.0

                display_text = self.last_speed_text[stid] or f"#{tid}"

                if (len(hist) >= fps_int
                        and (frame_number - self.last_update_frame[stid]) >= fps_int):

                    hist_list = list(hist)
                    speed_kmh = sf.compute_speed_kmh(hist_list, cam_cfg.fps)

                    if speed_kmh is not None:
                        bbox_ratio = (area_now / area_prev
                                      if area_prev and area_prev > 0 else 0.0)
                        age_frames = frame_number - self.track_birth_frame.get(
                            stid, frame_number)

                        if sf.valid_measurement(
                            hist_list, speed_kmh,
                            age_frames, cam_cfg.min_track_age_frames,
                            MIN_WORLD_DISPL_M, MAX_ABS_KMH,
                            bbox_ratio, BBOX_AREA_JUMP,
                            det_conf, MIN_DET_CONF,
                        ):
                            sh = self.speed_history[stid]
                            sh.append(speed_kmh)

                            # C median — no numpy allocation
                            if len(sh) >= 3:
                                speed_smooth = sf.median_speed(list(sh))
                            else:
                                speed_smooth = speed_kmh

                            display_text                     = f"{int(speed_smooth)} km/h"
                            self.last_speed_text[stid]       = display_text
                            self.last_update_frame[stid]     = frame_number

                            # ── Step-7: Δτ — record first valid speed ──
                            if cam_cfg.camera_id not in self._first_valid_speed_ts:
                                self._first_valid_speed_ts[cam_cfg.camera_id] = time.time()

                            if speed_smooth >= cam_cfg.speed_limit_kmh:
                                crop = None
                                try:
                                    frame_bgr = self._get_frame_bgr_cached(
                                        gst_buffer, frame_meta, frame_bgr_cache)
                                    if frame_bgr is not None and frame_bgr.size > 0:
                                        crop = self._crop_bbox(frame_bgr, obj_meta)
                                except Exception as exc:
                                    logger.debug(
                                        "[SpeedProbe] Overspeed snapshot capture error for camera=%s frame=%s track=%s: %s",
                                        cam_cfg.camera_id, frame_number, tid, exc, exc_info=True,
                                    )
                                self._maybe_publish_and_save(
                                    stid, cam_cfg.camera_id,
                                    ts_iso, speed_smooth, crop,
                                )
                        else:
                            display_text               = ""
                            self.last_speed_text[stid] = display_text

                # ── OSD text assembly ──────────────────────────────────────
                speed_text = self.last_speed_text.get(stid, "")
                plate_text = ""
                if stid in self.plate_locked and self.plate_locked[stid]:
                    plate_text = self.plate_locked[stid]

                if speed_text and plate_text:
                    final_display = f"{speed_text}\n{plate_text}"
                elif speed_text:
                    final_display = speed_text
                elif plate_text:
                    final_display = plate_text
                else:
                    final_display = ""

                obj_meta.text_params.display_text          = final_display
                obj_meta.text_params.font_params.font_size  = 6
                obj_meta.text_params.font_params.font_name  = "Serif"
                self.last_area[stid] = area_now

            # ── ROI / homography overlay ───────────────────────────────────
            if cam_cfg.roi_polygon is not None and len(cam_cfg.roi_polygon) > 0:
                add_polygon_display(batch_meta, frame_meta,
                                    cam_cfg.roi_polygon,
                                    color=(1.0, 0.0, 0.0, 1.0))

            if cam_cfg.source_points is not None and len(cam_cfg.source_points) > 0:
                add_polygon_display(batch_meta, frame_meta,
                                    cam_cfg.source_points,
                                    color=(0.0, 1.0, 0.0, 1.0))

            # Fix #8: always use wall-clock time for the cleanup timer, not the
            # buffer NTP timestamp.  A replayed video file carries old NTP
            # timestamps that diverge wildly from wall clock, making the
            # cleanup interval fire every frame (or never).
            self._periodic_cleanup(time.time(), frame_number)
            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK
