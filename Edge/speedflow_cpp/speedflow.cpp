/*
 * speedflow.cpp
 *
 * Implements all speed-compute, ROI, plate-quality, and geometry functions
 * declared in speedflow.h.  No OpenCV dependency — pure C math only.
 * plate_enhance.cpp (compiled separately) covers the OpenCV image ops.
 *
 * Build: see Makefile
 * Target: aarch64-linux-gnu (Jetson Orin), -O3 -march=armv8-a+simd
 */

#include "speedflow.h"

#include <algorithm>  // std::sort, std::nth_element
#include <cmath>
#include <cstring>

/* =========================================================================
 * 1.  Speed computation
 * ========================================================================= */

float sf_compute_speed_kmh(const float *history, int n, float fps)
{
    if (n < static_cast<int>(fps) || n < 2)
        return -1.0f;

    float distance_m = std::fabs(history[n - 1] - history[0]);
    float time_s     = static_cast<float>(n - 1) / fps;
    if (time_s <= 0.0f)
        return 0.0f;
    return (distance_m / time_s) * 3.6f;
}

int sf_valid_measurement(const float *history, int n,
                         float speed_kmh,
                         int   age_frames,
                         int   min_age_frames,
                         float min_world_displ_m,
                         float max_abs_kmh,
                         float bbox_area_ratio,
                         float bbox_area_jump_thresh,
                         float det_conf,
                         float min_det_conf)
{
    /* Track must be old enough */
    if (age_frames < min_age_frames)
        return 0;

    /* Must have moved at least min_world_displ_m */
    if (n >= 2) {
        float disp = std::fabs(history[n - 1] - history[0]);
        if (disp < min_world_displ_m)
            return 0;
    }

    /* Speed bounds */
    if (speed_kmh <= 0.0f || speed_kmh > max_abs_kmh)
        return 0;

    /* Bounding-box area jump (occlusion / track swap detector) */
    if (bbox_area_ratio > bbox_area_jump_thresh)
        return 0;

    /* Detection confidence gate (det_conf < 0 means unknown → skip check) */
    if (det_conf >= 0.0f && det_conf < min_det_conf)
        return 0;

    return 1;
}

float sf_median(const float *arr, int n)
{
    /* Stack-allocated scratch for small n (MEDIAN_WINDOW is typically <=15) */
    float scratch[64];
    int   use_heap = (n > 64);
    float *buf = use_heap ? new float[n] : scratch;

    std::memcpy(buf, arr, n * sizeof(float));

    /* nth_element: O(n) average, no full sort needed */
    float *mid = buf + n / 2;
    std::nth_element(buf, mid, buf + n);
    float result = *mid;

    /* For even n, take the average of the two middle elements */
    if (n % 2 == 0) {
        float *lo = std::max_element(buf, mid);
        result    = (*lo + result) * 0.5f;
    }

    if (use_heap) delete[] buf;
    return result;
}


/* =========================================================================
 * 2.  Perspective transform
 * ========================================================================= */

void sf_perspective_point(const double *homo9,
                          float px, float py,
                          float *out_x, float *out_y)
{
    double x = static_cast<double>(px);
    double y = static_cast<double>(py);

    /* Row-major: [h00 h01 h02 | h10 h11 h12 | h20 h21 h22] */
    double w = homo9[6] * x + homo9[7] * y + homo9[8];
    if (std::fabs(w) < 1e-12) w = 1e-12;

    *out_x = static_cast<float>((homo9[0] * x + homo9[1] * y + homo9[2]) / w);
    *out_y = static_cast<float>((homo9[3] * x + homo9[4] * y + homo9[5]) / w);
}

void sf_perspective_batch(const double *homo9,
                          const float  *in_pts,
                          float        *out_pts,
                          int           n)
{
    const double h00 = homo9[0], h01 = homo9[1], h02 = homo9[2];
    const double h10 = homo9[3], h11 = homo9[4], h12 = homo9[5];
    const double h20 = homo9[6], h21 = homo9[7], h22 = homo9[8];

    for (int i = 0; i < n; ++i) {
        double x = static_cast<double>(in_pts[2 * i]);
        double y = static_cast<double>(in_pts[2 * i + 1]);

        double w = h20 * x + h21 * y + h22;
        if (std::fabs(w) < 1e-12) w = 1e-12;

        out_pts[2 * i]     = static_cast<float>((h00 * x + h01 * y + h02) / w);
        out_pts[2 * i + 1] = static_cast<float>((h10 * x + h11 * y + h12) / w);
    }
}


/* =========================================================================
 * 3.  ROI – point-in-polygon  (ray-casting / even-odd rule)
 * ========================================================================= */

int sf_point_in_polygon(const int *poly_xy, int n_verts, float px, float py)
{
    if (n_verts < 3)
        return 0;   /* degenerate polygon — always outside */

    int inside = 0;
    int j = n_verts - 1;

    for (int i = 0; i < n_verts; ++i) {
        float xi = static_cast<float>(poly_xy[2 * i]);
        float yi = static_cast<float>(poly_xy[2 * i + 1]);
        float xj = static_cast<float>(poly_xy[2 * j]);
        float yj = static_cast<float>(poly_xy[2 * j + 1]);

        /* Ray from (px,py) towards +x crosses this edge? */
        bool cond1 = (yi > py) != (yj > py);
        bool cond2 = (px < (xj - xi) * (py - yi) / (yj - yi) + xi);

        if (cond1 && cond2)
            inside ^= 1;

        j = i;
    }
    return inside;
}


/* =========================================================================
 * 4.  Plate quality score
 * ========================================================================= */

float sf_plate_quality(float bbox_w, float bbox_h, float confidence)
{
    /* Mirror of _calculate_plate_quality in probes.py (scaled for 1280×720) */
    float conf_score = confidence * 70.0f;

    float area = bbox_w * bbox_h;
    float area_score = std::fminf(20.0f, std::fmaxf(0.0f,
                           (area - 1778.0f) / 5333.0f * 20.0f));

    float aspect = bbox_w / std::fmaxf(1.0f, bbox_h);
    float ideal_aspect = (aspect >= 1.8f) ? 2.5f : 1.1f;
    float aspect_score = std::fmaxf(0.0f,
                             10.0f - std::fabs(aspect - ideal_aspect) * 2.0f);

    return conf_score + area_score + aspect_score;
}


/* =========================================================================
 * 5.  Center distance
 * ========================================================================= */

float sf_center_distance(float l1, float t1, float w1, float h1,
                          float l2, float t2, float w2, float h2)
{
    float cx1 = l1 + w1 * 0.5f;
    float cy1 = t1 + h1 * 0.5f;
    float cx2 = l2 + w2 * 0.5f;
    float cy2 = t2 + h2 * 0.5f;
    float dx = cx1 - cx2;
    float dy = cy1 - cy2;
    return std::sqrt(dx * dx + dy * dy);
}
