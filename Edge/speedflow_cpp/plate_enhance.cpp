/*
 * plate_enhance.cpp
 *
 * Implements sf_enhance_bgr_inplace and sf_estimate_motion_blur using
 * OpenCV C++ (no Python runtime, no GIL, no NumPy).
 *
 * This is the entire PlatePreprocessorProbe.preprocess_image() logic ported
 * to C++.  Calling it from Python via ctypes removes:
 *   - Python function-call overhead per vehicle crop
 *   - GIL hold during cv2.bilateralFilter (OpenCV releases GIL for long ops
 *     but NOT for the Python-side dispatch overhead)
 *   - NumPy array creation and reference-counting on every call
 *
 * The function operates on a pre-cropped BGR buffer already on CPU RAM
 * (obtained via pyds.get_nvds_buf_surface).  Pixels are modified in-place;
 * the caller is responsible for writing back to the NVMM surface.
 *
 * Compile: requires OpenCV 4.x headers and libopencv_core/imgproc.
 */

#include "speedflow.h"

#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>

/* -------------------------------------------------------------------------
 * Internal helpers
 * ------------------------------------------------------------------------- */

namespace {

/* Pre-allocated CLAHE handle — creating it per call is expensive */
static cv::Ptr<cv::CLAHE> g_clahe_low    = cv::createCLAHE(1.5, cv::Size(8, 8));
static cv::Ptr<cv::CLAHE> g_clahe_medium = cv::createCLAHE(2.0, cv::Size(8, 8));
static cv::Ptr<cv::CLAHE> g_clahe_high   = cv::createCLAHE(2.5, cv::Size(8, 8));

/* Sharpening kernels (same as Python PlatePreprocessorProbe) */
static const float K_LIGHT[9]  = {  0, -1,  0, -1,  5, -1,  0, -1,  0 };
static const float K_MEDIUM[9] = { -1, -1, -1, -1,  9, -1, -1, -1, -1 };
static const float K_STRONG[9] = { -1, -2, -1, -2, 13, -2, -1, -2, -1 };

} /* anonymous namespace */


/* -------------------------------------------------------------------------
 * sf_estimate_motion_blur
 *
 * Laplacian variance method — identical threshold to the Python original.
 * Returns 0 (low), 1 (medium), 2 (high).
 * ------------------------------------------------------------------------- */
int sf_estimate_motion_blur(const unsigned char *data, int width, int height)
{
    /* Wrap the raw buffer without copying */
    cv::Mat bgr(height, width, CV_8UC3, const_cast<unsigned char *>(data));

    cv::Mat gray;
    cv::cvtColor(bgr, gray, cv::COLOR_BGR2GRAY);

    cv::Mat lap;
    cv::Laplacian(gray, lap, CV_64F);

    cv::Scalar mean, stddev;
    cv::meanStdDev(lap, mean, stddev);
    double variance = stddev[0] * stddev[0];

    if (variance > 500.0) return 0;   /* low blur  */
    if (variance > 200.0) return 1;   /* medium    */
    return 2;                          /* high blur */
}


/* -------------------------------------------------------------------------
 * sf_enhance_bgr_inplace
 *
 * Applies bilateral filter → sharpening kernel → CLAHE in-place.
 * motion_level: 0=low, 1=medium, 2=high.
 * Returns 0 on success, -1 on error.
 * ------------------------------------------------------------------------- */
int sf_enhance_bgr_inplace(unsigned char *data, int width, int height,
                            int motion_level)
{
    if (!data || width <= 0 || height <= 0)
        return -1;

    try {
        /* Wrap buffer — zero-copy */
        cv::Mat img(height, width, CV_8UC3, data);

        /* ── Select parameters by blur level ─────────────────────────────── */
        int d, sigma;
        const float *kernel_data;
        cv::Ptr<cv::CLAHE> *clahe_ptr;

        switch (motion_level) {
        case 0:  /* low blur */
            d = 3; sigma = 30;
            kernel_data = K_LIGHT;
            clahe_ptr   = &g_clahe_low;
            break;
        case 2:  /* high blur */
            d = 7; sigma = 70;
            kernel_data = K_STRONG;
            clahe_ptr   = &g_clahe_high;
            break;
        default: /* medium (1) */
            d = 5; sigma = 50;
            kernel_data = K_MEDIUM;
            clahe_ptr   = &g_clahe_medium;
            break;
        }

        /* ── 1. Edge-preserving bilateral denoising ───────────────────────── */
        cv::Mat denoised;
        cv::bilateralFilter(img, denoised, d,
                            static_cast<double>(sigma),
                            static_cast<double>(sigma));

        /* ── 2. Adaptive sharpening (3×3 kernel convolution) ─────────────── */
        cv::Mat kernel(3, 3, CV_32F, const_cast<float *>(kernel_data));
        cv::Mat sharpened;
        cv::filter2D(denoised, sharpened, -1, kernel);

        /* ── 3. Contrast enhancement: CLAHE on L channel only ────────────── */
        cv::Mat lab;
        cv::cvtColor(sharpened, lab, cv::COLOR_BGR2Lab);

        std::vector<cv::Mat> channels;
        cv::split(lab, channels);                    /* channels[0] = L */
        (*clahe_ptr)->apply(channels[0], channels[0]);
        cv::merge(channels, lab);

        cv::Mat result;
        cv::cvtColor(lab, result, cv::COLOR_Lab2BGR);

        /* Write back to the caller's buffer in-place */
        std::memcpy(data, result.data,
                    static_cast<size_t>(width) * height * 3);
    }
    catch (const cv::Exception &ex) {
        /* OpenCV errors must not crash the GLib main loop */
        (void)ex;
        return -1;
    }

    return 0;
}
