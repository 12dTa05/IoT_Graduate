/**
 * Frame Serializer Implementation
 * Efficient frame compression for P2P transmission
 */

#include "frame_serializer.h"
#include <iostream>

namespace zenoh {

std::vector<uint8_t> FrameSerializer::compressJPEG(const cv::Mat& frame, int quality) {
    std::vector<uint8_t> buffer;
    
    if (frame.empty()) {
        std::cerr << "[FrameSerializer] Empty frame" << std::endl;
        return buffer;
    }
    
    // JPEG compression parameters
    std::vector<int> params = {
        cv::IMWRITE_JPEG_QUALITY, quality,
        cv::IMWRITE_JPEG_OPTIMIZE, 1
    };
    
    // Encode to JPEG
    bool success = cv::imencode(".jpg", frame, buffer, params);
    
    if (!success) {
        std::cerr << "[FrameSerializer] JPEG compression failed" << std::endl;
        return {};
    }
    
    // Calculate compression ratio
    size_t original_size = frame.total() * frame.elemSize();
    float ratio = compressionRatio(original_size, buffer.size());
    
    std::cout << "[FrameSerializer] Compressed " << original_size << " bytes to " 
              << buffer.size() << " bytes (ratio: " << ratio << "x)" << std::endl;
    
    return buffer;
}

cv::Mat FrameSerializer::decompressJPEG(const std::vector<uint8_t>& jpeg_data) {
    if (jpeg_data.empty()) {
        std::cerr << "[FrameSerializer] Empty JPEG data" << std::endl;
        return cv::Mat();
    }
    
    // Decode JPEG
    cv::Mat frame = cv::imdecode(jpeg_data, cv::IMREAD_COLOR);
    
    if (frame.empty()) {
        std::cerr << "[FrameSerializer] JPEG decompression failed" << std::endl;
    }
    
    return frame;
}

std::vector<uint8_t> FrameSerializer::compressH264(const cv::Mat& frame) {
    // TODO: Implement H.264 compression using hardware encoder
    // For now, use JPEG as fallback
    return compressJPEG(frame, 85);
}

cv::Mat FrameSerializer::extractROI(const cv::Mat& frame, int x, int y, int width, int height) {
    if (frame.empty()) {
        return cv::Mat();
    }
    
    // Clamp coordinates to frame bounds
    x = std::max(0, std::min(x, frame.cols - 1));
    y = std::max(0, std::min(y, frame.rows - 1));
    width = std::max(1, std::min(width, frame.cols - x));
    height = std::max(1, std::min(height, frame.rows - y));
    
    cv::Rect roi(x, y, width, height);
    return frame(roi).clone();
}

cv::Mat FrameSerializer::resizeFrame(const cv::Mat& frame, int max_dimension) {
    if (frame.empty()) {
        return cv::Mat();
    }
    
    int width = frame.cols;
    int height = frame.rows;
    
    // Check if resize needed
    if (width <= max_dimension && height <= max_dimension) {
        return frame.clone();
    }
    
    // Calculate new dimensions maintaining aspect ratio
    float scale;
    if (width > height) {
        scale = static_cast<float>(max_dimension) / width;
    } else {
        scale = static_cast<float>(max_dimension) / height;
    }
    
    int new_width = static_cast<int>(width * scale);
    int new_height = static_cast<int>(height * scale);
    
    cv::Mat resized;
    cv::resize(frame, resized, cv::Size(new_width, new_height), 0, 0, cv::INTER_LINEAR);
    
    std::cout << "[FrameSerializer] Resized from " << width << "x" << height 
              << " to " << new_width << "x" << new_height << std::endl;
    
    return resized;
}

float FrameSerializer::compressionRatio(size_t original_size, size_t compressed_size) {
    if (compressed_size == 0) {
        return 0.0f;
    }
    return static_cast<float>(original_size) / compressed_size;
}

} // namespace zenoh
