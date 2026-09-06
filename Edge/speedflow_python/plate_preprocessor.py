#!/usr/bin/env python3
# speedflow/plate_preprocessor.py
"""
License Plate Preprocessing Probe.

Improves plate detection and OCR accuracy by enhancing vehicle crop quality
*before* the buffer reaches SGIE1 (LPD).

All pixel arithmetic is delegated to speedflow_cpp.so (sf.enhance_bgr_inplace
and sf.estimate_motion_blur).  The Python side keeps only the GStreamer/pyds
loop and the BGR→RGBA writeback — no Python/OpenCV enhancement fallback.

If the native .so is not available, speedflow_c raises a RuntimeError at
import time with build instructions.

Enhancement pipeline:
  1. Bilateral filter  – edge-preserving denoise
  2. Sharpening kernel – adaptive to motion blur level
  3. CLAHE             – contrast enhancement on L channel of LAB
"""

import cv2
import numpy as np
import pyds
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from . import speedflow_c as sf


class PlatePreprocessorProbe:
    """Preprocessing probe attached BEFORE SGIE1 (License Plate Detector).

    Enhancement runs on vehicle bounding-box crops only, so CPU load scales
    with the number of detected vehicles — not with frame resolution.

    All pixel arithmetic is handled by sf.enhance_bgr_inplace (C/OpenCV)
    which eliminates GIL contention and per-call Python overhead.
    """

    VEHICLE_CLASS_IDS = {2, 3, 5, 7}

    def __init__(self, **kwargs) -> None:
        # ponytail: kwargs ignored — native .so is mandatory, no need for
        # enable_sharpening/enable_contrast/enable_denoise toggles.
        self.processed_count = 0

    # ------------------------------------------------------------------
    # GStreamer probe callback
    # ------------------------------------------------------------------

    def buffer_probe(self, pad, info, u_data):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        try:
            batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
            l_frame    = batch_meta.frame_meta_list

            while l_frame:
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)

                # Decode NVMM surface to CPU — one copy per frame
                n_frame    = pyds.get_nvds_buf_surface(
                    hash(gst_buffer), frame_meta.batch_id
                )
                try:
                    frame_copy = np.array(n_frame, copy=True, order="C")

                    # Convert to BGR for C enhancement functions
                    if frame_copy.ndim == 3 and frame_copy.shape[2] == 4:
                        frame_bgr = cv2.cvtColor(frame_copy, cv2.COLOR_RGBA2BGR)
                        is_rgba   = True
                    elif frame_copy.ndim == 2:
                        frame_bgr = cv2.cvtColor(frame_copy, cv2.COLOR_GRAY2BGR)
                        is_rgba   = False
                    else:
                        frame_bgr = frame_copy
                        is_rgba   = False

                    h_frame, w_frame = frame_bgr.shape[:2]
                    modified = False

                    l_obj = frame_meta.obj_meta_list
                    while l_obj:
                        obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)

                        if obj_meta.class_id in self.VEHICLE_CLASS_IDS:
                            crop, x, y, x2, y2 = self._crop_vehicle(
                                frame_bgr, obj_meta, w_frame, h_frame
                            )
                            if crop is not None and crop.size > 0:
                                # Enhance in-place via C extension
                                self.preprocess_image_inplace(crop)
                                # crop is a view; write the pixels back
                                frame_bgr[y:y2, x:x2] = crop
                                modified = True

                        l_obj = l_obj.next

                    # Write modified BGR frame back to NVMM surface
                    if modified:
                        if is_rgba:
                            enhanced_out = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGBA)
                        else:
                            enhanced_out = frame_bgr
                        np.copyto(n_frame, enhanced_out)

                # VERY IMPORTANT: release the NVMM lock even if crop/convert/put raises
                finally:
                    pyds.unmap_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)

                self.processed_count += 1
                l_frame = l_frame.next

        except Exception as e:
            msg = str(e)
            if "RGBA/RGB" not in msg:
                print(f"[PlatePreprocessorProbe] Error: {e}")

        return Gst.PadProbeReturn.OK

    # ------------------------------------------------------------------
    # Enhancement entry point
    # ------------------------------------------------------------------

    def preprocess_image_inplace(self, crop: np.ndarray,
                                  motion_level: str = "medium") -> None:
        """
        Enhance *crop* in-place.  crop must be (H, W, 3) BGR uint8 C-contiguous.

        Delegates entirely to sf.enhance_bgr_inplace (C++). Motion level is
        auto-detected when the argument is 'medium'.
        """
        if crop is None or crop.size == 0:
            return

        # Convert string level to int (0/1/2) via motion-blur estimation
        if motion_level == "medium":
            motion_int = sf.estimate_motion_blur(crop)   # 0/1/2
        else:
            motion_int = {"low": 0, "high": 2}.get(motion_level, 1)

        # Make contiguous if needed (should already be, but be safe)
        buf = np.ascontiguousarray(crop, dtype=np.uint8)
        sf.enhance_bgr_inplace(buf, motion_int)
        if buf.ctypes.data != crop.ctypes.data:
            np.copyto(crop, buf)

    # Kept for backward compatibility in test code that calls the old API
    def preprocess_image(self, image_bgr: np.ndarray,
                         motion_level: str = "medium") -> np.ndarray:
        """Wraps preprocess_image_inplace; returns the modified array."""
        out = np.ascontiguousarray(image_bgr, dtype=np.uint8)
        self.preprocess_image_inplace(out, motion_level)
        return out

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _crop_vehicle(frame_bgr, obj_meta, w_frame, h_frame):
        x  = max(0, int(round(obj_meta.rect_params.left)))
        y  = max(0, int(round(obj_meta.rect_params.top)))
        x2 = min(w_frame, x + max(1, int(round(obj_meta.rect_params.width))))
        y2 = min(h_frame, y + max(1, int(round(obj_meta.rect_params.height))))
        if x >= x2 or y >= y2:
            return None, 0, 0, 0, 0
        return frame_bgr[y:y2, x:x2], x, y, x2, y2
