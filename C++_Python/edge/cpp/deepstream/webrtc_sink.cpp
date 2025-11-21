/**
 * WebRTC Sink Implementation
 * GStreamer webrtcbin integration for video streaming
 */

#include "webrtc_sink.h"
#include <iostream>
#include <json/json.h>

namespace deepstream {

WebRTCSink::WebRTCSink()
    : webrtcbin_(nullptr)
    , h264enc_(nullptr)
    , rtph264pay_(nullptr)
    , queue_(nullptr)
    , on_offer_(nullptr)
    , on_ice_candidate_(nullptr)
{
}

WebRTCSink::~WebRTCSink() {
    // Elements will be cleaned up by pipeline
}

GstElement* WebRTCSink::createSink(GstPipeline* pipeline) {
    // Create elements
    queue_ = gst_element_factory_make("queue", "webrtc-queue");
    h264enc_ = gst_element_factory_make("nvv4l2h264enc", "h264-encoder");
    rtph264pay_ = gst_element_factory_make("rtph264pay", "rtp-payloader");
    webrtcbin_ = gst_element_factory_make("webrtcbin", "webrtcbin");

    if (!queue_ || !h264enc_ || !rtph264pay_ || !webrtcbin_) {
        std::cerr << "[WebRTC] Failed to create elements" << std::endl;
        return nullptr;
    }

    // Configure H.264 encoder for low latency
    g_object_set(G_OBJECT(h264enc_),
        "bitrate", 4000000,           // 4 Mbps
        "profile", 0,                 // Baseline
        "preset-level", 1,            // UltraFastPreset
        "insert-sps-pps", TRUE,
        "insert-vui", TRUE,
        "idrinterval", 30,            // IDR every 30 frames
        nullptr);

    // Configure RTP payloader
    g_object_set(G_OBJECT(rtph264pay_),
        "config-interval", 1,
        "pt", 96,
        nullptr);

    // Configure webrtcbin
    g_object_set(G_OBJECT(webrtcbin_),
        "bundle-policy", 3,           // max-bundle
        "stun-server", "stun://stun.l.google.com:19302",
        nullptr);

    // Add elements to pipeline
    gst_bin_add_many(GST_BIN(pipeline),
        queue_, h264enc_, rtph264pay_, webrtcbin_, nullptr);

    // Link elements: queue -> h264enc -> rtph264pay -> webrtcbin
    if (!gst_element_link_many(queue_, h264enc_, rtph264pay_, nullptr)) {
        std::cerr << "[WebRTC] Failed to link encoder elements" << std::endl;
        return nullptr;
    }

    // Link RTP payloader to webrtcbin
    GstPad* payloader_src = gst_element_get_static_pad(rtph264pay_, "src");
    GstPad* webrtc_sink = gst_element_get_request_pad(webrtcbin_, "sink_%u");
    
    if (gst_pad_link(payloader_src, webrtc_sink) != GST_PAD_LINK_OK) {
        std::cerr << "[WebRTC] Failed to link payloader to webrtcbin" << std::endl;
        gst_object_unref(payloader_src);
        gst_object_unref(webrtc_sink);
        return nullptr;
    }

    gst_object_unref(payloader_src);
    gst_object_unref(webrtc_sink);

    // Connect signals
    g_signal_connect(webrtcbin_, "on-negotiation-needed",
        G_CALLBACK(onNegotiationNeeded), this);
    g_signal_connect(webrtcbin_, "on-ice-candidate",
        G_CALLBACK(onIceCandidate), this);

    std::cout << "[WebRTC] Sink created successfully" << std::endl;

    // Return queue element (this is what gets linked to from pipeline)
    return queue_;
}

void WebRTCSink::setRemoteDescription(const std::string& sdp) {
    if (!webrtcbin_) {
        std::cerr << "[WebRTC] webrtcbin not initialized" << std::endl;
        return;
    }

    std::cout << "[WebRTC] Setting remote description" << std::endl;

    GstSDPMessage* sdp_msg;
    gst_sdp_message_new(&sdp_msg);
    gst_sdp_message_parse_buffer((guint8*)sdp.c_str(), sdp.length(), sdp_msg);

    GstWebRTCSessionDescription* answer = gst_webrtc_session_description_new(
        GST_WEBRTC_SDP_TYPE_ANSWER, sdp_msg);

    GstPromise* promise = gst_promise_new();
    g_signal_emit_by_name(webrtcbin_, "set-remote-description", answer, promise);
    gst_promise_unref(promise);

    gst_webrtc_session_description_free(answer);
}

void WebRTCSink::addIceCandidate(const std::string& candidate, int sdp_mline_index) {
    if (!webrtcbin_) {
        return;
    }

    std::cout << "[WebRTC] Adding ICE candidate: " << candidate << std::endl;

    g_signal_emit_by_name(webrtcbin_, "add-ice-candidate", 
                          sdp_mline_index, candidate.c_str());
}

void WebRTCSink::setOnOfferCallback(OnOfferCallback callback) {
    on_offer_ = callback;
}

void WebRTCSink::setOnIceCandidateCallback(OnIceCandidateCallback callback) {
    on_ice_candidate_ = callback;
}

void WebRTCSink::onNegotiationNeeded(GstElement* webrtc, gpointer user_data) {
    WebRTCSink* sink = static_cast<WebRTCSink*>(user_data);

    std::cout << "[WebRTC] Negotiation needed, creating offer" << std::endl;

    GstPromise* promise = gst_promise_new_with_change_func(
        onOfferCreated, user_data, nullptr);
    
    g_signal_emit_by_name(webrtc, "create-offer", nullptr, promise);
}

void WebRTCSink::onOfferCreated(GstPromise* promise, gpointer user_data) {
    WebRTCSink* sink = static_cast<WebRTCSink*>(user_data);

    const GstStructure* reply = gst_promise_get_reply(promise);
    GstWebRTCSessionDescription* offer = nullptr;
    
    gst_structure_get(reply, "offer",
        GST_TYPE_WEBRTC_SESSION_DESCRIPTION, &offer, nullptr);

    gst_promise_unref(promise);

    // Set local description
    promise = gst_promise_new();
    g_signal_emit_by_name(sink->webrtcbin_, "set-local-description", offer, promise);
    gst_promise_unref(promise);

    // Get SDP string
    gchar* sdp_string = gst_sdp_message_as_text(offer->sdp);
    std::string sdp(sdp_string);
    g_free(sdp_string);

    std::cout << "[WebRTC] Offer created" << std::endl;

    // Call Python callback with SDP offer
    if (sink->on_offer_) {
        sink->on_offer_(sdp);
    }

    gst_webrtc_session_description_free(offer);
}

void WebRTCSink::onIceCandidate(GstElement* webrtc, guint mlineindex,
                                 gchar* candidate, gpointer user_data) {
    WebRTCSink* sink = static_cast<WebRTCSink*>(user_data);

    std::cout << "[WebRTC] ICE candidate: " << candidate << std::endl;

    // Call Python callback with ICE candidate
    if (sink->on_ice_candidate_) {
        sink->on_ice_candidate_(std::string(candidate), mlineindex);
    }
}

} // namespace deepstream
