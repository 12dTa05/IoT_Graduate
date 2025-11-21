/**
 * Buffer Probes Implementation
 * Speed calculation via homography + License plate extraction
 */

#include "ds_probes.h"
#include "gstnvdsmeta.h"
#include "nvdsinfer.h"
#include <iostream>
#include <fstream>
#include <sstream>
#include <cmath>

namespace deepstream {

// ==================== SpeedCalculator ====================

SpeedCalculator::SpeedCalculator(const cv::Mat& homography_matrix, float fps)
    : homography_(homography_matrix)
    , fps_(fps)
    , window_size_(static_cast<int>(fps))  // 1 second window
{
}

float SpeedCalculator::calculateSpeed(int track_id, float cx, float bottom_y) {
    // Transform point to world coordinates
    std::vector<cv::Point2f> src_pts = {cv::Point2f(cx, bottom_y)};
    std::vector<cv::Point2f> dst_pts;
    cv::perspectiveTransform(src_pts, dst_pts, homography_);

    float y_world = dst_pts[0].y;

    // Update history
    auto& hist = history_[track_id];
    hist.push_back({y_world, track_age_[track_id]++});

    // Keep only window_size_ frames
    if (hist.size() > window_size_) {
        hist.pop_front();
    }

    // Need at least window_size_ frames for accurate calculation
    if (hist.size() < window_size_) {
        return -1.0f;
    }

    // Calculate distance traveled
    float distance_m = std::abs(hist.back().y_world - hist.front().y_world);
    
    // Calculate time
    float time_s = (hist.size() - 1) / fps_;

    if (time_s <= 0) {
        return -1.0f;
    }

    // Speed in m/s -> km/h
    float speed_ms = distance_m / time_s;
    float speed_kmh = speed_ms * 3.6f;

    // Sanity check (max 200 km/h)
    if (speed_kmh > 200.0f || speed_kmh < 0) {
        return -1.0f;
    }

    return speed_kmh;
}

void SpeedCalculator::clearTrack(int track_id) {
    history_.erase(track_id);
    track_age_.erase(track_id);
}

// ==================== LicensePlateExtractor ====================

std::string LicensePlateExtractor::extractPlate(void* obj_meta_ptr) {
    NvDsObjectMeta* obj_meta = static_cast<NvDsObjectMeta*>(obj_meta_ptr);

    // Iterate through classifier metadata (from SGIE)
    for (NvDsMetaList* l_class = obj_meta->classifier_meta_list;
         l_class != nullptr; l_class = l_class->next) {
        
        NvDsClassifierMeta* class_meta = (NvDsClassifierMeta*)(l_class->data);

        // Check if this is LPRNet output (unique_component_id should match SGIE)
        if (class_meta->unique_component_id == 2) {  // SGIE ID
            for (NvDsMetaList* l_label = class_meta->label_info_list;
                 l_label != nullptr; l_label = l_label->next) {
                
                NvDsLabelInfo* label_info = (NvDsLabelInfo*)(l_label->data);
                
                if (label_info->result_label && strlen(label_info->result_label) > 0) {
                    return std::string(label_info->result_label);
                }
            }
        }
    }

    return "";  // No plate found
}

std::string LicensePlateExtractor::decodeLPROutput(const float* output, int length) {
    // LPRNet uses CTC decoding
    // Character set: 0-9, A-Z, and special characters
    const char* charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-";
    const int charset_size = strlen(charset);

    std::string result;
    int prev_char = -1;

    for (int i = 0; i < length; i++) {
        // Find max probability character
        int max_idx = 0;
        float max_prob = output[i * charset_size];
        
        for (int j = 1; j < charset_size; j++) {
            if (output[i * charset_size + j] > max_prob) {
                max_prob = output[i * charset_size + j];
                max_idx = j;
            }
        }

        // CTC decoding: skip blanks and repeated characters
        if (max_idx != charset_size - 1 && max_idx != prev_char) {
            result += charset[max_idx];
        }
        
        prev_char = max_idx;
    }

    return result;
}

// ==================== MetadataProbe ====================

MetadataProbe::MetadataProbe(const std::string& homography_file, float fps)
    : speed_calc_(cv::Mat::eye(3, 3, CV_64F), fps)  // Default identity matrix
    , speed_limit_(60.0f)
{
    // Load homography matrix from YAML file
    cv::FileStorage fs(homography_file, cv::FileStorage::READ);
    if (fs.isOpened()) {
        cv::Mat H;
        fs["homography_matrix"] >> H;
        if (!H.empty()) {
            speed_calc_ = SpeedCalculator(H, fps);
            std::cout << "Loaded homography matrix from " << homography_file << std::endl;
        }
        fs.release();
    } else {
        std::cerr << "Warning: Could not load homography file: " << homography_file << std::endl;
    }
}

GstPadProbeReturn MetadataProbe::callback(
    GstPad* pad, GstPadProbeInfo* info, gpointer u_data)
{
    MetadataProbe* probe = static_cast<MetadataProbe*>(u_data);
    
    GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
    if (!buf) {
        return GST_PAD_PROBE_OK;
    }

    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) {
        return GST_PAD_PROBE_OK;
    }

    std::string json = probe->buildJSON(batch_meta);
    
    // TODO: Send JSON to Python via shared memory or callback
    // For now, just print
    if (!json.empty() && json != "[]") {
        std::cout << "Metadata: " << json << std::endl;
    }

    return GST_PAD_PROBE_OK;
}

std::string MetadataProbe::buildJSON(void* batch_meta_ptr) {
    NvDsBatchMeta* batch_meta = static_cast<NvDsBatchMeta*>(batch_meta_ptr);
    
    std::ostringstream json;
    json << "[";
    bool first_obj = true;

    for (NvDsMetaList* l_frame = batch_meta->frame_meta_list;
         l_frame != nullptr; l_frame = l_frame->next) {
        
        NvDsFrameMeta* frame_meta = (NvDsFrameMeta*)(l_frame->data);

        for (NvDsMetaList* l_obj = frame_meta->obj_meta_list;
             l_obj != nullptr; l_obj = l_obj->next) {
            
            NvDsObjectMeta* obj_meta = (NvDsObjectMeta*)(l_obj->data);

            // Only process vehicle classes (car, truck, bus, motorcycle)
            if (obj_meta->class_id != 2 && obj_meta->class_id != 3 &&
                obj_meta->class_id != 5 && obj_meta->class_id != 7) {
                continue;
            }

            // Calculate center and bottom point
            float cx = obj_meta->rect_params.left + obj_meta->rect_params.width / 2.0f;
            float bottom_y = obj_meta->rect_params.top + obj_meta->rect_params.height;

            // Calculate speed
            float speed = speed_calc_.calculateSpeed(
                obj_meta->object_id, cx, bottom_y);

            // Extract license plate
            std::string plate = LicensePlateExtractor::extractPlate(obj_meta);

            // Build JSON object
            if (!first_obj) json << ",";
            first_obj = false;

            json << "{"
                 << "\"track_id\":" << obj_meta->object_id << ","
                 << "\"x\":" << static_cast<int>(obj_meta->rect_params.left) << ","
                 << "\"y\":" << static_cast<int>(obj_meta->rect_params.top) << ","
                 << "\"width\":" << static_cast<int>(obj_meta->rect_params.width) << ","
                 << "\"height\":" << static_cast<int>(obj_meta->rect_params.height) << ","
                 << "\"class_id\":" << obj_meta->class_id << ","
                 << "\"confidence\":" << obj_meta->confidence << ","
                 << "\"speed\":" << (speed > 0 ? static_cast<int>(speed) : 0) << ","
                 << "\"plate\":\"" << plate << "\","
                 << "\"class\":\"";

            // Class name
            switch (obj_meta->class_id) {
                case 2: json << "car"; break;
                case 3: json << "motorcycle"; break;
                case 5: json << "bus"; break;
                case 7: json << "truck"; break;
                default: json << "vehicle"; break;
            }

            json << "\"}";
        }
    }

    json << "]";
    return json.str();
}

} // namespace deepstream
