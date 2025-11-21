/**
 * Buffer Probes for Metadata Extraction
 * Extracts speed, license plates, and other metadata
 */

#ifndef DS_PROBES_H
#define DS_PROBES_H

#include <gst/gst.h>
#include <string>
#include <map>
#include <deque>
#include <opencv2/opencv.hpp>

namespace deepstream {

/**
 * Homography-based speed calculator
 */
class SpeedCalculator {
public:
    SpeedCalculator(const cv::Mat& homography_matrix, float fps);

    /**
     * Update position and calculate speed
     * @param track_id Object tracking ID
     * @param cx Center X coordinate
     * @param bottom_y Bottom Y coordinate
     * @return Speed in km/h, or -1 if not enough data
     */
    float calculateSpeed(int track_id, float cx, float bottom_y);

    /**
     * Clear history for a track ID
     */
    void clearTrack(int track_id);

private:
    struct Position {
        float y_world;
        int frame_count;
    };

    cv::Mat homography_;
    float fps_;
    int window_size_;  // Number of frames for speed calculation
    
    std::map<int, std::deque<Position>> history_;
    std::map<int, int> track_age_;
};

/**
 * License Plate extractor from SGIE metadata
 */
class LicensePlateExtractor {
public:
    /**
     * Extract license plate text from object metadata
     * @param obj_meta NvDsObjectMeta pointer
     * @return License plate string, or empty if not found
     */
    static std::string extractPlate(void* obj_meta);

    /**
     * Decode LPRNet output to text
     * @param output Raw output from LPRNet
     * @param length Output length
     * @return Decoded license plate string
     */
    static std::string decodeLPROutput(const float* output, int length);
};

/**
 * Enhanced probe with speed and LPR
 */
class MetadataProbe {
public:
    MetadataProbe(const std::string& homography_file, float fps);

    /**
     * Probe callback for OSD sink pad
     */
    static GstPadProbeReturn callback(
        GstPad* pad, GstPadProbeInfo* info, gpointer u_data);

    /**
     * Build JSON metadata from frame
     */
    std::string buildJSON(void* batch_meta);

private:
    SpeedCalculator speed_calc_;
    float speed_limit_;  // km/h
};

} // namespace deepstream

#endif // DS_PROBES_H
