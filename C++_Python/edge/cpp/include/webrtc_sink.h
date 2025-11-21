/**
 * WebRTC Sink for DeepStream
 * Replaces fakesink with webrtcbin for real-time streaming
 */

#ifndef WEBRTC_SINK_H
#define WEBRTC_SINK_H

#include <gst/gst.h>
#include <gst/webrtc/webrtc.h>
#include <string>
#include <functional>

namespace deepstream {

/**
 * WebRTC signaling callback types
 */
using OnOfferCallback = std::function<void(const std::string& sdp)>;
using OnIceCandidateCallback = std::function<void(const std::string& candidate, int sdp_mline_index)>;

/**
 * WebRTC Sink Manager
 * Handles WebRTC streaming with signaling
 */
class WebRTCSink {
public:
    WebRTCSink();
    ~WebRTCSink();

    /**
     * Create WebRTC sink elements
     * @param pipeline Parent pipeline
     * @return webrtcbin element
     */
    GstElement* createSink(GstPipeline* pipeline);

    /**
     * Set remote SDP answer from client
     * @param sdp SDP answer string
     */
    void setRemoteDescription(const std::string& sdp);

    /**
     * Add ICE candidate from client
     * @param candidate ICE candidate string
     * @param sdp_mline_index SDP m-line index
     */
    void addIceCandidate(const std::string& candidate, int sdp_mline_index);

    /**
     * Set callback for SDP offer
     */
    void setOnOfferCallback(OnOfferCallback callback);

    /**
     * Set callback for ICE candidates
     */
    void setOnIceCandidateCallback(OnIceCandidateCallback callback);

private:
    GstElement* webrtcbin_;
    GstElement* h264enc_;
    GstElement* rtph264pay_;
    GstElement* queue_;

    OnOfferCallback on_offer_;
    OnIceCandidateCallback on_ice_candidate_;

    // Signal handlers
    static void onNegotiationNeeded(GstElement* webrtc, gpointer user_data);
    static void onIceCandidate(GstElement* webrtc, guint mlineindex, 
                               gchar* candidate, gpointer user_data);
    static void onOfferCreated(GstPromise* promise, gpointer user_data);
};

} // namespace deepstream

#endif // WEBRTC_SINK_H
