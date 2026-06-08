# speedflow_python/speedflow_c.py
"""
ctypes bindings for speedflow_cpp.so.

Drop-in replacements for the Python functions previously scattered across
probes.py and plate_preprocessor.py.  All functions fall back gracefully to
pure-Python / NumPy implementations if the .so cannot be loaded, so the
pipeline still runs on a machine that has not built the C extension yet.

Load order for the .so:
  1. $SPEEDFLOW_CPP_SO  (env override)
  2. <this_file>/../../../speedflow_cpp/speedflow_cpp.so  (repo layout)
  3. LD_LIBRARY_PATH search via ctypes.util.find_library
"""

import ctypes
import ctypes.util
import math
import os
from pathlib import Path
from typing import Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Library load
# ---------------------------------------------------------------------------

def _find_so() -> Optional[str]:
    # Explicit env override
    env = os.environ.get("SPEEDFLOW_CPP_SO")
    if env and Path(env).exists():
        return env

    # Repo-relative default: Edge/speedflow_cpp/speedflow_cpp.so
    here = Path(__file__).resolve().parent          # speedflow_python/
    candidate = here.parent / "speedflow_cpp" / "speedflow_cpp.so"
    if candidate.exists():
        return str(candidate)

    # System search
    found = ctypes.util.find_library("speedflow_cpp")
    return found


_lib: Optional[ctypes.CDLL] = None
_lib_available = False

def _load_lib() -> None:
    global _lib, _lib_available
    path = _find_so()
    if path is None:
        return
    try:
        _lib = ctypes.CDLL(path)
        _declare_signatures()
        _lib_available = True
    except OSError as exc:
        import warnings
        warnings.warn(
            f"[speedflow_c] Could not load {path}: {exc}. "
            "Falling back to Python implementations.",
            RuntimeWarning,
            stacklevel=2,
        )


def _declare_signatures() -> None:
    """Tell ctypes the argument/return types for every exported symbol."""
    assert _lib is not None

    # sf_compute_speed_kmh(const float*, int, float) -> float
    _lib.sf_compute_speed_kmh.argtypes = [
        ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_float]
    _lib.sf_compute_speed_kmh.restype = ctypes.c_float

    # sf_valid_measurement(...) -> int
    _lib.sf_valid_measurement.argtypes = [
        ctypes.POINTER(ctypes.c_float), ctypes.c_int,   # history, n
        ctypes.c_float,                                   # speed_kmh
        ctypes.c_int,   ctypes.c_int,                    # age, min_age
        ctypes.c_float, ctypes.c_float,                  # min_displ, max_kmh
        ctypes.c_float, ctypes.c_float,                  # area_ratio, jump_thresh
        ctypes.c_float, ctypes.c_float,                  # det_conf, min_conf
    ]
    _lib.sf_valid_measurement.restype = ctypes.c_int

    # sf_median(const float*, int) -> float
    _lib.sf_median.argtypes = [
        ctypes.POINTER(ctypes.c_float), ctypes.c_int]
    _lib.sf_median.restype = ctypes.c_float

    # sf_perspective_batch(const double*, const float*, float*, int) -> void
    _lib.sf_perspective_batch.argtypes = [
        ctypes.POINTER(ctypes.c_double),   # homo9
        ctypes.POINTER(ctypes.c_float),    # in_pts
        ctypes.POINTER(ctypes.c_float),    # out_pts
        ctypes.c_int,                       # n
    ]
    _lib.sf_perspective_batch.restype = None

    # sf_perspective_point(const double*, float, float, float*, float*) -> void
    _lib.sf_perspective_point.argtypes = [
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_float, ctypes.c_float,
        ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
    ]
    _lib.sf_perspective_point.restype = None

    # sf_point_in_polygon(const int*, int, float, float) -> int
    _lib.sf_point_in_polygon.argtypes = [
        ctypes.POINTER(ctypes.c_int), ctypes.c_int,
        ctypes.c_float, ctypes.c_float,
    ]
    _lib.sf_point_in_polygon.restype = ctypes.c_int

    # sf_plate_quality(float, float, float) -> float
    _lib.sf_plate_quality.argtypes = [
        ctypes.c_float, ctypes.c_float, ctypes.c_float]
    _lib.sf_plate_quality.restype = ctypes.c_float

    # sf_center_distance(float×8) -> float
    _lib.sf_center_distance.argtypes = [
        ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
    ]
    _lib.sf_center_distance.restype = ctypes.c_float

    # sf_enhance_bgr_inplace(uint8*, int, int, int) -> int
    _lib.sf_enhance_bgr_inplace.argtypes = [
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_int, ctypes.c_int, ctypes.c_int]
    _lib.sf_enhance_bgr_inplace.restype = ctypes.c_int

    # sf_estimate_motion_blur(const uint8*, int, int) -> int
    _lib.sf_estimate_motion_blur.argtypes = [
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_int, ctypes.c_int]
    _lib.sf_estimate_motion_blur.restype = ctypes.c_int


_load_lib()


# ---------------------------------------------------------------------------
# Public API — thin wrappers with Python fallbacks
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """True if the C extension loaded successfully."""
    return _lib_available


# ── 1. Speed ────────────────────────────────────────────────────────────────

def compute_speed_kmh(history: Sequence[float], fps: float) -> Optional[float]:
    """
    Returns speed in km/h, or None if history is too short.
    history must have at least int(fps) samples.
    """
    n = len(history)
    if _lib_available:
        arr = (ctypes.c_float * n)(*history)
        result = _lib.sf_compute_speed_kmh(arr, n, ctypes.c_float(fps))
        return None if result < 0 else float(result)
    # Python fallback
    if n < int(fps) or n < 2:
        return None
    distance_m = abs(history[-1] - history[0])
    time_s = (n - 1) / fps
    if time_s <= 0:
        return 0.0
    return (distance_m / time_s) * 3.6


def valid_measurement(history: Sequence[float],
                      speed_kmh: float,
                      age_frames: int,
                      min_age_frames: int,
                      min_world_displ_m: float,
                      max_abs_kmh: float,
                      bbox_area_ratio: float,
                      bbox_area_jump_thresh: float,
                      det_conf: float,
                      min_det_conf: float) -> bool:
    n = len(history)
    if _lib_available:
        arr = (ctypes.c_float * n)(*history)
        return bool(_lib.sf_valid_measurement(
            arr, n,
            ctypes.c_float(speed_kmh),
            ctypes.c_int(age_frames), ctypes.c_int(min_age_frames),
            ctypes.c_float(min_world_displ_m), ctypes.c_float(max_abs_kmh),
            ctypes.c_float(bbox_area_ratio), ctypes.c_float(bbox_area_jump_thresh),
            ctypes.c_float(det_conf), ctypes.c_float(min_det_conf),
        ))
    # Python fallback
    if age_frames < min_age_frames:
        return False
    if n >= 2 and abs(history[-1] - history[0]) < min_world_displ_m:
        return False
    if speed_kmh <= 0 or speed_kmh > max_abs_kmh:
        return False
    if bbox_area_ratio > bbox_area_jump_thresh:
        return False
    if det_conf >= 0 and det_conf < min_det_conf:
        return False
    return True


def median_speed(values: Sequence[float]) -> float:
    """Median of a small float sequence (typically MEDIAN_WINDOW ≤ 15)."""
    n = len(values)
    if n == 0:
        return 0.0
    if _lib_available:
        arr = (ctypes.c_float * n)(*values)
        return float(_lib.sf_median(arr, n))
    # Python fallback
    return float(np.median(values))


# ── 2. Perspective transform ─────────────────────────────────────────────────

def perspective_batch(homo_matrix: np.ndarray,
                      points_xy: np.ndarray) -> np.ndarray:
    """
    Apply a 3×3 homography to N points.

    homo_matrix : (3, 3) float64 ndarray  (cv2.findHomography output)
    points_xy   : (N, 2) float32 ndarray  [[cx0, cy0], [cx1, cy1], ...]

    Returns (N, 2) float32 ndarray of world coordinates.
    """
    n = len(points_xy)
    if n == 0:
        return np.empty((0, 2), dtype=np.float32)

    if _lib_available:
        homo_c64 = np.ascontiguousarray(homo_matrix, dtype=np.float64).ravel()
        pts_f32  = np.ascontiguousarray(points_xy,   dtype=np.float32).ravel()
        out_f32  = np.empty(n * 2, dtype=np.float32)

        _lib.sf_perspective_batch(
            homo_c64.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            pts_f32.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            out_f32.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            ctypes.c_int(n),
        )
        return out_f32.reshape(n, 2)

    # Python / cv2 fallback
    import cv2
    pts = points_xy.reshape(n, 1, 2).astype(np.float32)
    result = cv2.perspectiveTransform(pts, homo_matrix)
    return result.reshape(n, 2).astype(np.float32)


def perspective_point(homo_matrix: np.ndarray,
                      px: float, py: float) -> Tuple[float, float]:
    """
    Apply a 3×3 homography to a single (px, py) point.
    Returns (world_x, world_y).
    """
    if _lib_available:
        homo_c64 = np.ascontiguousarray(homo_matrix, dtype=np.float64).ravel()
        out_x = ctypes.c_float(0.0)
        out_y = ctypes.c_float(0.0)
        _lib.sf_perspective_point(
            homo_c64.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_float(px), ctypes.c_float(py),
            ctypes.byref(out_x), ctypes.byref(out_y),
        )
        return float(out_x.value), float(out_y.value)

    # Fallback
    import cv2
    pts = np.array([[[px, py]]], dtype=np.float32)
    res = cv2.perspectiveTransform(pts, homo_matrix)
    return float(res[0][0][0]), float(res[0][0][1])


# ── 3. ROI ────────────────────────────────────────────────────────────────────

def point_in_polygon(poly_xy: np.ndarray, px: float, py: float) -> bool:
    """
    Returns True if (px, py) is inside the polygon.
    poly_xy: (N, 2) int32 ndarray of polygon vertices.
    """
    if poly_xy is None or len(poly_xy) == 0:
        return True   # no polygon → everything passes

    if _lib_available:
        poly_c = np.ascontiguousarray(poly_xy, dtype=np.int32).ravel()
        n_verts = len(poly_xy)
        return bool(_lib.sf_point_in_polygon(
            poly_c.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),
            ctypes.c_int(n_verts),
            ctypes.c_float(px), ctypes.c_float(py),
        ))

    # Python fallback — same even-odd ray-cast
    import cv2
    return cv2.pointPolygonTest(poly_xy, (px, py), False) >= 0


# ── 4. Plate quality ─────────────────────────────────────────────────────────

def plate_quality(bbox_w: float, bbox_h: float, confidence: float) -> float:
    if _lib_available:
        return float(_lib.sf_plate_quality(
            ctypes.c_float(bbox_w),
            ctypes.c_float(bbox_h),
            ctypes.c_float(confidence),
        ))
    # Python fallback
    conf_score  = confidence * 70.0
    area        = bbox_w * bbox_h
    area_score  = min(20.0, max(0.0, (area - 1778) / 5333 * 20))
    aspect      = bbox_w / max(1.0, bbox_h)
    ideal       = 2.5 if aspect >= 1.8 else 1.1
    aspect_score = max(0.0, 10.0 - abs(aspect - ideal) * 2.0)
    return conf_score + area_score + aspect_score


# ── 5. Center distance ────────────────────────────────────────────────────────

def center_distance(l1: float, t1: float, w1: float, h1: float,
                    l2: float, t2: float, w2: float, h2: float) -> float:
    if _lib_available:
        return float(_lib.sf_center_distance(
            ctypes.c_float(l1), ctypes.c_float(t1),
            ctypes.c_float(w1), ctypes.c_float(h1),
            ctypes.c_float(l2), ctypes.c_float(t2),
            ctypes.c_float(w2), ctypes.c_float(h2),
        ))
    # Python fallback
    cx1 = l1 + w1 * 0.5; cy1 = t1 + h1 * 0.5
    cx2 = l2 + w2 * 0.5; cy2 = t2 + h2 * 0.5
    return math.sqrt((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2)


# ── 6. Image enhancement ──────────────────────────────────────────────────────

def estimate_motion_blur(bgr: np.ndarray) -> int:
    """
    Returns 0 (low), 1 (medium), or 2 (high blur) for a BGR crop.
    """
    if bgr is None or bgr.size == 0:
        return 1
    h, w = bgr.shape[:2]
    if _lib_available:
        buf = np.ascontiguousarray(bgr, dtype=np.uint8)
        return int(_lib.sf_estimate_motion_blur(
            buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            ctypes.c_int(w), ctypes.c_int(h),
        ))
    # Python fallback
    import cv2
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    var  = cv2.Laplacian(gray, cv2.CV_64F).var()
    if var > 500:  return 0
    if var > 200:  return 1
    return 2


def enhance_bgr_inplace(bgr: np.ndarray, motion_level: int) -> np.ndarray:
    """
    Applies bilateral + sharpen + CLAHE to a BGR ndarray in-place.

    bgr          : (H, W, 3) uint8 C-contiguous BGR array.  MODIFIED in place.
    motion_level : 0=low, 1=medium, 2=high.

    Returns the same array (for convenience).
    """
    if bgr is None or bgr.size == 0:
        return bgr

    h, w = bgr.shape[:2]

    if _lib_available:
        buf = np.ascontiguousarray(bgr, dtype=np.uint8)
        ret = _lib.sf_enhance_bgr_inplace(
            buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            ctypes.c_int(w), ctypes.c_int(h),
            ctypes.c_int(motion_level),
        )
        if ret == 0:
            # Copy result back if the array wasn't already the same buffer
            if buf.ctypes.data != bgr.ctypes.data:
                np.copyto(bgr, buf)
        return bgr

    # Python / OpenCV fallback (original PlatePreprocessorProbe logic)
    import cv2
    if motion_level == 0:
        d, sigma, clip = 3, 30, 1.5
        kernel = np.array([[ 0,-1, 0],[-1, 5,-1],[ 0,-1, 0]], dtype=np.float32)
    elif motion_level == 2:
        d, sigma, clip = 7, 70, 2.5
        kernel = np.array([[-1,-2,-1],[-2,13,-2],[-1,-2,-1]], dtype=np.float32)
    else:
        d, sigma, clip = 5, 50, 2.0
        kernel = np.array([[-1,-1,-1],[-1, 9,-1],[-1,-1,-1]], dtype=np.float32)

    out = cv2.bilateralFilter(bgr, d, sigma, sigma)
    out = cv2.filter2D(out, -1, kernel)
    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
    l = clahe.apply(l)
    out = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    np.copyto(bgr, out)
    return bgr
