/**
 * FlatBuffers Serializer Implementation
 */

#include "flatbuffers_serializer.h"
#include <iostream>

namespace serialization {

FlatBuffersSerializer::FlatBuffersSerializer(size_t initial_size)
    : builder_(initial_size)
{
}

FlatBuffersSerializer::~FlatBuffersSerializer() {
}

std::pair<const uint8_t*, size_t> FlatBuffersSerializer::serialize(const FrameData& frame) {
    // Reset builder
    builder_.Clear();

    // Serialize all objects
    std::vector<flatbuffers::Offset<metadata::DetectionObject>> objects;
    objects.reserve(frame.objects.size());

    for (const auto& obj : frame.objects) {
        objects.push_back(serializeObject(obj));
    }

    // Create objects vector
    auto objects_vec = builder_.CreateVector(objects);

    // Create source ID string
    auto source_id = builder_.CreateString(frame.source_id);

    // Calculate total time
    float total_time = frame.inference_time_ms + frame.tracking_time_ms;

    // Create FrameMetadata
    auto frame_metadata = metadata::CreateFrameMetadata(builder_,
        frame.frame_number,
        frame.timestamp,
        objects_vec,
        static_cast<uint32_t>(frame.objects.size()),
        frame.fps,
        frame.inference_time_ms,
        frame.tracking_time_ms,
        total_time,
        source_id,
        frame.width,
        frame.height
    );

    // Finish buffer
    builder_.Finish(frame_metadata);

    return {builder_.GetBufferPointer(), builder_.GetSize()};
}

flatbuffers::Offset<metadata::DetectionObject> FlatBuffersSerializer::serializeObject(
    const ObjectData& obj)
{
    // Create bounding box
    auto bbox = metadata::BoundingBox(obj.x, obj.y, obj.width, obj.height);

    // Create strings
    auto class_name = builder_.CreateString(obj.class_name);
    auto plate = builder_.CreateString(obj.plate);
    auto speed_unit = builder_.CreateString("km/h");
    auto direction = builder_.CreateString("");  // TODO: Calculate direction

    // Create DetectionObject
    return metadata::CreateDetectionObject(builder_,
        obj.track_id,
        &bbox,
        obj.class_id,
        class_name,
        obj.confidence,
        obj.speed,
        speed_unit,
        plate,
        obj.plate_confidence,
        obj.timestamp,
        obj.timestamp,  // first_seen (TODO: track this)
        obj.timestamp,  // last_seen
        obj.is_overspeed,
        direction
    );
}

const uint8_t* FlatBuffersSerializer::getBuffer() const {
    return builder_.GetBufferPointer();
}

size_t FlatBuffersSerializer::getSize() const {
    return builder_.GetSize();
}

void FlatBuffersSerializer::reset() {
    builder_.Clear();
}

// Deserializer

const metadata::FrameMetadata* FlatBuffersDeserializer::deserialize(
    const uint8_t* buffer, size_t size)
{
    // Verify buffer first
    if (!verify(buffer, size)) {
        std::cerr << "[FlatBuffers] Buffer verification failed" << std::endl;
        return nullptr;
    }

    // Get root (zero-copy!)
    return metadata::GetFrameMetadata(buffer);
}

bool FlatBuffersDeserializer::verify(const uint8_t* buffer, size_t size) {
    flatbuffers::Verifier verifier(buffer, size);
    return metadata::VerifyFrameMetadataBuffer(verifier);
}

std::string FlatBuffersDeserializer::toJSON(const metadata::FrameMetadata* frame) {
    if (!frame) {
        return "{}";
    }

    std::ostringstream json;
    json << "{";
    json << "\"frame_number\":" << frame->frame_number() << ",";
    json << "\"timestamp\":" << frame->timestamp() << ",";
    json << "\"fps\":" << frame->fps() << ",";
    json << "\"object_count\":" << frame->object_count() << ",";
    json << "\"objects\":[";

    auto objects = frame->objects();
    for (size_t i = 0; i < objects->size(); i++) {
        auto obj = objects->Get(i);
        
        if (i > 0) json << ",";
        json << "{";
        json << "\"track_id\":" << obj->track_id() << ",";
        json << "\"x\":" << obj->bbox()->x() << ",";
        json << "\"y\":" << obj->bbox()->y() << ",";
        json << "\"width\":" << obj->bbox()->width() << ",";
        json << "\"height\":" << obj->bbox()->height() << ",";
        json << "\"class_id\":" << obj->class_id() << ",";
        json << "\"class_name\":\"" << obj->class_name()->c_str() << "\",";
        json << "\"confidence\":" << obj->confidence() << ",";
        json << "\"speed\":" << obj->speed() << ",";
        json << "\"plate\":\"" << obj->plate()->c_str() << "\",";
        json << "\"is_overspeed\":" << (obj->is_overspeed() ? "true" : "false");
        json << "}";
    }

    json << "]}";
    return json.str();
}

} // namespace serialization
