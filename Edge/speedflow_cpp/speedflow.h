/*
 * speedflow.h
 *
 * Pure-C API for all hot-path compute that was previously done in Python.
 * Compiled into speedflow_cpp.so and loaded via ctypes from speedflow_c.py.
 *
 * Design rules:
 *   - Only scalar C types and raw pointers cross the ABI.
 *   - No C++ types in the extern "C" surface.
 *   - All functions are stateless; Python owns all state (dicts, deques).
 *   - No heap allocation inside tight loops; callers pre-allocate buffers.
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/* -------------------------------------------------------------------------
 * 1.  Speed computation
 * ------------------------------------------------------------------------- */

/*
 * compute_speed_kmh
 *
 * Given an ordered history of world-Y positions (metres) and the camera FPS,
 * returns the speed in km/h, or -1.0 if there are fewer than (int)fps samples.
 *
 *   history   – pointer to float array, oldest position at index 0
 *   n         – number of valid entries in history
 *   fps       – camera frame rate (frames per second)
 */
float sf_compute_speed_kmh(const float *history, int n, float fps);

/*
 * valid_measurement
 *
 * Returns 1 if the speed measurement passes all quality gates, 0 otherwise.
 *
 *   history / n          – same as above (used for displacement check)
 *   speed_kmh            – raw speed estimate (km/h)
 *   age_frames           – how many frames this track has been alive
 *   min_age_frames       – minimum track age to accept (from cam config)
 *   min_world_displ_m    – minimum world displacement (m) required
 *   max_abs_kmh          – ceiling on accepted speed
 *   bbox_area_ratio      – area_end / area_start (jump detector)
 *   bbox_area_jump_thresh– threshold above which bbox jump is rejected
 *   det_conf             – detection confidence in [0,1]; negative = unknown
 *   min_det_conf         – minimum confidence to accept (ignored if det_conf<0)
 */
int sf_valid_measurement(const float *history, int n,
                         float speed_kmh,
                         int   age_frames,
                         int   min_age_frames,
                         float min_world_displ_m,
                         float max_abs_kmh,
                         float bbox_area_ratio,
                         float bbox_area_jump_thresh,
                         float det_conf,
                         float min_det_conf);

/*
 * median_speed
 *
 * Returns the median of an array of floats (in-place partial sort of a
 * private copy; caller's array is untouched).
 * n must be >= 1.
 */
float sf_median(const float *arr, int n);


/* -------------------------------------------------------------------------
 * 2.  Perspective transform (single point)
 *
 * Applies a 3×3 homography to one (x, y) point.
 * homo – row-major float64 [9] (same layout as cv2.findHomography output when
 *         cast to float64; caller must cast to float64 before passing).
 * out_x, out_y – output world coordinates.
 * ------------------------------------------------------------------------- */
void sf_perspective_point(const double *homo9,
                          float px, float py,
                          float *out_x, float *out_y);

/*
 * perspective_batch
 *
 * Apply the same homography to n (cx, bottom_y) pairs.
 *
 *   homo9       – 3×3 row-major float64
 *   in_pts      – interleaved [x0,y0, x1,y1, ...] float32, length 2*n
 *   out_pts     – pre-allocated output [x0,y0, x1,y1, ...] float32, length 2*n
 *   n           – number of points
 */
void sf_perspective_batch(const double *homo9,
                          const float  *in_pts,
                          float        *out_pts,
                          int           n);


/* -------------------------------------------------------------------------
 * 3.  ROI – point-in-polygon  (winding-number, integer polygon)
 * ------------------------------------------------------------------------- */

/*
 * point_in_polygon
 *
 * Returns 1 if (px, py) is inside or on the boundary of the polygon,
 * 0 otherwise.  Uses the even-odd (ray-casting) rule, same as OpenCV's
 * pointPolygonTest with measureDist=false.
 *
 *   poly_xy   – interleaved [x0,y0, x1,y1, ...] int32 polygon vertices
 *   n_verts   – number of vertices
 *   px, py    – query point (float)
 */
int sf_point_in_polygon(const int *poly_xy, int n_verts, float px, float py);


/* -------------------------------------------------------------------------
 * 4.  Plate quality score
 * ------------------------------------------------------------------------- */

/*
 * plate_quality
 *
 * Returns a composite quality score for a plate candidate.
 * Higher is better.  Formula mirrors _calculate_plate_quality in probes.py.
 *
 *   bbox_w, bbox_h   – plate bounding-box dimensions (pixels)
 *   confidence       – detection confidence in [0, 1]
 */
float sf_plate_quality(float bbox_w, float bbox_h, float confidence);


/* -------------------------------------------------------------------------
 * 5.  Center distance (for plate-to-vehicle association)
 * ------------------------------------------------------------------------- */

/*
 * center_distance
 *
 * Euclidean distance between centres of two axis-aligned boxes.
 * Each box is described by (left, top, width, height).
 */
float sf_center_distance(float l1, float t1, float w1, float h1,
                          float l2, float t2, float w2, float h2);


/* -------------------------------------------------------------------------
 * 6.  Image enhancement  (plate_enhance.cpp)
 *
 * All pixel operations that were previously done in Python/OpenCV:
 *   bilateral filter → sharpening kernel → CLAHE on L channel.
 *
 * The buffer format matches what pyds.get_nvds_buf_surface returns after
 * np.array(..., copy=True): packed uint8, shape [H, W, C] where C is 3 (BGR)
 * or 4 (BGRA).  We operate on BGR only; caller strips alpha if present.
 * ------------------------------------------------------------------------- */

/*
 * enhance_bgr_inplace
 *
 * Applies bilateral + sharpen + CLAHE to a BGR image buffer in-place.
 *
 *   data          – pointer to BGR pixel data (uint8, row-major, no padding)
 *   width, height – image dimensions
 *   motion_level  – 0 = low blur (light kernel), 1 = medium, 2 = high blur
 *
 * Returns 0 on success, -1 on error.
 */
int sf_enhance_bgr_inplace(unsigned char *data, int width, int height,
                            int motion_level);

/*
 * estimate_motion_blur
 *
 * Laplacian variance-based blur estimator.
 * Returns 0 (low), 1 (medium), or 2 (high).
 */
int sf_estimate_motion_blur(const unsigned char *data, int width, int height);


#ifdef __cplusplus
}
#endif
