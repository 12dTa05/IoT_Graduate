/**
 * FlatBuffers Serializer for Detection Metadata
 * Zero-copy, high-performance alternative to JSON
 */

#ifndef FLATBUFFERS_SERIALIZER_H
#define FLATBUFFERS_SERIALIZER_H

#include <flatbuffers/flatbuffers.h>
#include "metadata_generated.h"  // Generated from metadata.fbs
#include <vector>
#include <string>

namespace serialization {

/**
 * Object data structure (matches DeepStream metadata)
 */
struct ObjectData {
    uint32_t track_id;
    float x, y, width, height;
    int class_id;
    std::string class_name;
    float confidence;
    float speed;
    std::string plate;
    float plate_confidence;
    double timestamp;
    bool is_overspeed;
};

/**
 * Frame data structure
 */
struct FrameData {
    uint32_t frame_number;
    double timestamp;
    std::vector<ObjectData> objects;
    float fps;
    float inference_time_ms;
    float tracking_time_ms;
    std::string source_id;
    uint32_t width;
    uint32_t height;
};

/**
 * FlatBuffers Serializer
 * Converts detection data to FlatBuffers binary format
 */
class FlatBuffersSerializer {
public:
    FlatBuffersSerializer(size_t initial_size = 1024);
    ~FlatBuffersSerializer();

    /**
     * Serialize frame metadata to FlatBuffers
     * @param frame Frame data
     * @return Pointer to buffer and size
     */
    std::pair<const uint8_t*, size_t> serialize(const FrameData& frame);

    /**
     * Serialize single object
     * @param obj Object data
     * @return FlatBuffers offset
     */
    flatbuffers::Offset<metadata::DetectionObject> serializeObject(
        const ObjectData& obj);

    /**
     * Get buffer pointer (zero-copy)
     */
    const uint8_t* getBuffer() const;

    /**
     * Get buffer size
     */
    size_t getSize() const;

    /**
     * Reset builder for reuse
     */
    void reset();

private:
    flatbuffers::FlatBufferBuilder builder_;
};

/**
 * FlatBuffers Deserializer
 * Zero-copy access to FlatBuffers data
 */
class FlatBuffersDeserializer {
public:
    /**
     * Deserialize frame metadata from buffer
     * @param buffer FlatBuffers binary data
     * @param size Buffer size
     * @return Frame data (zero-copy access)
     */
    static const metadata::FrameMetadata* deserialize(
        const uint8_t* buffer, size_t size);

    /**
     * Verify buffer integrity
     */
    static bool verify(const uint8_t* buffer, size_t size);

    /**
     * Convert to JSON (for debugging/compatibility)
     */
    static std::string toJSON(const metadata::FrameMetadata* frame);
};

} // namespace serialization

#endif // FLATBUFFERS_SERIALIZER_H
