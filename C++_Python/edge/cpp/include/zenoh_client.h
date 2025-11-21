/**
 * Zenoh P2P Client for Edge-to-Edge Offloading
 * Enables edges to share inference workload
 */

#ifndef ZENOH_CLIENT_H
#define ZENOH_CLIENT_H

#include <string>
#include <functional>
#include <memory>
#include <vector>

namespace zenoh {

// Forward declarations
struct Session;
struct Publisher;
struct Subscriber;

/**
 * Inference request/response structures
 */
struct InferenceRequest {
    std::string source_edge_id;
    int frame_number;
    std::vector<uint8_t> frame_data;  // Compressed JPEG
    int width;
    int height;
    float timestamp;
};

struct InferenceResponse {
    std::string source_edge_id;
    int frame_number;
    std::string metadata_json;  // Detection results
    float processing_time_ms;
};

/**
 * Zenoh P2P Client
 * Handles edge-to-edge communication for offloading
 */
class ZenohClient {
public:
    ZenohClient(const std::string& edge_id, const std::string& router_address = "tcp/127.0.0.1:7447");
    ~ZenohClient();

    /**
     * Connect to Zenoh router
     */
    bool connect();

    /**
     * Disconnect from router
     */
    void disconnect();

    /**
     * Check if connected
     */
    bool isConnected() const;

    /**
     * Send inference request to target edge
     * @param target_edge_id Target edge to offload to
     * @param request Inference request data
     * @return true if sent successfully
     */
    bool sendInferenceRequest(const std::string& target_edge_id, const InferenceRequest& request);

    /**
     * Set callback for incoming inference requests
     * @param callback Function to handle requests
     */
    void setRequestCallback(std::function<void(const InferenceRequest&)> callback);

    /**
     * Send inference response back to source edge
     * @param response Response data
     * @return true if sent successfully
     */
    bool sendInferenceResponse(const InferenceResponse& response);

    /**
     * Set callback for incoming inference responses
     * @param callback Function to handle responses
     */
    void setResponseCallback(std::function<void(const InferenceResponse&)> callback);

    /**
     * Discover available edges
     * @return List of edge IDs
     */
    std::vector<std::string> discoverEdges();

    /**
     * Publish edge status (for discovery)
     * @param status JSON status string
     */
    void publishStatus(const std::string& status);

private:
    std::string edge_id_;
    std::string router_address_;
    bool connected_;

    // Zenoh session and publishers/subscribers
    std::shared_ptr<Session> session_;
    std::shared_ptr<Publisher> request_pub_;
    std::shared_ptr<Publisher> response_pub_;
    std::shared_ptr<Publisher> status_pub_;
    std::shared_ptr<Subscriber> request_sub_;
    std::shared_ptr<Subscriber> response_sub_;
    std::shared_ptr<Subscriber> discovery_sub_;

    // Callbacks
    std::function<void(const InferenceRequest&)> request_callback_;
    std::function<void(const InferenceResponse&)> response_callback_;

    // Helper methods
    std::string getRequestTopic(const std::string& target_edge_id) const;
    std::string getResponseTopic() const;
    std::string getStatusTopic() const;
    std::string getDiscoveryTopic() const;
};

} // namespace zenoh

#endif // ZENOH_CLIENT_H
