# speedflow_python/probes.py
# -*- coding: utf-8 -*-
import time, os, base64, json, threading
from collections import defaultdict, deque
import numpy as np
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
import pyds
import cv2

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
    """Nhẹ nhàng: ghi CSV nếu cần, không bắt buộc."""
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


class ROIFilterProbe:
    """
    Probe to filter out objects that are outside the ROI.
    Sử dụng thuật toán Point in Polygon của OpenCV thay cho nvdsanalytics
    để linh hoạt cho từng camera động.
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
            source_id = frame_meta.source_id
            
            cam_cfg = self.camera_manager.get_config(source_id)
            if not cam_cfg:
                l_frame = l_frame.next
                continue
                
            objects_to_remove = []
            l_obj = frame_meta.obj_meta_list
            while l_obj:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                
                # Check if object is in ROI using Python polygon test
                if not self._check_obj_in_roi(obj_meta, cam_cfg.roi_polygon):
                    objects_to_remove.append(obj_meta)
                
                l_obj = l_obj.next
            
            # Remove objects outside ROI
            for obj_meta in objects_to_remove:
                pyds.nvds_remove_obj_meta_from_frame(frame_meta, obj_meta)
            
            l_frame = l_frame.next
        
        return Gst.PadProbeReturn.OK
    
    def _check_obj_in_roi(self, obj_meta, roi_polygon: np.ndarray) -> bool:
        if roi_polygon is None or len(roi_polygon) == 0:
            return True
        cx = obj_meta.rect_params.left + obj_meta.rect_params.width / 2.0
        bottom_y = obj_meta.rect_params.top + obj_meta.rect_params.height
        dist = cv2.pointPolygonTest(roi_polygon, (cx, bottom_y), False)
        return dist >= 0


class SpeedProbe:
    """
    Hỗ trợ Multi-Stream:
    Phân tách state theo khoá `(source_id, track_id)`.
    Đọc cấu hình động từ CameraManager theo `source_id`.
    """
    def __init__(self, camera_manager: CameraManager, cooldown_s: float = 2.5):
        self.camera_manager = camera_manager

        # ID định danh node này — dùng trong payload Zenoh để Deduplication
        self._node_id = NODE_ID

        # Trạng thái theo key: stid = (source_id, track_id)
        # history_positions stores world-Y coordinates as a deque (O(1) popleft)
        # The maxlen is not fixed at init since it depends on per-camera FPS;
        # we use an unbounded deque and trim manually in the probe loop.
        self.history_positions = defaultdict(deque)
        self.last_speed_text   = defaultdict(str)
        self.last_update_frame = defaultdict(lambda: -1000)

        self.last_alert_ts     = defaultdict(float)
        self.cooldown_s        = float(cooldown_s)
        self.snap_count        = defaultdict(int)

        # publisher: ZenohPublisher object (có phương thức .put(data))
        # hoặc bất kỳ callable nào nhận dict (để tương thích ngược)
        self.publisher = None

        self.speed_history     = defaultdict(lambda: deque(maxlen=MEDIAN_WINDOW))
        self.track_birth_frame = {}
        self.last_area         = {}

        try:
            os.makedirs(str(SNAP_DIR), exist_ok=True)
        except Exception:
            pass

        # License plate tracking
        # SGIE (LPD) runs every (interval+1)=4 frames (interval=3 in config).
        # We want ~5 SGIE inference passes in the collection window, so:
        #   PLATE_DETECTION_FRAMES = 5 SGIE runs × 4 raw frames/run = 20 raw frames.
        # The old value of 5 raw frames gave only 1-2 SGIE runs — not enough
        # candidates to reliably select the best plate text.
        self.PLATE_DETECTION_FRAMES = 20   # raw frames; covers ~5 SGIE passes
        self.plate_detection_start_frame = {}
        self.plate_candidates = defaultdict(list)
        self.plate_locked = {}
        self.plate_detection_attempts = defaultdict(int)

        self.last_cleanup_time = time.time()

        # -----------------------------------------------------------------
        # FPS Counter — sliding window 1 giây per camera_id
        # Khoá: camera_id (str), Giá trị: deque của timestamps
        # Chỉ ghi từ GLib Main Loop thread → không cần lock
        # -----------------------------------------------------------------
        self._fps_timestamps: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=300)  # đủ chứa 10s × 30fps
        )
        self._fps_stats_lock = threading.Lock()   # bảo vệ đọc ngoài GLib thread
        self._fps_stats_cache: dict[str, float] = {}  # kết quả tính sẵn

        # Writer thread: định kỳ flush FPS stats ra file JSON cho health_agent
        self._fps_writer_running = True
        self._fps_writer_thread = threading.Thread(
            target=self._fps_writer_loop,
            name="FPSStatsWriter",
            daemon=True,
        )
        self._fps_writer_thread.start()

    def _bbox_area(self, obj_meta):
        w = max(1.0, obj_meta.rect_params.width)
        h = max(1.0, obj_meta.rect_params.height)
        return float(w * h)

    # ------------------------------------------------------------------
    # FPS Counter — gọi mỗi frame từ GLib Main Loop thread
    # ------------------------------------------------------------------

    def _tick_fps(self, camera_id: str) -> None:
        """
        Đánh dấu một frame đã được xử lý cho camera_id.
        Gọi một lần mỗi frame trong osd_sink_pad_buffer_probe.
        KHÔNG thread-safe với _fps_timestamps nhưng chỉ được gọi từ
        GLib Main Loop → an toàn.
        """
        now = time.monotonic()
        dq = self._fps_timestamps[camera_id]
        dq.append(now)
        # Chỉ giữ timestamps trong cửa sổ 1 giây
        cutoff = now - 1.0
        while dq and dq[0] < cutoff:
            dq.popleft()

        # Cập nhật cache — dùng lock vì writer thread đọc cache này
        with self._fps_stats_lock:
            self._fps_stats_cache[camera_id] = float(len(dq))

    def get_fps_stats(self) -> dict[str, float]:
        """Trả về dict {camera_id: fps} — thread-safe."""
        with self._fps_stats_lock:
            return dict(self._fps_stats_cache)

    def _fps_writer_loop(self) -> None:
        """
        Ghi FPS stats ra file JSON mỗi 2 giây.
        health_agent.py đọc file này để báo cáo lên Zenoh / Central Monitor.
        """
        while self._fps_writer_running:
            time.sleep(2.0)
            try:
                stats = self.get_fps_stats()
                stats["_updated_at"] = time.time()
                with open(FPS_STATS_FILE, "w") as f:
                    json.dump(stats, f)
            except Exception:
                pass  # Không crash nếu ghi file thất bại

    def stop_fps_writer(self) -> None:
        """Dừng FPS writer thread khi pipeline kết thúc."""
        self._fps_writer_running = False

    def _valid_measurement_full(self, stid, cam_cfg: CameraConfig, frame_no, hist, speed_kmh, area_start, area_end, det_conf):
        birth = self.track_birth_frame.get(stid, frame_no)
        age_frames = frame_no - birth
        if age_frames < cam_cfg.min_track_age_frames:
            return False

        if len(hist) >= 2:
            disp_m = abs(hist[-1] - hist[0])
            if disp_m < MIN_WORLD_DISPL_M:
                return False

        if speed_kmh <= 0 or speed_kmh > MAX_ABS_KMH:
            return False

        if area_start is not None and area_start > 0 and area_end / area_start > BBOX_AREA_JUMP:
            return False

        if det_conf is not None and det_conf < MIN_DET_CONF:
            return False

        return True

    def set_publisher(self, publisher):
        """
        Gán publisher cho SpeedProbe.

        publisher có thể là:
          - ZenohPublisher instance (có phương thức .put(data)) — khuyến nghị
          - Callable nhận dict (backward compat)
        """
        self.publisher = publisher

    def _select_best_plate_from_candidates(self, candidates):
        if not candidates:
            return None
        valid_candidates = [c for c in candidates if c.get('text')]
        if not valid_candidates:
            return None
        
        text_groups = defaultdict(list)
        for candidate in valid_candidates:
            text_groups[candidate['text']].append(candidate)
        
        text_frequencies = {text: len(entries) for text, entries in text_groups.items()}
        best_text = max(text_frequencies, key=text_frequencies.get)
        best_group = text_groups[best_text]
        best_entry = max(best_group, key=lambda x: x.get('quality', 0))
        return best_text

    @staticmethod
    def _extract_lpr_text(obj_meta):
        """Extract LPR text from NvDsClassifierMeta attached by SGIE2 (gie-unique-id=3).

        Tries result_label first (most DeepStream versions), then falls back to
        result_class_label which some older pyds bindings expose instead.
        """
        try:
            class_meta_list = obj_meta.classifier_meta_list
            while class_meta_list is not None:
                class_meta = pyds.NvDsClassifierMeta.cast(class_meta_list.data)
                if class_meta and class_meta.unique_component_id == 3:
                    label_info_list = class_meta.label_info_list
                    if label_info_list is not None:
                        label_info = pyds.NvDsLabelInfo.cast(label_info_list.data)
                        if label_info:
                            # Try result_label (DeepStream 6.x+)
                            text = getattr(label_info, 'result_label', None)
                            if text:
                                return text
                            # Fallback: result_class_label (older pyds)
                            text = getattr(label_info, 'result_class_label', None)
                            if text:
                                return text
                class_meta_list = class_meta_list.next
            return None
        except Exception:
            return None

    @staticmethod
    def _center_distance(box1, box2):
        cx1 = box1['left'] + box1['width'] / 2.0
        cy1 = box1['top'] + box1['height'] / 2.0
        cx2 = box2['left'] + box2['width'] / 2.0
        cy2 = box2['top'] + box2['height'] / 2.0
        return np.sqrt((cx1 - cx2)**2 + (cy1 - cy2)**2)

    def _associate_plate_to_vehicle(self, plate_bbox, vehicles_in_frame):
        best_vehicle_id = None
        min_distance = float('inf')
        
        for vid, vbox in vehicles_in_frame.items():
            dist = self._center_distance(plate_bbox, vbox)
            if dist < min_distance and dist < 300:
                plate_cx = plate_bbox['left'] + plate_bbox['width'] / 2.0
                v_left = vbox['left']
                v_right = vbox['left'] + vbox['width']
                h_tolerance = vbox['width'] * 0.5
                if v_left - h_tolerance <= plate_cx <= v_right + h_tolerance:
                    min_distance = dist
                    best_vehicle_id = vid
        return best_vehicle_id

    def _calculate_plate_quality(self, bbox, confidence):
        conf_score = confidence * 70.0
        area = bbox['width'] * bbox['height']
        area_score = min(20.0, max(0.0, (area - 4000) / 12000 * 20))
        aspect = bbox['width'] / max(1.0, bbox['height'])
        
        if aspect >= 1.8:
            ideal_aspect = 2.5
        else:
            ideal_aspect = 1.1
            
        aspect_diff = abs(aspect - ideal_aspect)
        aspect_score = max(0.0, 10.0 - aspect_diff * 2.0)
        return conf_score + area_score + aspect_score

    def _compute_speed_kmh(self, hist, fps):
        if len(hist) < int(fps):
            return None
        distance_m = abs(hist[-1] - hist[0])
        time_s = (len(hist) - 1) / float(fps)
        if time_s <= 0:
            return 0.0
        return (distance_m / time_s) * 3.6

    @staticmethod
    def _frame_bgr_from_gst_buffer(gst_buffer, frame_meta):
        surface = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
        img = np.array(surface, copy=True, order='C')
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        return img

    @staticmethod
    def _crop_bbox(image_bgr, obj_meta):
        h, w = image_bgr.shape[:2]
        x  = int(round(obj_meta.rect_params.left))
        y  = int(round(obj_meta.rect_params.top))
        bw = int(round(obj_meta.rect_params.width))
        bh = int(round(obj_meta.rect_params.height))
        x = max(0, x); y = max(0, y)
        x2 = min(w, x + max(1, bw))
        y2 = min(h, y + max(1, bh))
        if x >= x2 or y >= y2:
            return None
        return image_bgr[y:y2, x:x2]

    @staticmethod
    def _jpg_b64_and_bytes(image_bgr, quality=85):
        ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None, None
        b = buf.tobytes()
        return base64.b64encode(b).decode("ascii"), b

    def _maybe_publish_and_save(self, stid, cam_id, frame_iso_ts, speed_kmh, crop_bgr):
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
                # Deduplication key: dùng để lọc trùng trong giai đoạn
                # Make-before-Break khi 2 node cùng xử lý 1 camera.
                # Consumer chỉ cần lọc theo (track_id, ts) là đủ.
                "dedup_key":     f"{int(stid[1])}_{frame_iso_ts}",
            }
            try:
                # Hỗ trợ cả ZenohPublisher.put() và callable trực tiếp
                if hasattr(self.publisher, 'put'):
                    self.publisher.put(payload)   # non-blocking, ~0.1ms
                else:
                    self.publisher(payload)        # tương thích ngược
            except Exception:
                pass

        if self.snap_count[stid] < MAX_SNAPSHOT_PER_ID and image_b64 is not None:
            self.snap_count[stid] += 1

    def _periodic_cleanup(self, current_time: float):
        if current_time - self.last_cleanup_time < 30.0:  # Cleanup every 30s
            return
        self.last_cleanup_time = current_time

        # Collect ALL known stids from every tracking dict.
        all_stids = set()
        all_stids.update(self.history_positions.keys())
        all_stids.update(self.track_birth_frame.keys())
        all_stids.update(self.last_area.keys())
        all_stids.update(self.last_speed_text.keys())
        all_stids.update(self.plate_locked.keys())

        # Use last_alert_ts for recently alerted objects, otherwise
        # check if the object has any position history that was recently
        # active (proxy: if it has history entries, it was recently updated).
        # For objects never alerted, they're stale if not in current frame.
        # Since we don't have a per-stid "last seen" timestamp, use a
        # simple heuristic: purge stids with no history (already consumed)
        # or whose alert timestamp is old.
        stale_cutoff = current_time - 60.0
        # BUG-07: the last commit dropped the has_history guard, causing actively-
        # tracked vehicles (history accumulating, not yet alerted) to be purged
        # mid-track. Restore it: keep stids that still have position history,
        # regardless of whether they have triggered an alert recently.
        stale_keys = [
            stid for stid in all_stids
            if self.last_alert_ts.get(stid, 0.0) <= stale_cutoff
            and not (stid in self.history_positions and len(self.history_positions[stid]) > 0)
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

    def osd_sink_pad_buffer_probe(self, pad, info, u_data):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list

        while l_frame:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            frame_number = frame_meta.frame_num
            source_id = frame_meta.source_id

            cam_cfg = self.camera_manager.get_config(source_id)
            if not cam_cfg:
                l_frame = l_frame.next
                continue

            ts_ns = getattr(frame_meta, "ntp_timestamp", 0)
            # DeepStream ntp_timestamp is nanoseconds since the NTP epoch (Jan 1 1900).
            # Convert to Unix epoch (Jan 1 1970) by subtracting 70 years in seconds.
            # If the value is 0 or clearly invalid (before 2000-01-01 Unix), fall
            # back to the current wall-clock time.
            _NTP_UNIX_OFFSET_NS = 2_208_988_800 * 1_000_000_000  # 70 years in ns
            _MIN_VALID_UNIX_NS = 946_684_800 * 1_000_000_000     # 2000-01-01 in ns
            if ts_ns and ts_ns > _NTP_UNIX_OFFSET_NS:
                unix_ns = ts_ns - _NTP_UNIX_OFFSET_NS
            else:
                unix_ns = int(time.time() * 1e9)
            if unix_ns < _MIN_VALID_UNIX_NS:
                unix_ns = int(time.time() * 1e9)
            ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(unix_ns / 1e9))

            # Đếm FPS: đánh dấu một frame đã xử lý cho camera này
            self._tick_fps(cam_cfg.camera_id)

            vehicles_in_frame = {}
            plates_in_frame = []
            
            l_obj = frame_meta.obj_meta_list
            while l_obj:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)

                if obj_meta.class_id in VEHICLE_CLASS_IDS:
                    tid = obj_meta.object_id
                    vehicles_in_frame[tid] = {
                        'left': obj_meta.rect_params.left,
                        'top': obj_meta.rect_params.top,
                        'width': obj_meta.rect_params.width,
                        'height': obj_meta.rect_params.height,
                        'obj_meta': obj_meta
                    }
                elif obj_meta.class_id in LICENSE_PLATE_CLASS_IDS:
                    obj_meta.text_params.display_text = "license_plate"
                    plate_bbox = {
                        'left': obj_meta.rect_params.left,
                        'top': obj_meta.rect_params.top,
                        'width': obj_meta.rect_params.width,
                        'height': obj_meta.rect_params.height
                    }
                    plate_conf = getattr(obj_meta, "confidence", 0.0)
                    plates_in_frame.append({
                        'bbox': plate_bbox,
                        'obj_meta': obj_meta,
                        'conf': plate_conf
                    })
                l_obj = l_obj.next
            
            # Pass 2: License Plates
            for plate_info in plates_in_frame:
                vehicle_id = self._associate_plate_to_vehicle(plate_info['bbox'], vehicles_in_frame)
                if vehicle_id is not None:
                    stid = (source_id, vehicle_id)
                    if stid in self.plate_locked:
                        continue
                    
                    if stid not in self.plate_detection_start_frame:
                        self.plate_detection_start_frame[stid] = frame_number
                    
                    frames_in_window = frame_number - self.plate_detection_start_frame[stid]
                    
                    if frames_in_window < self.PLATE_DETECTION_FRAMES:
                        # Still inside the collection window — accumulate candidates
                        plate_text = self._extract_lpr_text(plate_info['obj_meta'])
                        if plate_text:
                            quality = self._calculate_plate_quality(plate_info['bbox'], plate_info['conf'])
                            self.plate_candidates[stid].append({
                                'text': plate_text,
                                'conf': plate_info['conf'],
                                'bbox': plate_info['bbox'],
                                'quality': quality,
                                'frame': frame_number
                            })
                    else:
                        # Window has closed (>= threshold).  Use >= not == so we never
                        # skip this branch when a detection frame is missed mid-window.
                        candidates = self.plate_candidates[stid]
                        best_plate_text = self._select_best_plate_from_candidates(candidates)
                        if best_plate_text:
                            self.plate_locked[stid] = best_plate_text
                        else:
                            self.plate_detection_attempts[stid] += 1
                            if self.plate_detection_attempts[stid] < 3:
                                # Retry: open a fresh window from now
                                self.plate_detection_start_frame[stid] = frame_number
                                self.plate_candidates[stid] = []
                            else:
                                # Give up after 3 attempts — mark as no plate
                                self.plate_locked[stid] = None
            
            # Pass 3: Speed & Display
            for tid, veh_info in vehicles_in_frame.items():
                stid = (source_id, tid)
                obj_meta = veh_info['obj_meta']
                
                # Transform to world coords using camera's homography matrix
                cx = obj_meta.rect_params.left + obj_meta.rect_params.width  / 2.0
                bottom_y = obj_meta.rect_params.top  + obj_meta.rect_params.height
                pts_src = np.array([[[cx, bottom_y]]], dtype=np.float32)
                pts_world = cv2.perspectiveTransform(pts_src, cam_cfg.homo_matrix)
                y_world = float(pts_world[0][0][1])

                hist = self.history_positions[stid]
                hist.append(y_world)
                
                # Maintain deque size based on FPS (~ 1.5 seconds max) — O(1) with deque
                max_hist_len = int(cam_cfg.fps * 1.5)
                while len(hist) > max_hist_len:
                    hist.popleft()

                if stid not in self.track_birth_frame:
                    self.track_birth_frame[stid] = frame_number

                area_now = self._bbox_area(obj_meta)
                area_prev = self.last_area.get(stid, None)
                det_conf = getattr(obj_meta, "confidence", None)

                display_text = self.last_speed_text[stid] or f"#{tid}"

                fps_int = int(cam_cfg.fps)
                if len(hist) >= fps_int and (frame_number - self.last_update_frame[stid] >= fps_int):
                    speed_kmh = self._compute_speed_kmh(hist, cam_cfg.fps)
                    
                    if self._valid_measurement_full(stid, cam_cfg, frame_number, hist, speed_kmh, area_prev, area_now, det_conf):
                        sh = self.speed_history[stid]
                        sh.append(speed_kmh)
                        speed_smooth = float(np.median(sh)) if len(sh) >= 3 else speed_kmh

                        display_text = f"{int(speed_smooth)} km/h"
                        self.last_speed_text[stid]   = display_text
                        self.last_update_frame[stid] = frame_number

                        if speed_smooth >= cam_cfg.speed_limit_kmh:
                            crop = None
                            try:
                                frame_bgr = self._frame_bgr_from_gst_buffer(gst_buffer, frame_meta)
                                if frame_bgr is not None and frame_bgr.size > 0:
                                    crop = self._crop_bbox(frame_bgr, obj_meta)
                            except Exception:
                                pass
                            self._maybe_publish_and_save(stid, cam_cfg.camera_id, ts_iso, speed_smooth, crop)
                    else:
                        display_text = ""
                        self.last_speed_text[stid] = display_text

                final_display = ""
                speed_text = self.last_speed_text.get(stid, "")
                plate_text = ""
                if stid in self.plate_locked:
                    locked_plate = self.plate_locked[stid]
                    if locked_plate:
                        plate_text = locked_plate
                
                if speed_text or plate_text:
                    if speed_text and plate_text:
                        final_display = f"{speed_text}\n{plate_text}"
                    elif speed_text:
                        final_display = speed_text
                    elif plate_text:
                        final_display = plate_text
                
                obj_meta.text_params.display_text = final_display
                self.last_area[stid] = area_now

            # Draw ROI box (Vùng giám sát - Red)
            if cam_cfg.roi_polygon is not None and len(cam_cfg.roi_polygon) > 0:
                add_polygon_display(batch_meta, frame_meta, cam_cfg.roi_polygon, color=(1.0, 0.0, 0.0, 1.0))

            # Draw Homography Source Points (Vùng đo tốc độ - Green)
            if cam_cfg.source_points is not None and len(cam_cfg.source_points) > 0:
                # Nếu source_points trùng hoàn toàn với roi_polygon, ta có thể vẽ lệch màu hoặc vẽ cả 2.
                # Ở đây vẽ màu xanh lá (Green)
                add_polygon_display(batch_meta, frame_meta, cam_cfg.source_points, color=(0.0, 1.0, 0.0, 1.0))

            # Periodic cleanup of stale tracking data
            self._periodic_cleanup(time.time())

            l_frame = l_frame.next
            
        return Gst.PadProbeReturn.OK