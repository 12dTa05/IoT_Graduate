/**
 * Frame Serializer for Zenoh P2P
 * Compresses frames for efficient network transmission
 */

#ifndef FRAME_SERIALIZER_H
#define FRAME_SERIALIZER_H

#include <vector>
#include <cstdint>
#include <opencv2/opencv.hpp>

namespace zenoh {

/**
 * Frame compression and serialization utilities
 */
class FrameSerializer {
public:
    /**
     * Compress frame to JPEG
     * @param frame Input frame (BGR)
     * @param quality JPEG quality (0-100)
     * @return Compressed JPEG data
     */
    static std::vector<uint8_t> compressJPEG(const cv::Mat& frame, int quality = 70);

    /**
     * Decompress JPEG to frame
     * @param jpeg_data Compressed JPEG data
     * @return Decompressed frame (BGR)
     */
    static cv::Mat decompressJPEG(const std::vector<uint8_t>& jpeg_data);

    /**
     * Compress frame to H.264 (for video streaming)
     * @param frame Input frame
     * @return Compressed H.264 data
     */
    static std::vector<uint8_t> compressH264(const cv::Mat& frame);

    /**
     * Extract ROI from frame (for targeted offloading)
     * @param frame Full frame
     * @param x ROI x coordinate
     * @param y ROI y coordinate
     * @param width ROI width
     * @param height ROI height
     * @return Cropped ROI
     */
    static cv::Mat extractROI(const cv::Mat& frame, int x, int y, int width, int height);

    /**
     * Resize frame for faster transmission
     * @param frame Input frame
     * @param max_dimension Maximum width or height
     * @return Resized frame
     */
    static cv::Mat resizeFrame(const cv::Mat& frame, int max_dimension = 640);

    /**
     * Calculate compression ratio
     * @param original_size Original size in bytes
     * @param compressed_size Compressed size in bytes
     * @return Compression ratio
     */
    static float compressionRatio(size_t original_size, size_t compressed_size);
};

} // namespace zenoh

#endif // FRAME_SERIALIZER_H
