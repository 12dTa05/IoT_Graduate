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
import os
import time
import threading
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple

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


class CSVLogger:
    """Lightweight CSV appender — optional, non-critical path."""
    def __init__(self, path, header):
        self.path = path
        self.header = header
        try:
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(",".join(header) + "\n")
        except Exception:
            pass

    def write(self, row):
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(",".join(map(str, row)) + "\n")
        except Exception:
            pass


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

    def __init__(self, camera_manager: CameraManager, cooldown_s: float = 2.5):
        self.camera_manager = camera_manager
        self._node_id       = NODE_ID

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
        self.publisher = None

        try:
            os.makedirs(str(SNAP_DIR), exist_ok=True)
        except Exception:
            pass

        # License plate collection (20 raw frames ≈ 5 SGIE runs × 4 frames)
        self.PLATE_DETECTION_FRAMES      = 20
        self.plate_detection_start_frame = {}
        self.plate_candidates            = defaultdict(list)
        self.plate_locked                = {}
        self.plate_detection_attempts    = defaultdict(int)

        self.last_cleanup_time = time.time()

        # FPS counter — sliding 1-second window per camera_id
        self._fps_timestamps: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=300)
        )
        self._fps_stats_lock  = threading.Lock()
        self._fps_stats_cache: Dict[str, float] = {}

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

    def _fps_writer_loop(self) -> None:
        while self._fps_writer_running:
            time.sleep(2.0)
            try:
                stats = self.get_fps_stats()
                stats["_updated_at"] = time.time()
                with open(FPS_STATS_FILE, "w") as f:
                    json.dump(stats, f)
            except Exception:
                pass

    def stop_fps_writer(self) -> None:
        self._fps_writer_running = False

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
            except Exception:
                pass

        if self.snap_count[stid] < MAX_SNAPSHOT_PER_ID and image_b64 is not None:
            self.snap_count[stid] += 1

    # ------------------------------------------------------------------
    # Stale-track cleanup  (every 30 s)
    # ------------------------------------------------------------------

    def _periodic_cleanup(self, current_time: float) -> None:
        if current_time - self.last_cleanup_time < 30.0:
            return
        self.last_cleanup_time = current_time

        all_stids: set = set()
        all_stids.update(self.history_positions.keys())
        all_stids.update(self.track_birth_frame.keys())
        all_stids.update(self.last_area.keys())
        all_stids.update(self.last_speed_text.keys())
        all_stids.update(self.plate_locked.keys())

        stale_cutoff = current_time - 60.0
        stale_keys = [
            stid for stid in all_stids
            if self.last_alert_ts.get(stid, 0.0) <= stale_cutoff
            and not (stid in self.history_positions
                     and len(self.history_positions[stid]) > 0)
        ]

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

        while l_frame:
            frame_meta   = pyds.NvDsFrameMeta.cast(l_frame.data)
            frame_number = frame_meta.frame_num
            source_id    = frame_meta.source_id

            cam_cfg = self.camera_manager.get_config(source_id)
            if not cam_cfg:
                l_frame = l_frame.next
                continue

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

            # ── Pass 2: License plate accumulation ────────────────────────
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

            # ── Pass 3: Speed & OSD display ───────────────────────────────
            fps_int = int(cam_cfg.fps)

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

                            if speed_smooth >= cam_cfg.speed_limit_kmh:
                                crop = None
                                try:
                                    frame_bgr = self._frame_bgr_from_gst_buffer(
                                        gst_buffer, frame_meta)
                                    if frame_bgr is not None and frame_bgr.size > 0:
                                        crop = self._crop_bbox(frame_bgr, obj_meta)
                                except Exception:
                                    pass
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

            self._periodic_cleanup(time.time())
            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK
