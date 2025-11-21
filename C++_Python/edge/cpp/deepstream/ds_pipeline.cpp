/**
 * DeepStream Pipeline Implementation
 * GStreamer pipeline with YOLO (primary) + LPRNet (secondary)
 */

#include "ds_pipeline.h"
#include "gstnvdsmeta.h"
#include "nvds_analytics_meta.h"
#include <iostream>
#include <sstream>
#include <cstring>

namespace deepstream {

Pipeline::Pipeline()
    : pipeline_(nullptr)
    , source_(nullptr)
    , streammux_(nullptr)
    , pgie_(nullptr)
    , tracker_(nullptr)
    , sgie_(nullptr)
    , analytics_(nullptr)
    , nvdsosd_(nullptr)
    , sink_(nullptr)
    , bus_(nullptr)
    , bus_watch_id_(0)
    , running_(false)
    , metadata_callback_(nullptr)
{
    gst_init(nullptr, nullptr);
}

Pipeline::~Pipeline() {
    stop();
    if (pipeline_) {
        gst_object_unref(pipeline_);
    }
}

GstElement* Pipeline::createElement(const char* factory, const char* name) {
    GstElement* element = gst_element_factory_make(factory, name);
    if (!element) {
        std::cerr << "Failed to create element: " << factory << std::endl;
        return nullptr;
    }
    return element;
}

bool Pipeline::build(
    const std::string& source_uri,
    const std::string& yolo_config,
    const std::string& lpr_config,
    const std::string& tracker_config,
    const std::string& analytics_config)
{
    // Create pipeline
    pipeline_ = gst_pipeline_new("deepstream-pipeline");
    if (!pipeline_) {
        std::cerr << "Failed to create pipeline" << std::endl;
        return false;
    }

    // Create elements
    source_ = createElement("uridecodebin", "source");
    streammux_ = createElement("nvstreammux", "stream-muxer");
    pgie_ = createElement("nvinfer", "primary-infer");
    tracker_ = createElement("nvtracker", "tracker");
    sgie_ = createElement("nvinfer", "secondary-infer");
    analytics_ = createElement("nvdsanalytics", "analytics");
    nvdsosd_ = createElement("nvdsosd", "onscreendisplay");
    
    // For now, use fakesink (will be replaced with WebRTC)
    sink_ = createElement("fakesink", "sink");

    // Check all elements created
    if (!source_ || !streammux_ || !pgie_ || !tracker_ || 
        !sgie_ || !analytics_ || !nvdsosd_ || !sink_) {
        std::cerr << "Failed to create one or more elements" << std::endl;
        return false;
    }

    // Configure source
    g_object_set(G_OBJECT(source_), "uri", source_uri.c_str(), nullptr);

    // Configure streammux
    g_object_set(G_OBJECT(streammux_),
        "batch-size", 1,
        "width", 1280,
        "height", 720,
        "batched-push-timeout", 40000,
        "live-source", 1,
        nullptr);

    // Configure primary inference (YOLO)
    g_object_set(G_OBJECT(pgie_),
        "config-file-path", yolo_config.c_str(),
        nullptr);

    // Configure tracker
    g_object_set(G_OBJECT(tracker_),
        "ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so",
        "ll-config-file", tracker_config.c_str(),
        "tracker-width", 640,
        "tracker-height", 384,
        "gpu-id", 0,
        nullptr);

    // Configure secondary inference (LPRNet)
    g_object_set(G_OBJECT(sgie_),
        "config-file-path", lpr_config.c_str(),
        nullptr);

    // Configure analytics
    g_object_set(G_OBJECT(analytics_),
        "config-file", analytics_config.c_str(),
        nullptr);

    // Configure OSD
    g_object_set(G_OBJECT(nvdsosd_),
        "display-text", 1,
        "display-bbox", 1,
        nullptr);

    // Configure sink
    g_object_set(G_OBJECT(sink_),
        "sync", FALSE,
        nullptr);

    // Add elements to pipeline
    gst_bin_add_many(GST_BIN(pipeline_),
        source_, streammux_, pgie_, tracker_, sgie_,
        analytics_, nvdsosd_, sink_, nullptr);

    // Link elements
    if (!linkElements()) {
        std::cerr << "Failed to link elements" << std::endl;
        return false;
    }

    // Add probe on OSD sink pad for metadata extraction
    GstPad* osd_sink_pad = gst_element_get_static_pad(nvdsosd_, "sink");
    if (!osd_sink_pad) {
        std::cerr << "Failed to get OSD sink pad" << std::endl;
        return false;
    }

    gst_pad_add_probe(osd_sink_pad, GST_PAD_PROBE_TYPE_BUFFER,
        osdSinkPadProbe, this, nullptr);
    gst_object_unref(osd_sink_pad);

    // Setup bus
    bus_ = gst_pipeline_get_bus(GST_PIPELINE(pipeline_));
    bus_watch_id_ = gst_bus_add_watch(bus_, busCallback, this);
    gst_object_unref(bus_);

    std::cout << "Pipeline built successfully" << std::endl;
    return true;
}

bool Pipeline::linkElements() {
    // Link: streammux -> pgie -> tracker -> sgie -> analytics -> nvdsosd -> sink
    if (!gst_element_link_many(streammux_, pgie_, tracker_, sgie_,
                                analytics_, nvdsosd_, sink_, nullptr)) {
        std::cerr << "Failed to link pipeline elements" << std::endl;
        return false;
    }

    // Connect source pad-added signal for dynamic linking
    g_signal_connect(source_, "pad-added", G_CALLBACK(+[](
        GstElement* src, GstPad* new_pad, gpointer data) {
        
        Pipeline* pipeline = static_cast<Pipeline*>(data);
        GstPad* sink_pad = gst_element_get_request_pad(
            pipeline->streammux_, "sink_0");
        
        if (gst_pad_is_linked(sink_pad)) {
            gst_object_unref(sink_pad);
            return;
        }

        GstCaps* caps = gst_pad_get_current_caps(new_pad);
        if (!caps) {
            gst_object_unref(sink_pad);
            return;
        }

        const gchar* name = gst_structure_get_name(
            gst_caps_get_structure(caps, 0));
        
        if (g_str_has_prefix(name, "video/")) {
            GstPadLinkReturn ret = gst_pad_link(new_pad, sink_pad);
            if (GST_PAD_LINK_FAILED(ret)) {
                std::cerr << "Failed to link source to streammux" << std::endl;
            } else {
                std::cout << "Source linked to streammux" << std::endl;
            }
        }

        gst_caps_unref(caps);
        gst_object_unref(sink_pad);
    }), this);

    return true;
}

bool Pipeline::start() {
    if (!pipeline_) {
        std::cerr << "Pipeline not built" << std::endl;
        return false;
    }

    GstStateChangeReturn ret = gst_element_set_state(
        pipeline_, GST_STATE_PLAYING);
    
    if (ret == GST_STATE_CHANGE_FAILURE) {
        std::cerr << "Failed to set pipeline to PLAYING state" << std::endl;
        return false;
    }

    running_ = true;
    std::cout << "Pipeline started" << std::endl;
    return true;
}

void Pipeline::stop() {
    if (pipeline_ && running_) {
        gst_element_set_state(pipeline_, GST_STATE_NULL);
        running_ = false;
        std::cout << "Pipeline stopped" << std::endl;
    }

    if (bus_watch_id_ > 0) {
        g_source_remove(bus_watch_id_);
        bus_watch_id_ = 0;
    }
}

bool Pipeline::isRunning() const {
    return running_;
}

void Pipeline::setMetadataCallback(MetadataCallback callback) {
    metadata_callback_ = callback;
}

float Pipeline::getFPS() const {
    // TODO: Calculate actual FPS from frame timestamps
    return 25.0f;
}

GstPadProbeReturn Pipeline::osdSinkPadProbe(
    GstPad* pad, GstPadProbeInfo* info, gpointer u_data)
{
    Pipeline* pipeline = static_cast<Pipeline*>(u_data);
    
    GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
    if (!buf) {
        return GST_PAD_PROBE_OK;
    }

    NvDsBatchMeta* batch_meta = gst_buffer_get_nvds_batch_meta(buf);
    if (!batch_meta) {
        return GST_PAD_PROBE_OK;
    }

    // Build JSON metadata
    std::ostringstream json;
    json << "[";
    bool first_obj = true;

    for (NvDsMetaList* l_frame = batch_meta->frame_meta_list;
         l_frame != nullptr; l_frame = l_frame->next) {
        
        NvDsFrameMeta* frame_meta = (NvDsFrameMeta*)(l_frame->data);

        for (NvDsMetaList* l_obj = frame_meta->obj_meta_list;
             l_obj != nullptr; l_obj = l_obj->next) {
            
            NvDsObjectMeta* obj_meta = (NvDsObjectMeta*)(l_obj->data);

            if (!first_obj) json << ",";
            first_obj = false;

            json << "{"
                 << "\"track_id\":" << obj_meta->object_id << ","
                 << "\"x\":" << obj_meta->rect_params.left << ","
                 << "\"y\":" << obj_meta->rect_params.top << ","
                 << "\"width\":" << obj_meta->rect_params.width << ","
                 << "\"height\":" << obj_meta->rect_params.height << ","
                 << "\"class_id\":" << obj_meta->class_id << ","
                 << "\"confidence\":" << obj_meta->confidence << ","
                 << "\"speed\":0,"  // TODO: Calculate from homography
                 << "\"plate\":\"\"" // TODO: Extract from LPRNet
                 << "}";
        }
    }

    json << "]";

    // Call Python callback with JSON metadata
    if (pipeline->metadata_callback_) {
        pipeline->metadata_callback_(json.str().c_str());
    }

    return GST_PAD_PROBE_OK;
}

gboolean Pipeline::busCallback(GstBus* bus, GstMessage* msg, gpointer data) {
    Pipeline* pipeline = static_cast<Pipeline*>(data);

    switch (GST_MESSAGE_TYPE(msg)) {
        case GST_MESSAGE_EOS:
            std::cout << "End of stream" << std::endl;
            pipeline->stop();
            break;

        case GST_MESSAGE_ERROR: {
            gchar* debug;
            GError* error;
            gst_message_parse_error(msg, &error, &debug);
            std::cerr << "Error: " << error->message << std::endl;
            if (debug) {
                std::cerr << "Debug: " << debug << std::endl;
            }
            g_free(debug);
            g_error_free(error);
            pipeline->stop();
            break;
        }

        case GST_MESSAGE_WARNING: {
            gchar* debug;
            GError* warning;
            gst_message_parse_warning(msg, &warning, &debug);
            std::cerr << "Warning: " << warning->message << std::endl;
            g_free(debug);
            g_error_free(warning);
            break;
        }

        default:
            break;
    }

    return TRUE;
}

} // namespace deepstream
