/**
 * DeepStream Pipeline Header
 * Main pipeline with YOLO + LPRNet + Tracker + Analytics
 */

#ifndef DS_PIPELINE_H
#define DS_PIPELINE_H

#include <gst/gst.h>
#include <string>
#include <memory>
#include <functional>

namespace deepstream {

// Metadata callback function type
using MetadataCallback = std::function<void(const char* json_metadata)>;

/**
 * DeepStream Pipeline Manager
 * Manages GStreamer pipeline with YOLO and LPRNet inference
 */
class Pipeline {
public:
    Pipeline();
    ~Pipeline();

    /**
     * Build and configure pipeline
     * @param source_uri RTSP URL or file path
     * @param yolo_config Path to YOLO config file
     * @param lpr_config Path to LPRNet config file
     * @param tracker_config Path to tracker config
     * @param analytics_config Path to analytics config
     * @return true if successful
     */
    bool build(
        const std::string& source_uri,
        const std::string& yolo_config,
        const std::string& lpr_config,
        const std::string& tracker_config,
        const std::string& analytics_config
    );

    /**
     * Start pipeline
     * @return true if successful
     */
    bool start();

    /**
     * Stop pipeline
     */
    void stop();

    /**
     * Check if pipeline is running
     */
    bool isRunning() const;

    /**
     * Set metadata callback
     * Called for each frame with JSON metadata
     */
    void setMetadataCallback(MetadataCallback callback);

    /**
     * Get current FPS
     */
    float getFPS() const;

private:
    GstElement* pipeline_;
    GstElement* source_;
    GstElement* streammux_;
    GstElement* pgie_;        // Primary inference (YOLO)
    GstElement* tracker_;
    GstElement* sgie_;        // Secondary inference (LPRNet)
    GstElement* analytics_;
    GstElement* nvdsosd_;
    GstElement* sink_;

    GstBus* bus_;
    guint bus_watch_id_;

    bool running_;
    MetadataCallback metadata_callback_;

    // Helper methods
    GstElement* createElement(const char* factory, const char* name);
    bool linkElements();
    static GstPadProbeReturn osdSinkPadProbe(
        GstPad* pad, GstPadProbeInfo* info, gpointer u_data);
    static gboolean busCallback(GstBus* bus, GstMessage* msg, gpointer data);
};

} // namespace deepstream

#endif // DS_PIPELINE_H
