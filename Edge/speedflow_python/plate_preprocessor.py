#!/usr/bin/env python3
# speedflow/plate_preprocessor.py
"""
License Plate Preprocessing Probe.

Improves plate detection and OCR accuracy by enhancing vehicle crop quality
*before* the buffer reaches SGIE1 (LPD).

What changed vs the original:
  - preprocess_image and _estimate_motion_blur now delegate to sf.enhance_bgr_inplace
    and sf.estimate_motion_blur (C/OpenCV compiled in speedflow_cpp.so).
  - The Python side keeps only the GStreamer/pyds loop and the
    bgr→rgba writeback — all pixel arithmetic is in C.
  - Full Python / OpenCV fallback preserved for machines without the .so.

Enhancement pipeline (unchanged in semantics):
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
    """
    Preprocessing probe attached BEFORE SGIE1 (License Plate Detector).

    Enhancement runs on vehicle bounding-box crops only, so CPU load scales
    with the number of detected vehicles — not with frame resolution.

    When speedflow_cpp.so is available, sf.enhance_bgr_inplace is called
    instead of the Python OpenCV chain, eliminating GIL contention and
    per-call Python overhead.
    """

    VEHICLE_CLASS_IDS = {2, 3, 5, 7}

    def __init__(
        self,
        enable_sharpening: bool = True,
        enable_contrast:   bool = True,
        enable_denoise:    bool = True,
        adaptive_mode:     bool = True,
    ) -> None:
        self.enable_sharpening = enable_sharpening
        self.enable_contrast   = enable_contrast
        self.enable_denoise    = enable_denoise
        self.adaptive_mode     = adaptive_mode
        self.processed_count   = 0

        # Kernels kept for the Python fallback path only
        self._k_light  = np.array([[ 0,-1, 0],[-1, 5,-1],[ 0,-1, 0]], dtype=np.float32)
        self._k_medium = np.array([[-1,-1,-1],[-1, 9,-1],[-1,-1,-1]], dtype=np.float32)
        self._k_strong = np.array([[-1,-2,-1],[-2,13,-2],[-1,-2,-1]], dtype=np.float32)

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
                            # Enhance in-place via C extension (or Python fallback)
                            self.preprocess_image_inplace(crop)
                            # crop is a view; write the enhanced pixels back
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

                # VERY IMPORTANT: release the NVMM lock
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

        Uses sf.enhance_bgr_inplace (C) when the extension is available;
        falls back to the original Python/OpenCV chain otherwise.
        The motion level is auto-detected when adaptive_mode is True.
        """
        if crop is None or crop.size == 0:
            return

        if self.adaptive_mode and motion_level == "medium":
            motion_int = sf.estimate_motion_blur(crop)   # 0/1/2
        else:
            motion_int = {"low": 0, "medium": 1, "high": 2}.get(motion_level, 1)

        if sf.is_available():
            # Make contiguous if needed (should already be, but be safe)
            buf = np.ascontiguousarray(crop, dtype=np.uint8)
            sf.enhance_bgr_inplace(buf, motion_int)
            if buf.ctypes.data != crop.ctypes.data:
                np.copyto(crop, buf)
        else:
            self._enhance_python(crop, motion_int)

    # Kept for backward compatibility in test code that calls the old API
    def preprocess_image(self, image_bgr: np.ndarray,
                         motion_level: str = "medium") -> np.ndarray:
        """Wraps preprocess_image_inplace; returns the modified array."""
        out = np.ascontiguousarray(image_bgr, dtype=np.uint8)
        self.preprocess_image_inplace(out, motion_level)
        return out

    # ------------------------------------------------------------------
    # Python/OpenCV fallback enhancement  (used when .so not loaded)
    # ------------------------------------------------------------------

    def _enhance_python(self, crop: np.ndarray, motion_int: int) -> None:
        """Full Python/OpenCV pipeline — modifies crop in-place."""
        if motion_int == 0:
            d, sigma, clip = 3, 30, 1.5
            kernel = self._k_light
        elif motion_int == 2:
            d, sigma, clip = 7, 70, 2.5
            kernel = self._k_strong
        else:
            d, sigma, clip = 5, 50, 2.0
            kernel = self._k_medium

        out = crop
        if self.enable_denoise:
            out = cv2.bilateralFilter(out, d, sigma, sigma)
        if self.enable_sharpening:
            out = cv2.filter2D(out, -1, kernel)
        if self.enable_contrast:
            lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
            l = clahe.apply(l)
            out = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)

        np.copyto(crop, out)

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



